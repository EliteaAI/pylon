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
      - Arbiter IPC transport (ZeroMQEventNode) stays thread-based: it keeps
        its proven reconnect/monitor/hmac/gzip machinery and the per-stream
        sequence-number ordering fix.  It delivers events on its own callback
        threads.
      - Gate-only asyncio node variants (AsyncStreamNode, AsyncServiceNode)
        bridge those thread-delivered events onto the event loop via loop-bound
        queues, so a request coroutine can `async for` over a response stream
        and `await` a service call WITHOUT blocking a thread per connection.
        This is what makes the hypercorn gate scale to many open connections.
      - The request path is a native ASGI handler (no WsgiToAsgi): it builds a
        WSGI environ from the ASGI scope, forwards it to the host over the
        request stream, and pumps the host's response stream straight to the
        ASGI `send` channel.
      - Socket.IO uses AsyncServer + ASGIApp for native asyncio operation.
      - A top-level ASGI router dispatches /socket.io/ to the ASGI app and
        everything else to the async WSGI bridge.
"""

#
# No monkey-patching — pure asyncio
#

import io
import sys
import signal
import argparse
import functools
import asyncio

import arbiter  # pylint: disable=E0401
import socketio  # pylint: disable=E0401
from hypercorn.asyncio import serve  # pylint: disable=E0401
from hypercorn.config import Config  # pylint: disable=E0401

from pylon.core import constants
from pylon.core.tools import log
from pylon.core.tools import log_support
from pylon.core.tools import package
from pylon.core.tools import exposure
from pylon.core.tools.context import Context
from pylon.core.tools.server import asgi as asgi_router
from pylon.framework import toolkit


def main():
    """ Entry point — called without monkey-patching """
    asyncio.run(async_main())


async def async_main():  # pylint: disable=R0914,R0915
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
    # IPC transport stays thread-based (delivers events on callback threads).
    context.event_node = arbiter.ZeroMQEventNode(
        connect_sub=args.ipc_socket_pub,
        connect_push=args.ipc_socket_pull,
        topic="pylon_ipc",
        callback_workers=None,
    )
    context.event_node.start()
    #
    # Gate-only async node variants — bridge bus events onto this loop.
    context.service_node = arbiter.AsyncServiceNode(
        context.event_node, default_timeout=15, loop=loop,
    )
    context.service_node.start()
    #
    context.stream_node = arbiter.AsyncStreamNode(
        context.event_node, id_prefix="gate:", loop=loop,
    )
    context.stream_node.start()
    #
    # Socket.IO — async variant
    context.sio = SIOGateServer(context, async_mode="asgi")
    #
    # Subscribe sio_invoke events — bridged async → event loop.
    context.event_node.subscribe("sio_invoke", context.sio.pylon_event_handler)
    # Serve sio_invoke as a service too (host uses request/response for
    # emit/call/enter_room/leave_room and expects a return value).
    context.service_node.register(context.sio.pylon_service_handler, "sio_invoke")
    #
    # Build the ASGI application tree
    #
    # 1. Native async WSGI bridge (no WsgiToAsgi).
    wsgi_bridge = functools.partial(
        asgi_request_handler,
        stream_node=context.stream_node,
        service_node=context.service_node,
    )
    #
    # 2. Socket.IO ASGI app
    sio_asgi_app = socketio.ASGIApp(
        context.sio,
        socketio_path="/",
    )
    #
    # 3. Top-level router
    context.app = asgi_router.RouterApp()
    context.app.map["/"] = wsgi_bridge
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
    except:  # pylint: disable=W0702
        log.exception("Stopping on exception")
    else:
        log.info("Stopping on event")
    finally:
        await context.sio.shutdown()
        #
        context.stream_node.stop()
        context.service_node.stop()
        context.event_node.stop()


#
# Request path
#


# WSGI environ values of these types are forwarded as-is; anything else is
# kept on the gate side and exposed to the host as a remote object proxy.
_PRIMITIVE_TYPES = (int, bool, str, bytes, tuple)

# environ keys never forwarded nor proxied.
_ENVIRON_BLACKLIST = (
    "werkzeug.socket",
)


async def _read_request_body(receive):
    """ Drain the ASGI request body into bytes """
    chunks = []
    #
    while True:
        message = await receive()
        #
        message_type = message.get("type")
        #
        if message_type == "http.disconnect":
            break
        #
        if message_type == "http.request":
            body = message.get("body", b"")
            if body:
                chunks.append(body)
            #
            if not message.get("more_body", False):
                break
        #
    return b"".join(chunks)


def _build_environ(scope, body):
    """ Build a WSGI environ from an ASGI http scope

    Mirrors asgiref.wsgi.WsgiToAsgi.build_environ so the host sees the same
    environ it would under the WsgiToAsgi bridge.
    """
    script_name = scope.get("root_path", "").encode("utf8").decode("latin1")
    path_info = scope["path"].encode("utf8").decode("latin1")
    if path_info.startswith(script_name):
        path_info = path_info[len(script_name):]
    #
    environ = {
        "REQUEST_METHOD": scope["method"],
        "SCRIPT_NAME": script_name,
        "PATH_INFO": path_info,
        "QUERY_STRING": scope["query_string"].decode("ascii"),
        "SERVER_PROTOCOL": "HTTP/%s" % scope["http_version"],
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scope.get("scheme", "http"),
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": True,
        "wsgi.run_once": False,
    }
    #
    if "server" in scope and scope["server"] is not None:
        environ["SERVER_NAME"] = scope["server"][0]
        environ["SERVER_PORT"] = str(scope["server"][1] or 80)
    else:
        environ["SERVER_NAME"] = "localhost"
        environ["SERVER_PORT"] = "80"
    #
    if scope.get("client") is not None:
        environ["REMOTE_ADDR"] = scope["client"][0]
    #
    for name, value in scope.get("headers", []):
        name = name.decode("latin1")
        if name == "content-length":
            corrected_name = "CONTENT_LENGTH"
        elif name == "content-type":
            corrected_name = "CONTENT_TYPE"
        else:
            corrected_name = "HTTP_%s" % name.upper().replace("-", "_")
        #
        value = value.decode("latin1")
        if corrected_name in environ:
            value = environ[corrected_name] + "," + value
        environ[corrected_name] = value
    #
    return environ


async def asgi_request_handler(scope, receive, send, stream_node, service_node):  # pylint: disable=R0914,R0915
    """ Native ASGI handler bridging a request to the host over streams """
    if scope["type"] == "lifespan":
        await asgi_router.lifespan_app(scope, receive, send)
        return
    #
    if scope["type"] != "http":
        # Websockets are handled by the Socket.IO app; other scope types are
        # not supported by this bridge.
        return
    #
    body = await _read_request_body(receive)
    environ = _build_environ(scope, body)
    #
    # Response stream (host emits, gate consumes) and request stream (gate
    # emits environ, host consumes).  wsgi_request_start returns the request
    # stream id — awaited, so no thread is held while the host responds.
    response_stream_id = stream_node.add_stream()
    request_stream_id = await service_node.call.wsgi_request_start(response_stream_id)
    #
    emitter = stream_node.get_emitter(request_stream_id)
    consumer = stream_node.get_consumer(response_stream_id)
    #
    # Split forwardable primitives from objects proxied back to the host.
    env = {}
    objs = []
    #
    for key, value in environ.items():
        if key in _ENVIRON_BLACKLIST:
            continue
        #
        if isinstance(value, _PRIMITIVE_TYPES):
            env[key] = value
        else:
            objs.append(key)
    #
    # Response state driven by OOB events / chunks from the host.
    state = {
        "started": False,
        "status": 200,
        "headers": [],
    }
    #
    async def _ensure_started():
        if state["started"]:
            return
        #
        state["started"] = True
        await send({
            "type": "http.response.start",
            "status": state["status"],
            "headers": state["headers"],
        })
    #
    def _parse_status(status):
        # WSGI status is "200 OK"; ASGI wants the integer code.
        try:
            return int(str(status).split(" ", 1)[0])
        except:  # pylint: disable=W0702
            return 200
    #
    def _encode_headers(headers):
        encoded = []
        for header_name, header_value in headers or []:
            if isinstance(header_name, str):
                header_name = header_name.encode("latin1")
            if isinstance(header_value, str):
                header_value = header_value.encode("latin1")
            encoded.append((header_name, header_value))
        return encoded
    #
    async def _on_start_response(tag, payload):  # pylint: disable=W0613
        # Capture status/headers; the actual http.response.start is sent
        # lazily before the first body byte (WSGI allows start_response to be
        # called again to replace an error response).
        call_args = payload.get("args", ())
        if len(call_args) >= 1:
            state["status"] = _parse_status(call_args[0])
        if len(call_args) >= 2:
            state["headers"] = _encode_headers(call_args[1])
    #
    async def _on_write(tag, payload):  # pylint: disable=W0613
        await _ensure_started()
        #
        write_args = payload.get("args", ())
        data = write_args[0] if write_args else b""
        if isinstance(data, str):
            data = data.encode("utf-8")
        #
        await send({
            "type": "http.response.body",
            "body": data,
            "more_body": True,
        })
    #
    def _on_object_call(tag, payload):  # pylint: disable=W0613
        # Host asks the gate to call one of the proxied objects (e.g.
        # wsgi.input.read()).  Reply on the REQUEST stream via a dedicated
        # 'object_call_response' OOB keyed by call_id, matching the host-side
        # AppObjectProxy contract.
        call_id = payload.get("call_id")
        #
        try:
            return_data = getattr(
                environ.get(payload["object_name"]), payload["method_name"]
            )(
                *payload["args"],
                **payload["kwargs"],
            )
            #
            emitter.oob("object_call_response", {
                "call_id": call_id,
                "return": return_data,
            })
        except BaseException as exception_data:  # pylint: disable=W0703
            emitter.oob("object_call_response", {
                "call_id": call_id,
                "raise": exception_data,
            })
    #
    consumer.register_oob_handler("start_response", _on_start_response)
    consumer.register_oob_handler("write", _on_write)
    consumer.register_oob_handler("object_call", _on_object_call)
    #
    # Forward the environ to the host.
    emitter.chunk({
        "environ": env,
        "objects": objs,
    })
    #
    try:
        async for chunk in consumer:
            await _ensure_started()
            #
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            #
            await send({
                "type": "http.response.body",
                "body": chunk,
                "more_body": True,
            })
        #
        # Stream ended cleanly — flush the terminal empty body.
        await _ensure_started()
        await send({
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        })
    except BaseException:  # pylint: disable=W0703
        log.exception("Request stream failed")
        #
        if not state["started"]:
            state["status"] = 500
            state["headers"] = [(b"content-type", b"text/plain")]
            await _ensure_started()
        #
        await send({
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        })
    finally:
        # Close the request stream so the host's pump thread stops waiting
        # (without this the host blocks on stream.get() forever).
        emitter.end()


class SIOGateServer(socketio.AsyncServer):
    """ Socket.IO async server patched for gate mode """

    def __init__(self, context, *args, **kwargs):
        self.__context = context
        self.__lock = asyncio.Lock()
        self.__loop = asyncio.get_event_loop()
        #
        super().__init__(*args, **kwargs)

    async def _handle_ack(self, eio_sid, namespace, id, data):  # pylint: disable=W0622,C0103
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
        """ Handle fire-and-forget events from the host (arbiter callback thread) """
        if event == "sio_invoke":
            method = payload.get("method")
            args = payload.get("args", [])
            kwargs = payload.get("kwargs", {})
            #
            # Bridge from arbiter's callback thread to the asyncio event loop.
            asyncio.run_coroutine_threadsafe(
                self._handle_sio_invoke(method, args, kwargs),
                self.__loop,
            )

    def pylon_service_handler(self, method, args=None, kwargs=None):
        """ Handle request/response calls from the host (arbiter callback thread)

        Runs on a service-node callback thread; the service node emits our
        return value back to the host.  Bridge to the loop and block this
        thread (not the loop) until the coroutine resolves.
        """
        args = args or []
        kwargs = kwargs or {}
        #
        future = asyncio.run_coroutine_threadsafe(
            self._handle_sio_invoke(method, args, kwargs),
            self.__loop,
        )
        #
        return future.result()

    async def _handle_sio_invoke(self, method, args, kwargs):
        """ Actual async invocation scheduled on the event loop """
        async with self.__lock:
            method_to_call = getattr(self, method)
            result = method_to_call(*args, **kwargs)
            #
            if asyncio.iscoroutine(result):
                result = await result
            #
            return result


if __name__ == "__main__":
    main()
