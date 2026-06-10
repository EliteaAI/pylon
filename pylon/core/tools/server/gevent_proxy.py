#!/usr/bin/python3
# coding=utf-8

#   Copyright 2026 EPAM Systems
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""
    Gevent Proxy Server

    This module implements a two-process architecture for pylon:
    
    Process 1 (gevent proxy) - handles HTTP/WS connections via gevent WSGI server,
    forwarding requests to Process 2 via ZMQ IPC. No gevent monkey-patching of the
    app code — only the I/O layer uses gevent.
    
    Process 2 (worker) - runs the actual Flask app hierarchy + SocketIO event handlers
    using native threads, without gevent monkey-patching. This allows CPU-bound tasks
    to use multiple cores naturally.
"""

import io
import os
import sys
import json
import time
import uuid
import queue
import pickle
import struct
import threading

from pylon.core import constants
from pylon.core.tools import log


def run_proxy_server(context):
    """ Run the gevent proxy server that forwards requests to a worker process """
    from gevent.pywsgi import WSGIServer  # pylint: disable=E0401,C0412,C0415
    from geventwebsocket.handler import WebSocketHandler  # pylint: disable=E0401,C0412,C0415
    from .splash import boot_splash_hook  # pylint: disable=C0415
    #
    # Build the proxy WSGI app
    proxy_config = context.settings.get("server", {}).get("proxy", {})
    proxy_app = GeventProxyApp(context, proxy_config)
    #
    # Wrap the root router with the proxy app
    original_router = context.root_router
    context.root_router = ProxyRouterWrapper(original_router, proxy_app)
    #
    if boot_splash_hook in context.root_router.hooks:
        context.root_router.hooks.remove(boot_splash_hook)
    #
    # Make and start the gevent WSGI server
    http_server = WSGIServer(
        (
            context.settings.get("server", {}).get("host", constants.SERVER_DEFAULT_HOST),
            context.settings.get("server", {}).get("port", constants.SERVER_DEFAULT_PORT)
        ),
        context.root_router,
        handler_class=WebSocketHandler,
        **context.settings.get("server", {}).get("kwargs", {}),
    )
    #
    setattr(http_server, "pre_start_hook", _http_server_pre_start_hook)
    http_server.start()
    #
    try:
        context.stop_event.wait()
    finally:
        from gevent.greenlet import Greenlet  # pylint: disable=E0401,C0412,C0415
        Greenlet.spawn(http_server.stop, timeout=None).join()
        proxy_app.shutdown()


def make_proxy_server(context):
    """ Make the gevent proxy WSGI server (early startup) """
    from gevent.pywsgi import WSGIServer  # pylint: disable=E0401,C0412,C0415
    from geventwebsocket.handler import WebSocketHandler  # pylint: disable=E0401,C0412,C0415
    from .splash import boot_splash_hook  # pylint: disable=C0415
    #
    proxy_config = context.settings.get("server", {}).get("proxy", {})
    proxy_app = GeventProxyApp(context, proxy_config)
    #
    # Store the proxy app on context so _proxy_main_loop can start its ZMQ connection
    context.proxy_app = proxy_app
    #
    # Wrap the root router with the proxy app
    original_router = context.root_router
    context.root_router = ProxyRouterWrapper(original_router, proxy_app)
    #
    if boot_splash_hook in context.root_router.hooks:
        context.root_router.hooks.remove(boot_splash_hook)
    #
    http_server = WSGIServer(
        (
            context.settings.get("server", {}).get("host", constants.SERVER_DEFAULT_HOST),
            context.settings.get("server", {}).get("port", constants.SERVER_DEFAULT_PORT)
        ),
        context.root_router,
        handler_class=WebSocketHandler,
        **context.settings.get("server", {}).get("kwargs", {}),
    )
    #
    setattr(http_server, "pre_start_hook", _http_server_pre_start_hook)
    #
    return http_server


class GeventProxyApp:
    """
    WSGI application that forwards requests to a worker process via ZMQ IPC.
    
    Uses the arbiter RpcNode pattern over a local ZeroMQ EventNode to communicate
    with the worker process. The worker registers wsgi_call and sio_call RPC functions
    that this proxy calls for each incoming request.
    
    The proxy binds a ZeroMQServerNode as a broker, then connects to it as a client.
    The worker also connects to the same broker. This allows bidirectional
    communication between proxy and worker.
    """

    def __init__(self, context, config):
        self.context = context
        self.config = config
        #
        # ZMQ IPC configuration
        # Use a shared RPC prefix that both proxy and worker agree on.
        # By default use context.node_name (hostname, same in both processes).
        # Can be overridden via config.
        self.zmq_id = config.get("rpc_prefix", f"proxy_{context.node_name}")
        self.ipc_sub = config.get("ipc_sub", "ipc:///tmp/pylon_proxy_sub.ipc")
        self.ipc_push = config.get("ipc_push", "ipc:///tmp/pylon_proxy_push.ipc")
        self.rpc_timeout = config.get("rpc_timeout", 300)
        self.rpc_retries = config.get("rpc_retries", 3)
        self.worker_start_timeout = config.get("worker_start_timeout", 30)
        #
        # Broker server node (binds PUB/PULL so worker can connect)
        self.server_node = None
        # EventNode and RpcNode for proxy-to-worker communication
        self.event_node = None
        self.rpc_node = None
        self.started = False

    def start(self):
        """ Start the ZMQ proxy connection """
        if self.started:
            return
        #
        import arbiter  # pylint: disable=E0401,C0415
        #
        # Step 1: Start a ZeroMQServerNode as broker — binds PUB and PULL
        # so both proxy and worker can connect SUB and PUSH to it.
        self.server_node = arbiter.ZeroMQServerNode(
            bind_pub=self.ipc_sub,
            bind_pull=self.ipc_push,
            sockopt_linger=1000,
        )
        self.server_node.start()
        #
        # Step 2: Connect to the broker as a client
        self.event_node = arbiter.ZeroMQEventNode(
            connect_sub=self.ipc_sub,
            connect_push=self.ipc_push,
            topic="proxy_events",
            hmac_key=self.config.get("hmac_key", None),
            hmac_digest=self.config.get("hmac_digest", "sha512"),
            callback_workers=1,
            mute_first_failed_connections=0,
            log_errors=True,
        )
        self.event_node.start()
        #
        self.rpc_node = arbiter.RpcNode(
            self.event_node,
            id_prefix=f"{self.zmq_id}_",
            trace=self.config.get("trace", False),
            proxy_timeout=self.rpc_timeout,
        )
        self.rpc_node.start()
        #
        self.started = True

    def shutdown(self):
        """ Stop the ZMQ proxy connection """
        if self.rpc_node is not None:
            self.rpc_node.stop()
        #
        if self.event_node is not None:
            self.event_node.stop()
        #
        if self.server_node is not None:
            self.server_node.stop()
        #
        self.started = False

    def wait_for_worker(self, timeout=None):
        """ Wait for worker to become available by pinging it """
        if timeout is None:
            timeout = self.worker_start_timeout
        #
        deadline = time.monotonic() + timeout
        ping_name = f"{self.zmq_id}_ping"
        #
        while time.monotonic() < deadline:
            try:
                result = self.rpc_node.call_with_timeout(
                    ping_name, timeout=5
                )
                if result is True:
                    return True
            except (queue.Empty, Exception):  # pylint: disable=W0703
                time.sleep(0.5)
        #
        return False

    def __call__(self, environ, start_response):
        """
        WSGI call: forward the request to the worker via ZMQ RPC.
        
        Serializes the WSGI environ, calls wsgi_call on the worker,
        and writes the response back.
        """
        if not self.started:
            return self._no_worker(environ, start_response)
        #
        # Prepare the environ for RPC transport
        rpc_environ = self._prepare_environ(environ)
        #
        try:
            wsgi_result = self.rpc_node.call_with_timeout(
                f"{self.zmq_id}_wsgi_call",
                self.rpc_timeout,
                rpc_environ,
            )
        except queue.Empty:
            log.warning("Worker WSGI call timeout, returning 504")
            start_response("504 Gateway Timeout", [
                ("Content-type", "text/plain"),
                ("Cache-Control", "no-store"),
            ])
            return [b"Worker timeout\n"]
        except Exception as exc:  # pylint: disable=W0703
            log.exception("Worker WSGI call failed")
            start_response("502 Bad Gateway", [
                ("Content-type", "text/plain"),
                ("Cache-Control", "no-store"),
            ])
            return [str(exc).encode("utf-8")]
        #
        status = wsgi_result["status"]
        headers = wsgi_result["headers"]
        body = wsgi_result["body"]
        #
        start_response(status, headers)
        return [body]

    def _prepare_environ(self, wsgi_environ):
        """ Serialize WSGI environ for RPC transport """
        result = dict(wsgi_environ)
        #
        # Drop non-serializable keys
        drop_keys = [
            "werkzeug.socket",
            "werkzeug.request",
            "waitress.client_disconnected",
            "wsgi.errors",
            "wsgi.file_wrapper",
        ]
        for key in drop_keys:
            result.pop(key, None)
        #
        # Read and encode the request body
        try:
            body = result.get("wsgi.input", "").read()
            result["wsgi.input"] = body
        except Exception:  # pylint: disable=W0703
            result["wsgi.input"] = b""
        #
        return result

    def _no_worker(self, environ, start_response):
        """ Fallback when no worker is available """
        start_response("503 Service Unavailable", [
            ("Content-type", "text/plain"),
            ("Cache-Control", "no-store, no-cache, max-age=0"),
        ])
        return [b"Proxy not connected to worker\n"]


class ProxyRouterWrapper:
    """
    Wraps the original RouterApp to intercept SocketIO and health endpoints,
    forwarding all other requests through the proxy.
    
    Also proxies `.map` and `.hooks` attributes to the original router so that
    code that modifies the router (e.g. app.py add_socketio_app) still works.
    """

    def __init__(self, original_router, proxy_app):
        self.original_router = original_router
        self.proxy_app = proxy_app
        self.hooks = original_router.hooks

    @property
    def map(self):
        """ Forward .map access to the original router """
        return self.original_router.map

    def __call__(self, environ, start_response):
        # Run hooks first (splash, etc.)
        for hook in list(self.hooks):
            try:
                hook_app = hook(self, environ, start_response)
            except Exception:  # pylint: disable=W0703
                hook_app = None
            #
            if hook_app is not None:
                return hook_app(environ, start_response)
        #
        # Route matching: check if this is a SocketIO or health endpoint
        # that should be handled locally by the proxy
        app_path = environ.get("PATH_INFO", "")
        root_path = environ.get("SCRIPT_NAME", "")
        #
        # Health endpoints — handle locally in the proxy (no app logic needed)
        for endpoint in ["/healthz/", "/livez/", "/readyz/"]:
            if app_path.startswith(endpoint) or app_path == endpoint.rstrip("/"):
                if endpoint.rstrip("/") in self.original_router.map:
                    return self.original_router.map[endpoint.rstrip("/")](environ, start_response)
        #
        # SocketIO endpoint — forward to worker
        socketio_route = None
        try:
            from tools import context  # pylint: disable=E0401,C0415
            socketio_route = context.socketio_route
        except Exception:  # pylint: disable=W0703
            pass
        #
        if socketio_route:
            route_item = socketio_route.rstrip("/")
            if app_path.startswith(socketio_route) or app_path == route_item:
                return self.proxy_app(environ, start_response)
        #
        # All other requests — forward to worker
        return self.proxy_app(environ, start_response)


def _http_server_pre_start_hook(handler):
    from tools import context  # pylint: disable=E0401,C0415
    #
    try:
        route = context.socketio_route
    except Exception:  # pylint: disable=W0702
        return True
    #
    route_item = route.rstrip("/")
    app_path = handler.environ.get("PATH_INFO", "")
    #
    # In proxy mode, return True for all routes so that ALL requests
    # (including SocketIO) go through the WSGI app and are forwarded
    # to the worker process. The worker handles WebSocket upgrades.
    proxy_config = context.settings.get("server", {}).get("proxy", {})
    if proxy_config.get("mode", False):
        return True
    #
    if app_path.startswith(route) or app_path == route_item:
        return False
    #
    return True
