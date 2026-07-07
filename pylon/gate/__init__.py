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
