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
    Gevent Worker Process

    This is the backend worker process for the gevent proxy architecture.
    
    It runs WITHOUT gevent monkey-patching, using native threads for request
    processing. It hosts the Flask app hierarchy and SocketIO event handlers,
    communicating with the proxy process via ZMQ IPC.
    
    WebSocket bridging architecture:
    
    The proxy accepts WebSocket connections locally (gevent) and forwards
    events bidirectionally with the worker via ZeroMQ EventNode.
    
    PROXY:   WS recv → EventNode emit "ws_event" → WORKER: eio._handle_ws()
    WORKER:  eio.send() → EventNode emit "ws_response_{sid}" → PROXY: WS send
    
    This allows the worker to process SocketIO events without holding open
    TCP connections, while the proxy efficiently manages 1000s of concurrent
    WebSocket connections using gevent.
    
    The worker monkey-patches engineio.Server.send() to forward responses
    via EventNode instead of writing to the transport.
"""

import io
import os
import sys
import json
import time
import uuid
import queue
import threading
import traceback

from pylon.core.tools import log


def run_worker_server(context):
    """ Run the worker server that processes requests forwarded from the proxy """
    #
    # Start the ZMQ listener that registers wsgi_call and RPC functions,
    # subscribes to WebSocket events, and monkey-patches EngineIO.
    worker = GeventWorker(context)
    worker.start()
    #
    try:
        context.stop_event.wait()
    finally:
        worker.shutdown()


class GeventWorker:
    """
    Worker process that:
    1. Handles HTTP requests via ZMQ RPC (wsgi_call)
    2. Bridges WebSocket events via ZMQ EventNode (ws_event / ws_response)
    3. Monkey-patches engineio.Server.send() to forward emits via EventNode
    """

    def __init__(self, context):
        self.context = context
        self.worker_id = f"worker_{context.id}"
        #
        proxy_config = context.settings.get("server", {}).get("proxy", {})
        #
        # ZMQ IPC configuration (must match the proxy's config)
        self.ipc_sub = proxy_config.get("ipc_sub", "ipc:///tmp/pylon_proxy_sub.ipc")
        self.ipc_push = proxy_config.get("ipc_push", "ipc:///tmp/pylon_proxy_push.ipc")
        self.rpc_timeout = proxy_config.get("rpc_timeout", 300)
        #
        # Shared RPC prefix — must match the proxy's zmq_id.
        # Passed from proxy via PYLON_PROXY_RPC_PREFIX env var to avoid UUID mismatch.
        self.rpc_prefix = os.environ.get("PYLON_PROXY_RPC_PREFIX", f"proxy_{context.node_name}")
        #
        self.event_node = None
        self.rpc_node = None
        self.started = False
        # Reference to the EngineIO server for monkey-patching
        self._eio = None

    def start(self):
        """ Start the ZMQ worker, register RPC functions, and patch EngineIO """
        if self.started:
            return
        #
        import arbiter  # pylint: disable=E0401,C0415
        #
        self.event_node = arbiter.ZeroMQEventNode(
            connect_sub=self.ipc_sub,
            connect_push=self.ipc_push,
            topic="proxy_events",
            hmac_key=None,  # No HMAC needed for local IPC
            hmac_digest="sha512",
            callback_workers=None,
            mute_first_failed_connections=0,
            log_errors=True,
        )
        self.event_node.start()
        #
        self.rpc_node = arbiter.RpcNode(
            self.event_node,
            id_prefix=f"worker_{self.context.id}_",
            trace=False,
        )
        self.rpc_node.start()
        #
        # Register RPC functions using the shared prefix so proxy can call them
        self.rpc_node.register(self._ping, name=f"{self.rpc_prefix}_ping")
        self.rpc_node.register(self._wsgi_call, name=f"{self.rpc_prefix}_wsgi_call")
        #
        # Apply EngineIO monkey-patch so socketio.emit() sends via EventNode
        self._patch_engineio_send()
        #
        # Subscribe to WebSocket events from proxy
        self.event_node.subscribe("ws_open", self._on_ws_open)
        self.event_node.subscribe("ws_event", self._on_ws_event)
        self.event_node.subscribe("ws_close", self._on_ws_close)
        #
        self.started = True
        log.info("Worker registered RPC functions and is ready")

    def shutdown(self):
        """ Stop the ZMQ worker """
        if self.rpc_node is not None:
            self.rpc_node.stop()
        #
        if self.event_node is not None:
            self.event_node.stop()
        #
        self.started = False

    def _patch_engineio_send(self):
        """
        Monkey-patch engineio.Server.send() to forward responses via EventNode
        instead of writing to the transport.
        
        This allows SocketIO event handlers in the worker to call sio.emit(),
        which goes through EngineIO send(), and have the data sent to the
        correct WebSocket connection in the proxy.
        """
        import engineio  # pylint: disable=E0401,C0415
        #
        # Store original send method
        self._original_eio_send = engineio.Server.send
        #
        # Reference to our event_node for use in the patched method
        _self = self
        #
        def _patched_send(eio_self, sid, data):
            """ Forward EngineIO send() via EventNode to proxy """
            en = getattr(eio_self, '_pylon_event_node', None)
            if en is not None:
                en.emit(f"ws_response_{sid}", {"data": data})
            else:
                # Fallback to original send
                _self._original_eio_send(eio_self, sid, data)
        #
        engineio.Server.send = _patched_send
        #
        # If context.sio already exists, attach our event_node to its eio
        if self.context.sio is not None:
            self._eio = self.context.sio.eio
            self._eio._pylon_event_node = self.event_node

    def _on_ws_open(self, event_name, payload):
        """
        Handle WebSocket connection open event from proxy.
        
        Creates a virtual EngineIO socket entry so that subsequent
        ws_event messages can be processed through EngineIO's _handle_ws().
        """
        sid = payload.get("sid")
        environ = payload.get("environ", {})
        #
        if self.context.sio is None:
            return
        #
        eio = self.context.sio.eio
        #
        # Create a virtual EngineIO socket entry
        # _get_socket(sid) creates a Socket if it doesn't exist
        sock = eio._get_socket(sid)  # pylint: disable=W0212
        #
        # Set transport to websocket
        sock.transport = "websocket"
        #
        # Attach our event_node so _patched_send can find it
        eio._pylon_event_node = self.event_node

    def _on_ws_event(self, event_name, payload):
        """
        Handle WebSocket message event from proxy.
        
        Processes the raw EngineIO frame text through EngineIO's _handle_ws(),
        which triggers the SocketIO event handler chain.
        Any emits from handlers are captured by our monkey-patched send().
        """
        sid = payload.get("sid")
        data = payload.get("data")
        #
        if self.context.sio is None or sid is None or data is None:
            return
        #
        eio = self.context.sio.eio
        #
        # Ensure event_node is attached
        eio._pylon_event_node = self.event_node
        #
        # Process the raw EngineIO frame through EngineIO's WebSocket handler.
        # This parses the frame, dispatches to SocketIO, and runs event handlers.
        try:
            eio._handle_ws(sid, data)  # pylint: disable=W0212
        except Exception:  # pylint: disable=W0703
            log.exception("Worker WS event processing error")

    def _on_ws_close(self, event_name, payload):
        """
        Handle WebSocket connection close event from proxy.
        
        Removes the virtual EngineIO socket entry.
        """
        sid = payload.get("sid")
        #
        if self.context.sio is None:
            return
        #
        eio = self.context.sio.eio
        #
        # Remove the virtual socket
        if sid in eio.sockets:
            del eio.sockets[sid]

    def _ping(self):
        """ Respond to proxy health checks """
        return True

    def _wsgi_call(self, environ):
        """
        Process a WSGI request forwarded from the proxy.
        
        This function is called by the proxy via RPC. It reconstructs the WSGI
        environ, calls the Flask app hierarchy, and returns the response.
        """
        response = {
            "status": None,
            "headers": None,
            "body": io.BytesIO(),
        }
        #
        def start_response(status, headers):
            """ WSGI: start_response """
            response["status"] = status
            response["headers"] = headers
        #
        # Reconstruct the environ with proper wsgi.input
        call_environ = dict(environ)
        call_environ["wsgi.errors"] = sys.stderr
        call_environ["wsgi.input"] = io.BytesIO(
            environ.get("wsgi.input", b"")
        )
        #
        # Apply URL prefix if needed
        app_path = call_environ.get("PATH_INFO", "")
        if self.context.url_prefix and app_path.startswith(self.context.url_prefix):
            app_path = app_path[len(self.context.url_prefix):]
            call_environ["PATH_INFO"] = app_path
            call_environ["SCRIPT_NAME"] = self.context.url_prefix
        #
        data = None
        #
        # Use root_router (not app_router_wsgi) because it has both the app_router
        # route AND the SocketIO route registered. app_router_wsgi only has Flask routes.
        try:
            data = self.context.root_router(
                call_environ, start_response,
            )
            for item in data:
                response["body"].write(item)
        except Exception:  # pylint: disable=W0703
            log.exception("Worker WSGI call error")
            if response["status"] is None:
                start_response("500 Internal Server Error", [
                    ("Content-type", "text/plain"),
                ])
            response["body"].write(
                traceback.format_exc().encode("utf-8")
            )
        finally:
            if data is not None and hasattr(data, "close"):
                try:
                    data.close()
                except Exception:  # pylint: disable=W0703
                    pass
        #
        response["body"] = response["body"].getvalue()
        return response
        if event_name == "connect":
            # For connect events, reconstruct the WSGI environ
            call_environ = dict(args[1])
            call_environ["wsgi.errors"] = sys.stderr
            call_environ["wsgi.input"] = io.BytesIO(
                args[1].get("wsgi.input", b"")
            )
            #
            app_path = call_environ.get("PATH_INFO", "")
            if self.context.url_prefix and app_path.startswith(self.context.url_prefix):
                app_path = app_path[len(self.context.url_prefix):]
                call_environ["PATH_INFO"] = app_path
                call_environ["SCRIPT_NAME"] = self.context.url_prefix
            #
            args = list(args)
            args[1] = call_environ
            args = tuple(args)
        #
        try:
            self.context.sio.pylon_trigger_event(event_name, namespace, *args)
        except Exception:  # pylint: disable=W0703
            if not self.context.is_async:
                log.exception("Failed to trigger SIO worker event")
