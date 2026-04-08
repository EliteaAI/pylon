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
