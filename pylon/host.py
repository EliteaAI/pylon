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
    Pylon host

    The idea is that this:
    - has native threads (non-gevent-monkey-patched)
    - runs the actual plugins
    - processes requests and events in threads (possibly limited in a pool)
    - all inbound communication is via gate, though may have some outbound communication (e.g. for plugins to access external services)
"""

import os
import sys
import argparse
import signal
import functools
import threading
import traceback

import arbiter  # pylint: disable=E0401

from pylon.core.tools import env
from pylon.core.tools import log
from pylon.core.tools import log_support
from pylon.core.tools.server import wsgi


def dump_threads_handler(signum, frame):
    """Signal handler to dump all thread stacks to stderr without limits."""
    # Write directly to stderr using a separator line
    sys.stderr.write(f"\n--- THREAD DUMP (PID {os.getpid()}) ---\n")
    
    # Extract the current execution frames for all active threads
    for thread_id, stack_frame in sys._current_frames().items():
        sys.stderr.write(f"\nStack trace for Thread ID: {thread_id}\n")
        
        # limit=None ensures the complete call stack is extracted
        # file=sys.stderr sends the trace to your standard error console
        traceback.print_stack(f=stack_frame, limit=None, file=sys.stderr)
        
    sys.stderr.write("--- END OF THREAD DUMP ---\n")
    sys.stderr.flush()


def main():
    """ Entry point """
    parser = argparse.ArgumentParser(description="Pylon host")
    parser.add_argument("--config-seed", type=str, default=env.get_var("CONFIG_SEED", None), help="Configuration seed")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--ipc-socket-pub", type=str, default="ipc:///tmp/ipc_pub.sock", help="Path to the pub IPC socket")
    parser.add_argument("--ipc-socket-pull", type=str, default="ipc:///tmp/ipc_pull.sock", help="Path to the pull IPC socket")
    args = parser.parse_args()
    #
    log_support.enable_basic_logging(force_debug=args.debug)
    #
    stop_event = threading.Event()
    #
    signal.signal(signal.SIGUSR1, dump_threads_handler)
    signal.signal(signal.SIGINT, lambda signum, frame: stop_event.set())
    signal.signal(signal.SIGTERM, lambda signum, frame: stop_event.set())
    #
    event_node = arbiter.ZeroMQEventNode(
        connect_sub=args.ipc_socket_pub,
        connect_push=args.ipc_socket_pull,
        topic="pylon_ipc",
        callback_workers=None,
    )
    event_node.start()
    #
    service_node = arbiter.ServiceNode(event_node, default_timeout=15)
    service_node.start()
    #
    stream_node = arbiter.StreamNode(event_node, id_prefix="host:")
    stream_node.start()
    #
    app = wsgi.RouterApp(
        default=wsgi.ok_app,
        # default=wsgi.debug_app,
    )
    app.map["/"] = demo_app
    #
    service_node.register(functools.partial(wsgi_request_start, stream_node=stream_node, app=app), "wsgi_request_start")
    #
    event_node.subscribe("sio_event", functools.partial(on_sio_event, node=event_node))
    event_node.subscribe("sio_ack", functools.partial(on_sio_event, node=event_node))
    #
    try:
        stop_event.wait()
    except:
        log.exception("Stopping on exception")
    else:
        log.info("Stopping on event")
    finally:
        stream_node.stop()
        service_node.stop()
        event_node.stop()


def on_sio_event(event, payload, node):
    """ Handle events from the gate """
    log.info("EVENT: event=%s, payload=%s", event, payload)
    #
    if payload["event"] == "client-message":
        node.emit(
            "sio_invoke",
            {
                "method": "emit",
                "args": ["server-message", f"Received: {payload}"],
                "kwargs": {
                    "to": payload["args"][0],
                },
            }
        )


def wsgi_request_start(output_stream_id, stream_node, app):
    input_stream_id = stream_node.add_stream()
    #
    request_thread = AppRequestThread(app, stream_node, input_stream_id, output_stream_id)
    request_thread.start()
    #
    return input_stream_id


class AppRequestThread(threading.Thread):
    def __init__(self, app, stream_node, input_stream_id, output_stream_id):
        super().__init__(daemon=True)
        #
        self.app = app
        self.stream_node = stream_node
        #
        self.input_stream_id = input_stream_id
        self.output_stream_id = output_stream_id
        #
        self.proxy_lock = threading.Lock()

    def run(self):
        emitter = self.stream_node.get_emitter(self.output_stream_id)
        consumer = self.stream_node.get_consumer(self.input_stream_id)
        iterator = iter(consumer)
        #
        environ_data = next(iterator)
        #
        environ = environ_data["environ"]
        for obj_name in environ_data["objects"]:
            environ[obj_name] = AppObjectProxy(
                obj_name, emitter, iterator, self.proxy_lock,
                blacklist=[
                    "readinto",
                    "readinto1",
                ],
            )
        #
        # log.info("Environ: %s", environ)
        #
        def _write(*args, **kwargs):
            # log.info("Write: %s, %s", args, kwargs)
            emitter.oob("write", {"args": args, "kwargs": kwargs})
        #
        def _start_response(*args, **kwargs):
            # log.info("Start response: %s, %s", args, kwargs)
            emitter.oob("start_response", {"args": args, "kwargs": kwargs})
            return _write
        #
        try:
            for chunk in self.app(environ, _start_response):
                # log.info("Chunk: %s", chunk)
                emitter.chunk(chunk)
        except BaseException as exception:  # pylint: disable=W0718
            # log.exception("Exception")
            emitter.exception(exception_info=str(exception))
        else:
            # log.info("End")
            emitter.end()


class AppObjectProxy:  # pylint: disable=R0903
    """ Remote object proxy """

    def __init__(  # pylint: disable=R0913
            self, obj_name,
            stream_emitter, consumer_iterator,
            proxy_lock,
            blacklist=None,
    ):
        self.__obj_name = obj_name
        self.__stream_emitter = stream_emitter
        self.__consumer_iterator = consumer_iterator
        self.__proxy_lock = proxy_lock
        self.__blacklist = blacklist.copy() if blacklist is not None else []
        self.__partials = {}

    def __request(self, method_name, *args, **kwargs):
        with self.__proxy_lock:
            self.__stream_emitter.oob(
                "object_call", {
                    "object_name": self.__obj_name,
                    "method_name": method_name,
                    "args": args,
                    "kwargs": kwargs,
                })
            #
            result_data = next(self.__consumer_iterator)
            #
            if "raise" in result_data:
                raise result_data.get("raise", RuntimeError())
            #
            return result_data.get("return", None)

    def __getattr__(self, name):
        # log.info("Attr: %s", name)
        #
        if name in self.__blacklist:
            raise AttributeError()
        #
        if name not in self.__partials:
            self.__partials[name] = functools.partial(self.__request, name)
        #
        return self.__partials[name]


DEMO_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Socket.IO Boilerplate</title>
    <style>
        body { font-family: sans-serif; padding: 20px; }
        #messages { border: 1px solid #ccc; height: 200px; overflow-y: auto; padding: 10px; margin-bottom: 10px; }
    </style>
</head>
<body>

    <h1>Socket.IO Live Connection</h1>
    <div id="messages"></div>
    <button id="sendBtn">Send Message to Server</button>

    <!-- Load the Socket.IO client library -->
    <script src="https://cdn.socket.io/4.8.3/socket.io.min.js" integrity="sha384-kzavj5fiMwLKzzD1f8S7TeoVIEi7uKHvbTA3ueZkrzYq75pNQUiUi6Dy98Q3fxb0" crossorigin="anonymous"></script>
    <script>
        // Connect to the Socket.IO server automatically
        const socket = io();

        const messagesDiv = document.getElementById('messages');
        const sendBtn = document.getElementById('sendBtn');

        // Confirm connection status
        socket.on('connect', () => {
            appendMessage(`Connected with ID: ${socket.id}`);
        });

        // Listen for messages emitted by the server
        socket.on('server-message', (data) => {
            appendMessage(`Server says: ${data}`);
        });

        // Emit an event when clicking the button
        sendBtn.addEventListener('click', () => {
            const payload = `Hello at ${new Date().toLocaleTimeString()}`;
            socket.emit('client-message', payload);
        });

        // Helper to display text in the DOM
        function appendMessage(text) {
            const p = document.createElement('p');
            p.textContent = text;
            messagesDiv.appendChild(p);
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
        }
    </script>
</body>
</html>
"""


def demo_app(environ, start_response):
    """ Serve demo HTML page """
    _ = environ
    #
    start_response("200 OK", [
        ("Content-type", "text/html; charset=utf-8"),
    ])
    #
    return [DEMO_HTML.encode("utf-8")]


if __name__ == "__main__":
    main()
