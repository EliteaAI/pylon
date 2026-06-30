#!/usr/bin/python3
# coding=utf-8
# pylint: disable=C0411,C0412,C0413

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
    Pylon gate (hypercorn/asyncio variant)

    This version uses hypercorn instead of gevent, enabling HTTP/2 and HTTP/3.
    No monkey-patching is performed — pure asyncio throughout.

    The architecture:
      - Arbiter IPC nodes (ZeroMQEventNode, ServiceNode, StreamNode) remain
        thread-based — they work naturally alongside asyncio.
      - The existing WSGI request bridge (wsgi_app) stays unchanged and runs
        inside hypercorn's WSGIWrapper thread-pool bridge.
      - Socket.IO uses AsyncServer + ASGIApp for native asyncio operation.
      - A top-level ASGI router (pylon.core.tools.server.asgi.RouterApp)
        dispatches /socket.io/ to the ASGI app and everything else to the
        WSGI wrapper.
"""

#
# No monkey-patching — pure asyncio
#

import sys
import types
import signal
import argparse
import functools
import asyncio

import arbiter  # pylint: disable=E0401
import socketio  # pylint: disable=E0401
import asgiref.wsgi  # pylint: disable=E0401
from hypercorn.asyncio import serve  # pylint: disable=E0401
from hypercorn.config import Config  # pylint: disable=E0401
from hypercorn.app_wrappers import WSGIWrapper  # pylint: disable=E0401

from pylon.core import constants
from pylon.core.tools import log
from pylon.core.tools import log_support
from pylon.core.tools import package
from pylon.core.tools import exposure
from pylon.core.tools.context import Context
from pylon.core.tools.server import wsgi as wsgi_router
from pylon.core.tools.server import asgi as asgi_router
from pylon.framework import toolkit


def main():
    """ Entry point — called without monkey-patching """
    asyncio.run(async_main())


async def async_main():
    """ Async entry point """
    context = Context()
    context.role = "gate"
    context.stop_event = asyncio.Event()
    #
    loop = asyncio.get_event_loop()
    #
    loop.add_signal_handler(signal.SIGINT, context.stop_event.set)
    loop.add_signal_handler(signal.SIGTERM, context.stop_event.set)
    #
    parser = argparse.ArgumentParser(description="Pylon gate (hypercorn)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--ipc-socket-pub", type=str, default="ipc:///tmp/ipc_pub.sock", help="Path to the pub IPC socket")
    parser.add_argument("--ipc-socket-pull", type=str, default="ipc:///tmp/ipc_pull.sock", help="Path to the pull IPC socket")
    parser.add_argument("--host", type=str, default=constants.SERVER_DEFAULT_HOST, help="Host to listen on")
    parser.add_argument("--http-port", type=int, default=constants.SERVER_DEFAULT_PORT, help="HTTP port to listen on")
    parser.add_argument("--http2", action="store_true", default=True, help="Enable HTTP/2")
    parser.add_argument("--http3", action="store_true", default=True, help="Enable HTTP/3 (QUIC)")
    args = parser.parse_args()
    #
    log_support.enable_basic_logging(force_debug=args.debug)
    package.collect_runtime_versions(context)
    toolkit.basic_init(context)
    #
    log.info(
        "Starting plugin-based core gate (hypercorn) — python: %s, pylon: %s, arbiter: %s",
        sys.version,
        context.pylon_version,
        context.arbiter_version,
    )
    #
    context.web_runtime = "asyncio"  # Needed for downstream components (dynamic runtime detection)
    #
    context.event_node = arbiter.ZeroMQEventNode(
        connect_sub=args.ipc_socket_pub,
        connect_push=args.ipc_socket_pull,
        topic="pylon_ipc",
        callback_workers=None,
    )
    context.event_node.start()
    #
    context.service_node = arbiter.ServiceNode(context.event_node, default_timeout=15)
    context.service_node.start()
    #
    context.stream_node = arbiter.StreamNode(context.event_node, id_prefix="gate:")
    context.stream_node.start()
    #
    # Socket.IO — async variant
    context.sio = SIOGateServer(context, async_mode="asgi")
    #
    # Subscribe sio_invoke events — pylon_event_handler bridges async → event loop
    context.event_node.subscribe("sio_invoke", context.sio.pylon_event_handler)
    #
    # Build the ASGI application tree
    #
    # 1. WSGI bridge (unchanged wsgi_app, wrapped by hypercorn's WSGIWrapper)
    wsgi_bridge = functools.partial(
        wsgi_app,
        stream_node=context.stream_node,
        service_node=context.service_node,
    )
    wrapped_wsgi = asgiref.wsgi.WsgiToAsgi(wsgi_bridge)
    #
    # 2. Socket.IO ASGI app
    sio_asgi_app = socketio.ASGIApp(
        context.sio,
        socketio_path="/",
    )
    #
    # 3. Top-level router
    context.app = asgi_router.RouterApp()
    context.app.map["/"] = wrapped_wsgi
    context.app.map["/socket.io/"] = sio_asgi_app
    #
    # Configure hypercorn
    hc_config = Config()
    hc_config.bind = [f"{args.host}:{args.http_port}"]
    # HTTP/2 is enabled by advertising h2 in ALPN
    if args.http2:
        hc_config.alpn_protocols = ["h2", "http/1.1"]
    # HTTP/3 (QUIC) requires a separate QUIC bind address
    if args.http3:
        hc_config.quic_bind = [f"{args.host}:{args.http_port}"]
    #
    # Serve
    try:
        await serve(
            context.app,
            hc_config,
            shutdown_trigger=context.stop_event.wait,
        )
    #
    except:
        log.exception("Stopping on exception")
    else:
        log.info("Stopping on event")
    finally:
        await context.sio.shutdown()
        #
        context.stream_node.stop()
        context.service_node.stop()
        context.event_node.stop()


def wsgi_app(environ, start_response, stream_node, service_node):
    """ Unchanged WSGI request bridge — runs in hypercorn's thread pool """
    response_stream_id = stream_node.add_stream()
    request_stream_id = service_node.call.wsgi_request_start(response_stream_id)
    #
    emitter = stream_node.get_emitter(request_stream_id)
    #
    env = environ.copy()
    objs = []
    #
    blacklist = [
        "werkzeug.socket",
    ]
    #
    for key in list(env):
        obj_type = type(env[key])
        #
        if key in blacklist:
            env.pop(key, None)
            continue
        #
        if obj_type not in [int, bool, str, bytes, tuple]:
            env.pop(key, None)
            objs.append(key)
            continue
    #
    consumer = stream_node.get_consumer(response_stream_id)
    #
    consumer.register_oob_handler(
        "start_response",
        lambda tag, payload: start_response(*payload["args"], **payload["kwargs"]),
    )
    #
    def _object_call(tag, payload):
        _ = tag
        #
        try:
            return_data = getattr(
                environ.get(payload["object_name"]), payload["method_name"]
            )(
                *payload["args"],
                **payload["kwargs"],
            )
            #
            emitter.chunk({
                "return": return_data,
            })
        except BaseException as exception_data:  # pylint: disable=W0703
            emitter.chunk({
                "raise": exception_data,
            })
    #
    consumer.register_oob_handler("object_call", _object_call)
    #
    emitter.chunk({
        "environ": env,
        "objects": objs,
    })
    #
    return iter(consumer)


class SIOGateServer(socketio.AsyncServer):
    """ Socket.IO async server patched for gate mode """

    def __init__(self, context, *args, **kwargs):
        self.__context = context
        self.__lock = asyncio.Lock()
        self.__loop = asyncio.get_event_loop()
        #
        super().__init__(*args, **kwargs)

    async def _handle_ack(self, eio_sid, namespace, id, data):
        namespace = namespace or "/"
        sid = self.manager.sid_from_eio_sid(eio_sid, namespace)
        #
        log.debug("ACK: eio_sid=%s, namespace=%s, sid=%s, id=%s, data=%s", eio_sid, namespace, sid, id, data)
        #
        self.__context.event_node.emit(
            "sio_ack",
            {
                "eio_sid": eio_sid,
                "namespace": namespace,
                "sid": sid,
                "id": id,
                "data": data,
            },
        )

    async def _trigger_event(self, event, namespace, *args):
        log.debug("EVENT: event=%s, namespace=%s, args=%s", event, namespace, args)
        #
        handler, args = self._get_event_handler(event, namespace, args)
        if handler is not None:
            log.debug("EVENT HANDLER: %s, %s", handler, args)
        #
        handler, args = self._get_namespace_handler(namespace, args)
        if handler is not None:
            log.debug("NAMESPACE HANDLER: %s, %s", handler, args)
        #
        if event == "connect":
            args = list(args)
            args[1] = exposure.prepare_rpc_environ(args[1])
            args = tuple(args)
        #
        self.__context.event_node.emit(
            "sio_event",
            {
                "event": event,
                "namespace": namespace,
                "args": args,
            },
        )
        #
        return self.not_handled

    def pylon_event_handler(self, event, payload):
        """ Handle events from the host — called from arbiter callback thread """
        if event == "sio_invoke":
            method = payload.get("method")
            args = payload.get("args", [])
            kwargs = payload.get("kwargs", {})
            #
            # Bridge from arbiter's callback thread to the asyncio event loop
            asyncio.run_coroutine_threadsafe(
                self._handle_sio_invoke(method, args, kwargs),
                self.__loop,
            )

    async def _handle_sio_invoke(self, method, args, kwargs):
        """ Actual async invocation scheduled on the event loop """
        async with self.__lock:
            method_to_call = getattr(self, method)
            return await method_to_call(*args, **kwargs)


if __name__ == "__main__":
    main()
