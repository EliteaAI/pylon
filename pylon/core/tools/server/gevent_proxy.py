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
    
    WebSocket bridging:
    The proxy accepts WebSocket connections locally (gevent), then bridges events
    bidirectionally with the worker via ZeroMQ EventNode:
    
    PROXY:   WS recv → EventNode emit → WORKER: eio._handle_ws() → SocketIO handlers
    WORKER:  eio.send() → EventNode emit → PROXY:  WS send
    
    This allows the worker to process SocketIO events without holding open TCP
    connections, while the proxy efficiently manages 1000s of concurrent WS connections.
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
    #
    # Build the proxy WSGI app
    proxy_config = context.settings.get("server", {}).get("proxy", {})
    proxy_app = GeventProxyApp(context, proxy_config)
    #
    # Set the proxy app as the root router directly (no ProxyRouterWrapper wrapper).
    # In proxy mode, ALL requests go through GeventProxyApp which handles
    # WebSocket upgrades, health endpoints, and forwards everything to worker.
    context.root_router = proxy_app
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
    #
    proxy_config = context.settings.get("server", {}).get("proxy", {})
    proxy_app = GeventProxyApp(context, proxy_config)
    #
    # Store the proxy app on context so _proxy_main_loop can start its ZMQ connection
    context.proxy_app = proxy_app
    #
    # Set the proxy app as the root router directly
    context.root_router = proxy_app
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
        # Registry of active WebSocket bridges: sid → WebSocketBridge
        self._ws_bridges = {}

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
            callback_workers=None,
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
        WSGI call: forward the request to the worker via ZMQ RPC,
        OR handle WebSocket upgrade locally and bridge to worker.
        
        WebSocket upgrades are detected by HTTP_UPGRADE header.
        We create a WebSocket from the raw socket (stored in werkzeug.socket
        by _http_server_pre_start_hook) and bridge to the worker via EventNode.
        
        Health endpoints (/healthz/, /livez/, /readyz/) are handled locally.
        """
        if not self.started:
            return self._no_worker(environ, start_response)
        #
        # Handle health endpoints locally (no worker needed)
        app_path = environ.get("PATH_INFO", "")
        for endpoint in ["/healthz/", "/livez/", "/readyz/"]:
            endpoint_stripped = endpoint.rstrip("/")
            if app_path.startswith(endpoint) or app_path == endpoint_stripped:
                start_response("200 OK", [
                    ("Content-type", "text/plain"),
                    ("Cache-Control", "no-store"),
                ])
                return [b"OK\n"]
        #
        # Detect WebSocket upgrade by HTTP_UPGRADE header
        upgrade = environ.get("HTTP_UPGRADE", "").lower()
        if upgrade == "websocket":
            # Create WebSocket from raw socket and bridge to worker
            return self._handle_websocket_from_sock(environ, start_response)
        #
        # Normal HTTP request — forward to worker via RPC
        return self._handle_http(environ, start_response)

    def _handle_http(self, environ, start_response):
        """ Forward a normal HTTP request to the worker via ZMQ RPC """
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

    def _handle_websocket_from_sock(self, environ, start_response):
        """
        Accept a WebSocket upgrade request by creating a WebSocket from the
        raw TCP socket using simple_websocket.Server (available in the container
        as a dependency of python-socketio).
        
        The proxy keeps the WS connection open and bridges events to/from the
        worker via EventNode.
        """
        import simple_websocket  # pylint: disable=E0401,C0415
        #
        ws = simple_websocket.Server(environ)
        sid = self._start_ws_bridge(ws, environ)
        #
        start_response("101 Switching Protocols", [
            ("Upgrade", "websocket"),
            ("Connection", "upgrade"),
        ])
        return []

    def _start_ws_bridge(self, ws_sock, environ):
        """ Start a WebSocket bridge: common logic for both WS upgrade types """
        sid = str(uuid.uuid4())
        #
        bridge = WebSocketBridge(self, ws_sock, sid, environ)
        self._ws_bridges[sid] = bridge
        #
        # Subscribe for responses from worker for this SID
        self.event_node.subscribe(f"ws_response_{sid}", bridge._on_worker_response)
        #
        # Start the bridge reader (reads WS → emits to EventNode)
        from gevent.greenlet import Greenlet  # pylint: disable=E0401,C0412,C0415
        Greenlet(bridge._reader).start()
        #
        # Send the initial environ to worker so it can set up the SocketIO socket
        self.event_node.emit("ws_open", {
            "sid": sid,
            "environ": self._prepare_environ(environ),
        })
        #
        return sid

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


class WebSocketBridge:
    """
    Bridges a single WebSocket connection between proxy and worker.
    
    Reader greenlet: reads messages from WS → emits to EventNode as "ws_event"
    Response listener: receives "ws_response_{sid}" from EventNode → writes to WS
    
    The worker processes each event through its EngineIO+SocketIO stack
    and sends responses back via EventNode.
    """

    def __init__(self, proxy_app, ws_sock, sid, environ):
        self.proxy_app = proxy_app
        self.ws_sock = ws_sock
        self.sid = sid
        self.environ = environ
        self._closed = False

    def _reader(self):
        """ Greenlet: read messages from WebSocket and forward to worker """
        try:
            while not self._closed:
                msg = self.ws_sock.receive()
                if msg is None:
                    break
                # Forward the raw EngineIO data to worker
                self.proxy_app.event_node.emit("ws_event", {
                    "sid": self.sid,
                    "data": msg,
                })
        except Exception:  # pylint: disable=W0703
            pass
        finally:
            self._close()

    def _on_worker_response(self, event_name, payload):
        """ Called when worker sends a response for this SID """
        if self._closed:
            return
        try:
            data = payload.get("data")
            self.ws_sock.send(data)
        except Exception:  # pylint: disable=W0703
            self._close()

    def _close(self):
        """ Close the bridge """
        if self._closed:
            return
        self._closed = True
        # Notify worker that connection is closed
        self.proxy_app.event_node.emit("ws_close", {
            "sid": self.sid,
        })
        # Unsubscribe from response topic
        self.proxy_app.event_node.unsubscribe(
            f"ws_response_{self.sid}", self._on_worker_response
        )
        # Remove from registry
        self.proxy_app._ws_bridges.pop(self.sid, None)


def _http_server_pre_start_hook(handler):
    """ In proxy mode, always return True so ALL requests reach the WSGI app.
    Also store the raw socket in werkzeug.socket so that simple_websocket.Server
    (used by Engine.IO) can create a WebSocket from it for transport=websocket
    upgrade requests. """
    handler.environ['werkzeug.socket'] = handler.socket
    return True
