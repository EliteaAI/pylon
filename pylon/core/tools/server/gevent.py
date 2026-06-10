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

from pylon.core import constants


def run_server(context):
    """ Run A/WSGI server """
    #
    # Detect worker by runtime: gevent_worker means this is the backend worker process
    # This must be checked BEFORE proxy_mode, because the worker also has proxy_mode=True
    # in its config (same settings file) but must NOT start an HTTP server.
    is_worker = context.web_runtime == "gevent_worker"
    #
    if is_worker:
        # Worker mode: this process handles app logic, proxy handles I/O
        from .gevent_worker import run_worker_server  # pylint: disable=C0415
        run_worker_server(context)
        return
    #
    # Check if we are in proxy mode (split architecture)
    proxy_config = context.settings.get("server", {}).get("proxy", {})
    proxy_mode = proxy_config.get("mode", False)
    #
    if proxy_mode:
        # Proxy mode: this process handles I/O, worker handles app logic
        from .gevent_proxy import run_proxy_server  # pylint: disable=C0415
        run_proxy_server(context)
    else:
        # Traditional mode: everything in one process
        from gevent.greenlet import Greenlet  # pylint: disable=E0401,C0412,C0415
        from .splash import boot_splash_hook  # pylint: disable=C0415
        #
        if boot_splash_hook in context.root_router.hooks:
            context.root_router.hooks.remove(boot_splash_hook)
        #
        try:
            context.stop_event.wait()
        finally:
            Greenlet.spawn(context.http_server.stop, timeout=None).join()


def make_server(context):
    """ Make WSGI server """
    #
    # Detect worker by runtime first
    is_worker = context.web_runtime == "gevent_worker"
    if is_worker:
        return None
    #
    # Check if we are in proxy mode
    proxy_config = context.settings.get("server", {}).get("proxy", {})
    proxy_mode = proxy_config.get("mode", False)
    #
    if proxy_mode:
        from .gevent_proxy import make_proxy_server  # pylint: disable=C0415
        return make_proxy_server(context)
    #
    # Traditional mode
    from gevent.pywsgi import WSGIServer  # pylint: disable=E0401,C0412,C0415
    from geventwebsocket.handler import WebSocketHandler  # pylint: disable=E0401,C0412,C0415
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


def _http_server_pre_start_hook(handler):
    from tools import context  # pylint: disable=E0401,C0415
    #
    try:
        route = context.socketio_route
    except:  # pylint: disable=W0702
        return True  # pylon has not initialized SocketIO yet
    #
    route_item = route.rstrip("/")
    #
    app_path = handler.environ.get("PATH_INFO", "")
    #
    if app_path.startswith(route) or app_path == route_item:
        return False
    #
    return True
