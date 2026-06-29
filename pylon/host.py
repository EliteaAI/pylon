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
import uuid
import socket
import argparse
import signal
import functools
import threading
import traceback

import arbiter  # pylint: disable=E0401

from pylon.core.tools import log
from pylon.core.tools import log_support
from pylon.core.tools import db_support
from pylon.core.tools import env
from pylon.core.tools import package
from pylon.core.tools import module
from pylon.core.tools import event
from pylon.core.tools import seed
from pylon.core.tools import git
from pylon.core.tools import app
from pylon.core.tools import rpc
from pylon.core.tools import ssl
from pylon.core.tools import slot
from pylon.core.tools import server
from pylon.core.tools import external_routing
from pylon.core.tools import exposure
from pylon.core.tools import profiling
from pylon.core.tools import manager
from pylon.core.tools import process
from pylon.core.tools.context import Context
from pylon.core.tools.signal import ZombieReaper
from pylon.framework import toolkit
from pylon.framework import router


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
    context = Context()
    context.role = "host"
    context.stop_event = threading.Event()
    #
    signal.signal(signal.SIGINT, lambda signum, frame: context.stop_event.set())
    signal.signal(signal.SIGTERM, lambda signum, frame: context.stop_event.set())
    signal.signal(signal.SIGUSR1, dump_threads_handler)
    #
    parser = argparse.ArgumentParser(description="Pylon host")
    parser.add_argument("--config-seed", type=str, default=env.get_var("CONFIG_SEED", None), help="Configuration seed")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--ipc-socket-pub", type=str, default="ipc:///tmp/ipc_pub.sock", help="Path to the pub IPC socket")
    parser.add_argument("--ipc-socket-pull", type=str, default="ipc:///tmp/ipc_pull.sock", help="Path to the pull IPC socket")
    args = parser.parse_args()
    #
    log_support.enable_basic_logging(force_debug=args.debug)
    package.collect_runtime_versions(context)
    toolkit.basic_init(context)
    #
    log.info(
        "Starting plugin-based core host (python: %s, pylon: %s, arbiter: %s)",
        sys.version,
        context.pylon_version,
        context.arbiter_version,
    )
    #
    context.ipc_event_node = arbiter.ZeroMQEventNode(
        connect_sub=args.ipc_socket_pub,
        connect_push=args.ipc_socket_pull,
        topic="pylon_ipc",
        callback_workers=None,
    )
    context.ipc_event_node.start()
    #
    context.ipc_service_node = arbiter.ServiceNode(context.ipc_event_node, default_timeout=15)
    context.ipc_service_node.start()
    #
    context.ipc_stream_node = arbiter.StreamNode(context.ipc_event_node, id_prefix="host:")
    context.ipc_stream_node.start()
    #
    # Main (adapted)
    #
    # Save env-provided settings
    context.web_runtime = "host"
    context.runtime_init = env.get_var("INIT", "unknown")
    context.debug = env.get_var("DEVELOPMENT_MODE", "").lower() in ["true", "yes"] or args.debug
    # Load settings from seed
    log.info("Loading and parsing settings")
    context.settings_data, context.settings = seed.load_settings_from_seed(args.config_seed, return_data_first=True)
    if not context.settings:
        log.error("Settings are empty or invalid. Exiting")
        os._exit(1)  # pylint: disable=W0212
    # Basic init
    db_support.basic_init(context)
    # Tunable pylon settings
    seed.apply_tunable_settings(context)
    # Allow to override debug from config
    if "debug" in context.settings.get("server", {}):
        context.debug = context.settings.get("server").get("debug")
    # Save reloader status
    context.reloader_used = False
    context.before_reloader = False
    # Save global node name
    context.node_name = context.settings.get("server", {}).get("name", socket.gethostname())
    # Generate pylon ID
    context.id = f'{context.node_name}_{str(uuid.uuid4())}'
    # Set environment overrides (e.g. to add env var with data from vault)
    log.info("Setting environment overrides")
    for key, value in context.settings.get("environment", {}).items():
        os.environ[key] = value
    # Transitional: add server-related data, make root router with hook and start (if gevent)
    server.init_context(context)
    context.server_mode = "block"
    # Reinit logging with full config
    log_support.reinit_logging(context)
    # Log pylon ID
    log.info("Pylon ID: %s", context.id)
    # Set process title
    import setproctitle  # pylint: disable=C0415,E0401
    setproctitle.setproctitle(f'pylon {context.id}')
    # Initialize local data
    context.local = threading.local()
    # Enable zombie reaping
    if context.settings.get("system", {}).get("zombie_reaping", {}).get("enabled", False):
        context.zombie_reaper = ZombieReaper(context)
        context.zombie_reaper.start()
    # Disable core dump file generation
    if context.settings.get("system", {}).get("disable_core_dumps", True):
        import resource  # pylint: disable=C0415,E0401
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Prepare SSL custom cert bundle
    ssl.init(context)
    # Apply patches needed for pure-python git and providers
    git.apply_patches()
    # Save profiling settings
    context.profiling = context.settings.get("system", {}).get("profiling", {}).copy()
    #
    # Phase: core entity instances
    #
    # Make AppManager instance
    log.info("Creating AppManager instance")
    context.app_manager = app.AppManager(context)
    # Make ModuleManager instance
    log.info("Creating ModuleManager instance")
    context.module_manager = module.ModuleManager(context)
    #
    # Phase: framework
    #
    # Init framework toolkit
    toolkit.init(context)
    # Initialize DB support
    db_support.init(context)
    #
    # Phase: entity instances
    #
    # Make EventManager instance
    log.info("Creating EventManager instance")
    context.event_manager = event.EventManager(context)
    # Make RpcManager instance
    log.info("Creating RpcManager instance")
    context.rpc_manager = rpc.RpcManager(context)
    # Make SlotManager instance
    log.info("Creating SlotManager instance")
    context.slot_manager = slot.SlotManager(context)
    #
    # Phase: pylon manager
    #
    log.info("Creating Manager instance")
    context.manager = manager.Manager(context)
    #
    # Phase: A/WSGI apps
    #
    # Init app hierarchy
    context.app_manager.init_hierarchy()
    #
    # Transitional: SIO
    #
    context.sio = None
    #
    # Phase: router
    #
    # Init framework router
    router.init(context)
    #
    # Phase: modules
    #
    # Enable profiling: init
    profiling.profiling_start(context, "init")
    # Modules can clear flag in init and set it in pylon_modules_initialized handler after some async work to delay server start until they are ready
    context.can_exit_init_stage = threading.Event()
    context.can_exit_init_stage.set()
    # Load and initialize modules
    context.module_manager.init_modules()
    context.event_manager.fire_event("pylon_modules_initialized", context.id)
    # Wait for async module initialization if needed
    context.can_exit_init_stage.wait()
    # Print profile stats: init
    profiling.profiling_stop(context, "init")
    #
    # Phase: exposure
    #
    # Register external route
    external_routing.register(context)
    # Expose pylon
    context.exposure = Context()
    context.exposure.config = context.settings.get("exposure", {})
    #
    exposure.expose(context)
    #
    # Phase: operational
    #
    # Enable profiling: run
    profiling.profiling_start(context, "run")
    # Run A/WSGI server
    try:
        context.ipc_service_node.register(
            functools.partial(
                wsgi_request_start,
                stream_node=context.ipc_stream_node,
                app=context.root_router,
            ),
            "wsgi_request_start",
        )
        #
        context.ipc_event_node.subscribe(
            "sio_event",
            functools.partial(
                on_sio_event,
                context=context,
            )
        )
        context.ipc_event_node.subscribe(
            "sio_ack",
            functools.partial(
                on_sio_event,
                context=context,
            )
        )
        #
        server.run_server(context)
    except:  # pylint: disable=W0702
        log.debug("Stopping on exception:\n%s", traceback.format_exc())
    finally:
        # TODO: show splash and delay server stop
        log.info("A/WSGI server stopped")
        # Print profile stats: run
        profiling.profiling_stop(context, "run")
        # Set stop event
        context.stop_event.set()
        # Unexpose pylon
        exposure.unexpose(context)
        # Unregister external route
        external_routing.unregister(context)
        # Enable profiling: deinit
        profiling.profiling_start(context, "deinit")
        # De-init modules
        context.module_manager.deinit_modules()
        # Print profile stats: deinit
        profiling.profiling_stop(context, "deinit")
        # Leave mesh
        # Stop subpylons
        process.stop_subpylons(context)
        # De-initialize DB support
        db_support.deinit(context)
    #
    # Phase: terminate
    #
    # Flush logs here
    log.info("Exiting")
    log.flush()
    #
    context.ipc_stream_node.stop()
    context.ipc_service_node.stop()
    context.ipc_event_node.stop()
    # Exit
    log.shutdown()


def on_sio_event(event, payload, context):
    """ Handle events from the gate """
    log.info("EVENT: event=%s, payload=%s", event, payload)


def wsgi_request_start(output_stream_id, stream_node, app):
    input_stream_id = stream_node.add_stream()
    #
    # Idea: using threadpool if needed
    #
    request_thread = AppRequestThread(app, stream_node, input_stream_id, output_stream_id)
    request_thread.start()
    #
    return input_stream_id


class SIOHostProxy:
    """ Remote SIO proxy """

    def __init__(self, context):
        self.__context = context

    def emit(self, *args, **kwargs):
        self.__context.ipc_event_node.emit(
            "sio_invoke",
            {
                "method": "emit",
                "args": args,
                "kwargs": kwargs,
            }
        )


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


if __name__ == "__main__":
    main()
