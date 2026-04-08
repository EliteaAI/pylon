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
from pylon.core.tools import env


def run_server(context):
    """ Run A/WSGI server """
    from werkzeug.serving import run_simple  # pylint: disable=E0401,C0412,C0415
    #
    run_simple(
        hostname=context.settings.get("server", {}).get("host", constants.SERVER_DEFAULT_HOST),
        port=context.settings.get("server", {}).get("port", constants.SERVER_DEFAULT_PORT),
        application=context.root_router,
        threaded=context.settings.get("server", {}).get("threaded", True),
        use_debugger=context.debug,
        use_reloader=context.reloader_used,
        reloader_interval=context.settings.get("server", {}).get(
            "reloader_interval", int(env.get_var("RELOADER_INTERVAL", "1")),
        ),
        reloader_type=context.settings.get("server", {}).get(
            "reloader_type", env.get_var("RELOADER_TYPE", "auto"),
        ),
        **context.settings.get("server", {}).get("kwargs", {}),
    )
