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

""" Pylon gate """

from pylon.core.tools import log
from pylon.core.tools import env
from pylon.core.tools import seed


def load_socketio_config():
    """ Load the socketio config from the config seed (best-effort)

    The gate runs as its own subpylon and does not go through the full host
    init, so it loads just the settings it needs.  Returns the "socketio"
    section of the parsed settings, or {} if unavailable.
    """
    config_seed = env.get_var("CONFIG_SEED", None)
    #
    try:
        _, settings = seed.load_settings_from_seed(config_seed, return_data_first=True)
        if settings:
            return settings.get("socketio", {}) or {}
    except:  # pylint: disable=W0702
        log.exception("Failed to load socketio config from seed, using defaults")
    #
    return {}


def build_sio_kwargs(socketio_config):
    """ Build common SIO server kwargs (CORS + selected passthroughs)

    Mirrors pylon.core.tools.server.socketio.create_socketio_instance so the
    gate's Socket.IO server enforces the same CORS policy as the host would.
    """
    sio_kwargs = {
        "cors_allowed_origins": socketio_config.get("cors_allowed_origins", "*"),
    }
    #
    for arg_item in ["transports"]:
        if arg_item in socketio_config:
            sio_kwargs[arg_item] = socketio_config[arg_item]
    #
    return sio_kwargs
