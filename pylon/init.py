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
    Pylon entry

    The idea is that this:
    - will run as first container / runtime process (replacing dumb-init)
    - parse the config
    - launch and supervise needed subprocesses or exec into such (e.g. for preload mode)
    - allow for intelligent restarts and runtime updates
"""

import os
import sys
import signal
import argparse
import threading

import arbiter  # pylint: disable=E0401

from pylon.core.tools import log
from pylon.core.tools import log_support
from pylon.core.tools import exposure
from pylon.core.tools import package
from pylon.core.tools import env
from pylon.core.tools import seed
from pylon.core.tools import db_support
from pylon.core.tools import process
from pylon.core.tools.context import Context
from pylon.core.tools.signal import kill_remaining_processes
from pylon.framework import toolkit


def main():
    """ Entry point """
    context = Context()
    context.role = "init"
    context.stop_event = threading.Event()
    #
    signal.signal(signal.SIGINT, lambda signum, frame: context.stop_event.set())
    signal.signal(signal.SIGTERM, lambda signum, frame: context.stop_event.set())
    #
    parser = argparse.ArgumentParser(description="Pylon init")
    parser.add_argument("--config-seed", type=str, default=env.get_var("CONFIG_SEED", None), help="Configuration seed")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    #
    log_support.enable_basic_logging(force_debug=args.debug)
    package.collect_runtime_versions(context)
    toolkit.basic_init(context)
    #
    log.info(
        "Starting plugin-based core init (python: %s, pylon: %s, arbiter: %s)",
        sys.version,
        context.pylon_version,
        context.arbiter_version,
    )
    #
    log.info("Loading and parsing settings")
    context.settings_data, context.settings = seed.load_settings_from_seed(args.config_seed, return_data_first=True)
    if not context.settings:
        log.error("Settings are empty or invalid. Exiting")
        os._exit(1)  # pylint: disable=W0212
    #
    db_support.basic_init(context)
    seed.apply_tunable_settings(context)
    db_support.basic_deinit(context)
    #
    context.runtime_init = env.get_var("INIT", "unknown")
    context.reloader_used = False
    context.before_reloader = False
    #
    exposure.expose_zmq(context)
    #
    context.ipc_zmq_server = arbiter.ZeroMQServerNode(
        bind_pub="ipc:///tmp/ipc_pub.sock",
        bind_pull="ipc:///tmp/ipc_pull.sock",
    )
    context.ipc_zmq_server.start()
    #
    context.server_mode = "init"
    #
    context.gate_subpylon = process.SubpylonInstance(context, {
        "name": "gate",
        "command": [sys.executable, "-m", "pylon.gate_hypercorn"],
    })
    context.gate_subpylon.start()
    #
    context.host_subpylon = process.SubpylonInstance(context, {
        "name": "host",
        "command": [sys.executable, "-m", "pylon.host"],
    })
    context.host_subpylon.start()
    #
    try:
        context.stop_event.wait()
    except:
        log.exception("Stopping on exception")
    else:
        log.info("Stopping on event")
    finally:
        context.host_subpylon.stop()
        context.gate_subpylon.stop()
        context.ipc_zmq_server.stop()
        #
        exposure.unexpose_zmq(context)
    #
    if context.settings.get("system", {}).get("kill_remaining_processes", True) and \
            context.runtime_init in ["pylon", "dumb-init"]:
        kill_remaining_processes(context)


if __name__ == "__main__":
    main()
