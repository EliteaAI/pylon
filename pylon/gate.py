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
    Pylon gate

    The idea is that this:
    - always runs as gevent-monkey-patched
    - runs the web server and socketio server
    - passes actual request streams and events to the host
    - forwards host responses and socketio emits to connected clients
    - able to handle a lot of open connections
"""

#
# Before all other imports and code: patch standard library and other libraries to use async I/O
#

import gevent.monkey

from build.lib.pylon.core.tools import context  # pylint: disable=E0401
gevent.monkey.patch_all()
#
import psycogreen.gevent  # pylint: disable=E0401
psycogreen.gevent.patch_psycopg()
#
import ssl
import gevent.hub  # pylint: disable=E0401
#
hub_not_errors = list(gevent.hub.Hub.NOT_ERROR)
hub_not_errors.append(ssl.SSLError)
gevent.hub.Hub.NOT_ERROR = tuple(hub_not_errors)

#
# Normal imports and code below
#

import sys
import types
import signal
import argparse
import functools

import gevent  # pylint: disable=E0401
import arbiter  # pylint: disable=E0401
import socketio  # pylint: disable=E0401
from gevent.pywsgi import WSGIServer  # pylint: disable=E0401,C0412,C0415
from geventwebsocket.handler import WebSocketHandler  # pylint: disable=E0401,C0412,C0415

from pylon.core import constants
from pylon.core.tools import log
from pylon.core.tools import log_support
from pylon.core.tools import package
from pylon.core.tools import exposure
from pylon.core.tools.context import Context
from pylon.core.tools.server import wsgi
from pylon.framework import toolkit


def main():
    """ Entry point """
    context = Context()
    context.role = "gate"
    context.stop_event = gevent.event.Event()
    #
    signal.signal(signal.SIGINT, lambda signum, frame: context.stop_event.set())
    signal.signal(signal.SIGTERM, lambda signum, frame: context.stop_event.set())
    #
    parser = argparse.ArgumentParser(description="Pylon gate")
    # parser.add_argument("--config", type=str, default="/etc/pylon/config.yaml", help="Path to the configuration file")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--ipc-socket-pub", type=str, default="ipc:///tmp/ipc_pub.sock", help="Path to the pub IPC socket")
    parser.add_argument("--ipc-socket-pull", type=str, default="ipc:///tmp/ipc_pull.sock", help="Path to the pull IPC socket")
    # parser.add_argument("--enable-protocols", type=str, default="http,https", help="Protocols to enable (comma-separated)")
    parser.add_argument("--host", type=str, default=constants.SERVER_DEFAULT_HOST, help="Host to listen on")
    parser.add_argument("--http-port", type=int, default=constants.SERVER_DEFAULT_PORT, help="HTTP port to listen on")
    # parser.add_argument("--https-port", type=int, default=8443, help="HTTPS port to listen on")
    args = parser.parse_args()
    #
    log_support.enable_basic_logging(force_debug=args.debug)
    package.collect_runtime_versions(context)
    toolkit.basic_init(context)
    #
    log.info(
        "Starting plugin-based core gate (python: %s, pylon: %s, arbiter: %s)",
        sys.version,
        context.pylon_version,
        context.arbiter_version,
    )
    #
    context.web_runtime = "gevent"  # Needed for downstream components (that are using dynamic runtime detection)
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
    context.sio = SIOGateServer(context, async_mode="gevent")
    #
    context.event_node.subscribe("sio_invoke", context.sio.pylon_event_handler)
    #
    context.app = wsgi.RouterApp()
    context.app.map["/"] = functools.partial(
        wsgi_app,
        stream_node=context.stream_node,
        service_node=context.service_node,
    )
    context.app.map["/socket.io/"] = socketio.WSGIApp(
        socketio_app=context.sio,
        socketio_path="/",
    )
    #
    context.http_server = WSGIServer(
        (
            args.host,
            args.http_port
        ),
        context.app,
        handler_class=WebSocketHandler,
    )
    #
    setattr(context.http_server, "pre_start_hook", websocket_upgrade_hook)
    #
    context.http_server.start()
    #
    try:
        context.stop_event.wait()
    #
    except:
        log.exception("Stopping on exception")
    else:
        log.info("Stopping on event")
    finally:
        context.sio.shutdown()
        context.http_server.stop()
        #
        context.stream_node.stop()
        context.service_node.stop()
        context.event_node.stop()


def wsgi_app(environ, start_response, stream_node, service_node):
    response_stream_id = stream_node.add_stream()
    request_stream_id = service_node.call.wsgi_request_start(response_stream_id)
    #
    # log.info("Request stream ID: %s", request_stream_id)
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
    # log.info("IN env: %s", env)
    #
    for key in list(env):
        obj_type = type(env[key])
        # log.info(" %s -> %s", key, obj_type)
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
    # log.info("OUT env: %s", env)
    # log.info("OUT objs: %s", objs)
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
        # log.info("Call: %s", payload)
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
            # TODO: exception wrap?
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


def websocket_upgrade_hook(handler):
    route = "/socket.io/"
    route_item = route.rstrip("/")
    #
    app_path = handler.environ.get("PATH_INFO", "")
    #
    if app_path.startswith(route) or app_path == route_item:
        return False  # Allow websocket upgrade
    #
    return True


class SIOGateServer(socketio.Server):
    """ SocketIO server patched for gate mode """

    def __init__(self, context, *args, **kwargs):
        self.__context = context
        self.__lock = gevent.lock.Semaphore(1)
        #
        super().__init__(*args, **kwargs)
 
    def _handle_ack(self, eio_sid, namespace, id, data):
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

    def _trigger_event(self, event, namespace, *args):
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
        """ Handle events from the host """
        if event == "sio_invoke":
            method = payload.get("method")
            args = payload.get("args", [])
            kwargs = payload.get("kwargs", {})
            #
            with self.__lock:
                method_to_call = getattr(self, method)
                return method_to_call(*args, **kwargs)


if __name__ == "__main__":
    main()
