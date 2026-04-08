#!/usr/bin/python
# coding=utf-8
# pylint: disable=I0011,E0401

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

"""
    Configuration tools
"""

import os
import re
import logging

import hvac  # pylint: disable=E0401


env_var_pattern_str = re.compile(
    r"""
    \$(
        # ${VAR} or ${VAR:default}
        \{
            (?P<brace_name>[A-Za-z_][A-Za-z0-9_]*)
            (?::(?P<default>[^}]*))?
        \}
        |
        # $VAR
        (?P<simple_name>[A-Za-z_][A-Za-z0-9_]*)
    )
    """,
    re.VERBOSE,
)


env_var_pattern_bytes = re.compile(
    br"""
    \$(
        # ${VAR} or ${VAR:default}
        \{
            (?P<brace_name>[A-Za-z_][A-Za-z0-9_]*)
            (?::(?P<default>[^}]*))?
        \}
        |
        # $VAR
        (?P<simple_name>[A-Za-z_][A-Za-z0-9_]*)
    )
    """,
    re.VERBOSE,
)


def env_vars_expansion(data, env_vars=None):
    """ Expand vars in data with optional defaults """
    #
    if isinstance(data, bytes):
        pattern = env_var_pattern_bytes
        #
        if env_vars is None:
            if hasattr(os, "environb"):
                env_vars = os.environb
            else:
                env_vars = {
                    key.encode(): value.encode()
                    for key, value in os.environ.items()
                }
        #
        def replace(match):
            name = match.group("simple_name") or match.group("brace_name")
            default = match.group("default")
            value = env_vars.get(name)
            #
            if value is None and default is not None:
                return default
            #
            if value is None:
                return match.group(0)
            #
            if isinstance(value, str):
                return value.encode()
            #
            return value
    #
    else:
        pattern = env_var_pattern_str
        #
        if env_vars is None:
            env_vars = os.environ
        #
        def replace(match):
            name = match.group("simple_name") or match.group("brace_name")
            default = match.group("default")
            value = env_vars.get(name)
            #
            if value is None and default is not None:
                return default
            #
            if value is None:
                return match.group(0)
            #
            if isinstance(value, bytes):
                return value.decode()
            #
            return value
    #
    return pattern.sub(replace, data)


def config_substitution(obj, secrets):
    """ Allows to use raw environmental variables and secrets inside YAML/JSON config """
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            obj[config_substitution(key, secrets)] = \
                config_substitution(obj.pop(key), secrets)
    if isinstance(obj, list):
        for index, item in enumerate(obj):
            obj[index] = config_substitution(item, secrets)
    if isinstance(obj, str):
        if re.match(r"^\$\![a-zA-Z_][a-zA-Z0-9_]*$", obj.strip()) \
                and obj.strip()[2:] in os.environ:
            return os.environ[obj.strip()[2:]]
        if re.match(r"^\$\=\S*$", obj.strip()):
            obj_key = obj.strip()[2:]
            obj_value = secrets.get(obj_key, None)
            if obj_value is not None:
                return obj_value
        if match := re.match(r"^\$\:(?P<tunable>\S+?)(\:(?P<default>\S*))?$", obj.strip()):
            obj_tunable = match.group("tunable")
            obj_default = match.group("default")
            obj_value = tunable_get(obj_tunable, obj_default)
            if obj_value is not None:
                return obj_value
    return obj


def vault_secrets(settings):
    """ Get secrets from HashiCorp Vault """
    if "vault" not in settings:
        return {}
    #
    config = settings["vault"]
    #
    client = hvac.Client(
        url=config["url"],
        verify=config.get("ssl_verify", False),
        namespace=config.get("namespace", None),
    )
    #
    if "auth_token" in config:
        client.token = config["auth_token"]
    #
    if "auth_username" in config:
        client.auth.userpass.login(
            config.get("auth_username"), config.get("auth_password", "")
        )
    #
    if "auth_role_id" in config:
        client.auth.approle.login(
            config.get("auth_role_id"), config.get("auth_secret_id", "")
        )
    #
    if not client.is_authenticated():
        logging.error("Vault authentication failed")
        return {}
    #
    secrets_path = config.get("secrets_path", "secrets")
    secrets_mount_point = config.get("secrets_mount_point", "kv")
    #
    secrets_kv_version = config.get("secrets_kv_version", 2)
    secrets_version = config.get("secrets_version", None)
    #
    try:
        if secrets_kv_version == 1:
            result = client.secrets.kv.v1.read_secret(
                path=secrets_path,
                mount_point=secrets_mount_point,
            ).get("data", {})
        elif secrets_kv_version == 2:
            result = client.secrets.kv.v2.read_secret_version(
                path=secrets_path,
                version=secrets_version,
                mount_point=secrets_mount_point,
                raise_on_deleted_version=True,
            ).get("data", {}).get("data", {})
        else:
            logging.error("Unknown Vault KV version: %s", secrets_kv_version)
            result = {}
    except:  # pylint: disable=W0702
        logging.exception("Failed to read Vault secrets")
        result = {}
    #
    return result


tunable_cache = {}


def tunable_list(include_values=False):
    """ Tunables: enumerate """
    from tools import context  # pylint: disable=C0415,E0401
    from pylon.framework.db.models.tunable_value import TunableValue  # pylint: disable=C0415
    #
    result = []
    #
    with context.pylon_db.make_session() as db_session:
        tunable_objs = db_session.query(TunableValue).all()
    #
    for tunable_obj in tunable_objs:
        item = {
            "tunable": tunable_obj.tunable,
        }
        #
        if include_values:
            item["value"] = tunable_obj.value
        #
        result.append(item)
    #
    return result


def tunable_exists(tunable):
    """ Tunables: check """
    from tools import context  # pylint: disable=C0415,E0401
    from pylon.framework.db.models.tunable_value import TunableValue  # pylint: disable=C0415
    #
    if tunable in tunable_cache and tunable_cache[tunable] is not ...:
        return True
    #
    with context.pylon_db.make_session() as db_session:
        tunable_obj = db_session.query(TunableValue).get(tunable)
        #
        if tunable_obj is None:
            return False
        #
        return True


def tunable_get(tunable, default=None):
    """ Tunables: get """
    from tools import context  # pylint: disable=C0415,E0401
    from pylon.framework.db.models.tunable_value import TunableValue  # pylint: disable=C0415
    #
    if tunable in tunable_cache:
        data = tunable_cache[tunable]
        #
        if data is ...:
            return default
        #
        return data
    #
    with context.pylon_db.make_session() as db_session:
        tunable_obj = db_session.query(TunableValue).get(tunable)
        #
        if tunable_obj is None:
            tunable_cache[tunable] = ...
            return default
        #
        tunable_cache[tunable] = tunable_obj.value
        return tunable_obj.value


def tunable_set(tunable, value):
    """ Tunables: set """
    from tools import context  # pylint: disable=C0415,E0401
    from pylon.framework.db.models.tunable_value import TunableValue  # pylint: disable=C0415
    #
    with context.pylon_db.make_session() as db_session, db_session.begin():
        tunable_obj = db_session.query(TunableValue).get(tunable)
        #
        if tunable_obj is None:
            tunable_obj = TunableValue(
                tunable=tunable,
                value=value,
            )
            #
            db_session.add(tunable_obj)
        else:
            tunable_obj.value = value
        #
        tunable_cache[tunable] = value
        return None


def tunable_delete(tunable):
    """ Tunables: delete """
    from tools import context  # pylint: disable=C0415,E0401
    from pylon.framework.db.models.tunable_value import TunableValue  # pylint: disable=C0415
    #
    with context.pylon_db.make_session() as db_session, db_session.begin():
        tunable_obj = db_session.query(TunableValue).get(tunable)
        #
        if tunable_obj is not None:
            db_session.delete(tunable_obj)
        #
        tunable_cache[tunable] = ...
        return None
