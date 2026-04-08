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


async def lifespan_app(scope, receive, send):
    """ Dummy no-op lifespan app """
    _ = scope
    #
    while True:
        message = await receive()
        #
        if message["type"] == "lifespan.startup":
            await send({
                "type": "lifespan.startup.complete",
            })
        #
        elif message["type"] == "lifespan.shutdown":
            await send({
                "type": "lifespan.shutdown.complete",
            })
            return


async def noop_app(scope, receive, send):
    """ Dummy app that always returns 404 """
    if scope["type"] == "lifespan":
        await lifespan_app(scope, receive, send)
        return
    #
    await send({
        "type": "http.response.start",
        "status": 404,
        "headers": [
            (b"Content-type", b"text/plain"),
        ],
    })
    #
    await send({
        "type": "http.response.body",
        "body": b"Not Found\n",
    })


async def ok_app(scope, receive, send):
    """ Dummy app that always returns 200 """
    if scope["type"] == "lifespan":
        await lifespan_app(scope, receive, send)
        return
    #
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            (b"Content-type", b"text/plain"),
        ],
    })
    #
    await send({
        "type": "http.response.body",
        "body": b"OK\n",
    })


async def debug_app(scope, receive, send):
    """ Dummy debug app """
    log.debug("ASGI scope: %s", scope)
    await noop_app(scope, receive, send)


class RouterApp:  # pylint: disable=R0903
    """ App router """

    def __init__(self, app_map=None, default=noop_app, update_path=True):
        self.map = app_map.copy() if app_map is not None else {}
        self.default = default
        self.update_path = update_path

    async def __call__(self, scope, receive, send):
        if "path" not in scope:
            await self.default(scope, receive, send)
            return
        #
        root_path = scope.get("root_path", "")
        app_path = scope["path"][len(root_path):]
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
                #
                break
        #
        scope["root_path"] = root_path
        #
        await target_app(scope, receive, send)
