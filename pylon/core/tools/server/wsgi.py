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

from pylon.core.tools import log


def noop_app(environ, start_response):
    """ Dummy app that always returns 404 """
    _ = environ
    #
    start_response("404 Not Found", [
        ("Content-type", "text/plain")
    ])
    #
    return [b"Not Found\n"]


def ok_app(environ, start_response):
    """ Dummy app that always returns 200 """
    _ = environ
    #
    start_response("200 OK", [
        ("Content-type", "text/plain")
    ])
    #
    return [b"OK\n"]


def debug_app(environ, start_response):
    """ Dummy debug app """
    log.debug("WSGI environ: %s", environ)
    return noop_app(environ, start_response)


class RouterApp:  # pylint: disable=R0903
    """ App router """

    def __init__(self, app_map=None, default=noop_app, update_path=True):
        self.map = app_map.copy() if app_map is not None else {}
        self.default = default
        self.update_path = update_path
        self.hooks = []

    def __call__(self, environ, start_response):
        for hook in list(self.hooks):
            try:
                hook_app = hook(self, environ, start_response)
            except:  # pylint: disable=W0702
                hook_app = None
            #
            if hook_app is not None:
                return hook_app(environ, start_response)
        #
        root_path = environ.get("SCRIPT_NAME", "")
        app_path = environ.get("PATH_INFO", "")
        #
        target_app = self.default
        #
        routes = list(self.map.keys())
        routes.sort(key=len, reverse=True)
        #
        for route in routes:
            route_item = route.rstrip("/")
            #
            if route.endswith("/"):
                root_path_addon = route_item
            elif "/" in route_item:
                root_path_addon = route_item.rsplit("/", 1)[0]
            else:
                root_path_addon = ""
            #
            if app_path.startswith(route) or app_path == route_item:
                target_app = self.map.get(route, self.default)
                #
                if self.update_path:
                    root_path = root_path.rstrip("/") + root_path_addon
                    app_path = app_path[len(root_path_addon):]
                #
                break
        #
        environ["SCRIPT_NAME"] = root_path
        environ["PATH_INFO"] = app_path
        #
        return target_app(environ, start_response)
