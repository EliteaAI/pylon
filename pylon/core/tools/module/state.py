#!/usr/bin/python
# coding=utf-8
# pylint: disable=C0302

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

""" Modules """

import json


def exists(plugin):
    """ State: check """
    from tools import context  # pylint: disable=C0415,E0401
    from pylon.framework.db.models.plugin_state import PluginState  # pylint: disable=C0415
    #
    with context.pylon_db.make_session() as db_session:
        state_obj = db_session.query(PluginState).get(plugin)
        #
        if state_obj is None:
            return False
        #
        return True


def get(plugin, default={}):
    """ State: get """
    from tools import context  # pylint: disable=C0415,E0401
    from pylon.framework.db.models.plugin_state import PluginState  # pylint: disable=C0415
    #
    with context.pylon_db.make_session() as db_session:
        state_obj = db_session.query(PluginState).get(plugin)
        #
        if state_obj is None:
            return default.copy()
        #
        return json.loads(state_obj.state)


def set(plugin, value):
    """ State: set """
    from tools import context  # pylint: disable=C0415,E0401
    from pylon.framework.db.models.plugin_state import PluginState  # pylint: disable=C0415
    #
    with context.pylon_db.make_session() as db_session, db_session.begin():
        state_obj = db_session.query(PluginState).get(plugin)
        value_bin = json.dumps(value).encode()
        #
        if state_obj is None:
            state_obj = PluginState(
                plugin=plugin,
                state=value_bin,
            )
            #
            db_session.add(state_obj)
        else:
            state_obj.state = value_bin
        #
        return None


def delete(plugin):
    """ State: delete """
    from tools import context  # pylint: disable=C0415,E0401
    from pylon.framework.db.models.plugin_state import PluginState  # pylint: disable=C0415
    #
    with context.pylon_db.make_session() as db_session, db_session.begin():
        state_obj = db_session.query(PluginState).get(plugin)
        #
        if state_obj is not None:
            db_session.delete(state_obj)
        #
        return None
