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
    import waitress  # pylint: disable=E0401,C0412,C0415
    waitress.serve(
        context.root_router,
        host=context.settings.get("server", {}).get("host", constants.SERVER_DEFAULT_HOST),
        port=context.settings.get("server", {}).get("port", constants.SERVER_DEFAULT_PORT),
        threads=context.settings.get("server", {}).get(
            "threads", constants.SERVER_DEFAULT_THREADS
        ),
        connection_limit=context.settings.get("server", {}).get(
            "connections", constants.SERVER_DEFAULT_CONNECTIONS
        ),
        clear_untrusted_proxy_headers=False,
        ident="Pylon",
        **context.settings.get("server", {}).get("kwargs", {}),
    )


class WaitressSocket:  # pylint: disable=R0903
    """ Get socket from waitress channel """

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        channel = None
        #
        if "waitress.client_disconnected" in environ:
            channel = environ["waitress.client_disconnected"].__self__
        #
        if channel is not None:
            environ["werkzeug.socket"] = WaitressSocketWrapper(channel)
        #
        return self.app(environ, start_response)


class WaitressSocketWrapper:  # pylint: disable=R0903
    """ Get socket from waitress channel: wrapper """

    def __init__(self, channel):
        self.channel = channel
        self.socket = None

    def __getattr__(self, name):
        if self.socket is None:
            self.socket = self.channel.socket
            #
            self.channel.socket = None
            self.channel.del_channel()
            self.channel.cancel()
            #
            self.socket.setblocking(1)
        #
        return getattr(self.socket, name)
