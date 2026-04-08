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

""" Server """

import os
import signal
import logging
import importlib
import subprocess

from pylon.core.tools import log


def init_context(context):
    """ Add server-related data to context """
    context.server_mode = context.settings.get("server", {}).get("mode", "web")
    #
    context.url_prefix = context.settings.get("server", {}).get("path", "/")
    while context.url_prefix.endswith("/"):
        context.url_prefix = context.url_prefix[:-1]
    #
    context.is_async = context.web_runtime in ["hypercorn"]
    if context.is_async:
        # Mute "Task exception was never retrieved"
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    #
    context.server_log_filter = log.Filter()
    logging.getLogger("server").addFilter(context.server_log_filter)
    logging.getLogger("werkzeug").addFilter(context.server_log_filter)
    logging.getLogger("geventwebsocket.handler").addFilter(context.server_log_filter)
    #
    # Root router
    #
    if context.is_async:
        from .asgi import RouterApp  # pylint: disable=C0415
    else:
        from .wsgi import RouterApp  # pylint: disable=C0415
    #
    context.root_router = RouterApp()
    #
    # Health endpoints
    #
    health_config = context.settings.get("server", {}).get("health", {})
    health_log = health_config.get("log", False)
    #
    if health_log and context.is_async:
        log.warning("Health endpoint logs are not supported with %s", context.web_runtime)
    #
    if context.is_async:
        from .asgi import ok_app  # pylint: disable=C0415
    else:
        from .wsgi import ok_app  # pylint: disable=C0415
    #
    for endpoint in ["healthz", "livez", "readyz"]:
        if health_config.get(endpoint, False):
            log.info("Adding %s endpoint", endpoint)
            context.root_router.map[f"/{endpoint}/"] = ok_app
            #
            if not health_log:
                context.server_log_filter.filter_strings.append(f"GET /{endpoint}")
    #
    if context.web_runtime == "gevent" and context.server_mode == "web":
        early_run_server(context)


def early_run_server(context):
    """ Run server early with health endpoints and 'boot splash' hook """
    from .gevent import make_server  # pylint: disable=C0415
    from .splash import boot_splash_hook  # pylint: disable=C0415
    #
    context.root_router.hooks.append(boot_splash_hook)
    #
    context.http_server = make_server(context)
    context.http_server.start()


def run_server(context):
    """ Run A/WSGI server """
    if context.server_mode == "web":
        log.info("Starting %s server", context.web_runtime)
        #
        runtime = importlib.import_module(f"pylon.core.tools.server.{context.web_runtime}")
        runtime.run_server(context)
    elif context.server_mode == "block":
        log.info("Blocking until stop_event is set")
        #
        context.stop_event.wait()
    else:  # oneshot / preload / etc
        return


def restart():
    """ Stop server (will be restarted by docker/runtime) """
    from tools import context  # pylint: disable = E0401,C0415
    #
    restart_method = context.settings.get("server", {}).get("restart_method", None)
    if restart_method is None:
        if context.web_runtime == "gevent":
            restart_method = "stop_event"
        else:
            restart_method = "subprocess"
    #
    pylon_pid = os.getpid()
    if context.runtime_init == "dumb-init":
        pylon_pid = 1
    #
    log.info("Stopping for a restart (pid = %s, method = %s)", pylon_pid, restart_method)
    #
    if restart_method == "stop_event":
        context.stop_event.set()
    elif restart_method == "subprocess":
        subprocess.Popen(  # pylint: disable=R1732
            ["/bin/bash", "-c", f"bash -c 'sleep 1; kill {pylon_pid}' &"]
        )
    else:  # signal
        if context.web_runtime == "gevent":
            context.stop_event.set()  # Act as in stop_event
        else:
            os.kill(pylon_pid, signal.SIGTERM)
