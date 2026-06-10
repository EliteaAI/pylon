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
    
    The worker registers wsgi_call and sio_call RPC functions that the proxy
    calls for each incoming HTTP request or SocketIO event.
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
    # Start the ZMQ listener that registers wsgi_call and sio_call RPC functions
    worker = GeventWorker(context)
    worker.start()
    #
    try:
        context.stop_event.wait()
    finally:
        worker.shutdown()


class GeventWorker:
    """
    Worker process that registers WSGI and SocketIO call handlers via ZMQ RPC.
    
    The proxy process connects to this worker via a ZeroMQ EventNode and calls
    the registered functions for each incoming request. This worker uses native
    threads (not gevent) so CPU-bound tasks can utilize multiple cores.
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

    def start(self):
        """ Start the ZMQ worker and register RPC functions """
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
            callback_workers=1,
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
        self.rpc_node.register(self._sio_call, name=f"{self.rpc_prefix}_sio_call")
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
        try:
            data = self.context.app_router_wsgi(
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

    def _sio_call(self, event_name, namespace, args):
        """
        Process a SocketIO event forwarded from the proxy.
        
        This function is called by the proxy via RPC when a SocketIO event
        arrives. It triggers the registered SocketIO handlers.
        """
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
