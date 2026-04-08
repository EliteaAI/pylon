#!/usr/bin/python
# coding=utf-8
# pylint: disable=I0011

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
    Session tools
"""

import pickle

from flask_session.base import ServerSideSession, ServerSideSessionInterface, Serializer  # pylint: disable=E0401

from pylon.core.tools import log
from pylon.core.tools.context import Context


def make_session_interface(context):
    """ Make session interface for server-side session storage """
    # Patch
    ServerSideSession.regenerate = _regenerate
    ServerSideSession.destroy = _destroy
    #
    ServerSideSessionInterface.open_session = _patched_open_session(
        ServerSideSessionInterface.open_session
    )
    ServerSideSessionInterface.save_session = _patched_save_session(
        ServerSideSessionInterface.save_session
    )
    # Get configs
    application_config = context.settings.get("application", {})
    sessions_config = context.settings.get("sessions", {})
    #
    session_prefix = sessions_config.get("prefix", None)
    session_permanent = sessions_config.get("permanent", True)
    #
    # TODO: KeyValueNodeSessionInterface
    redis_config = sessions_config.get("redis", {})
    memory_config = sessions_config.get("memory", {})
    # Target
    target = Context()
    # Redis
    if redis_config:
        from redis import StrictRedis  # pylint: disable=E0401,C0415
        from flask_session.redis import RedisSessionInterface  # pylint: disable=E0401,C0415
        #
        redis_params = {
            "host": redis_config.get("host", "localhost"),
            "port": redis_config.get("port", 6379),
            #
            "socket_keepalive": redis_config.get("socket_keepalive", True),
            "socket_timeout": redis_config.get("socket_timeout", 60),
            "socket_connect_timeout": redis_config.get("socket_connect_timeout", 30),
        }
        #
        if redis_config.get("use_ssl", False):
            ssl_verify = redis_config.get("ssl_verify", False)
            #
            redis_params["ssl"] = True
            redis_params["ssl_cert_reqs"] = "required" if ssl_verify else "none"
            redis_params["ssl_check_hostname"] = ssl_verify
        #
        if redis_config.get("use_managed_identity", False):
            from redis_entraid.cred_provider import create_from_default_azure_credential  # pylint: disable=C0415,E0401,W0401
            #
            credential_provider = create_from_default_azure_credential(  # pylint: disable=E0602
                ("https://redis.azure.com/.default",),
            )
            #
            redis_params["credential_provider"] = credential_provider
        else:
            redis_password = redis_config.get("password", None)
            redis_username = redis_config.get("username", None)
            #
            if redis_password is not None:
                redis_params["password"] = redis_password
            #
            if redis_username is not None:
                redis_params["username"] = redis_username
        #
        client = StrictRedis(**redis_params)
        #
        target.cls = RedisSessionInterface
        target.kwargs = {
            "client": client,
            #
            "use_signer": "SECRET_KEY" in application_config,
            "permanent": session_permanent,
            "sid_length": 32,
            "serialization_format": "msgpack",
        }
        #
        if session_prefix:
            target.kwargs["key_prefix"] = session_prefix
        #
        log.info("Using redis for session storage")
    else:  # Memory
        from cachelib.simple import SimpleCache  # pylint: disable=E0401,C0415
        from flask_session.cachelib import CacheLibSessionInterface  # pylint: disable=E0401,C0415
        #
        client = SimpleCache(
            threshold=memory_config.get("threshold", 1000),
            default_timeout=memory_config.get("default_timeout", 0),
        )
        #
        target.cls = CacheLibSessionInterface
        target.kwargs = {
            "client": client,
            #
            "use_signer": "SECRET_KEY" in application_config,
            "permanent": session_permanent,
            "sid_length": 32,
            "serialization_format": "msgpack",
        }
        #
        if session_prefix:
            target.kwargs["key_prefix"] = session_prefix
        #
        log.info("Using memory for session storage")
    # Result
    def _result(app):
        result = target.cls(app, **target.kwargs)
        result.serializer = PickleSerializer(app)
        return result
    #
    return _result


def _regenerate(self):
    added_dummy = False
    #
    if not self:
        self["_dummy"] = True  # dummy value to trigger the session to be regenerated
        added_dummy = True
    #
    from flask import current_app  # pylint: disable=E0401,C0415
    current_app.session_interface.regenerate(self)
    #
    if added_dummy:
        self.pop("_dummy", None)


def _destroy(self):
    self.regenerate()  # regenerate will delete data from storage
    self.clear()
    #
    self.modified = True


def _patched_open_session(original_open_session):
    def _open_session(self, app, request):
        if isinstance(request, Context):
            if not hasattr(request, "cookies"):
                request.cookies = {}
        #
        return original_open_session(self, app, request)
    #
    return _open_session


def _patched_save_session(original_save_session):
    def _save_session(self, app, session, response):
        if isinstance(response, Context):
            if not hasattr(response, "set_cookie"):
                response.set_cookie = lambda *args, **kwargs: None
            #
            if not hasattr(response, "delete_cookie"):
                response.delete_cookie = lambda *args, **kwargs: None
            #
            if not hasattr(response, "vary"):
                response.vary = set()
        #
        return original_save_session(self, app, session, response)
    #
    return _save_session


class PickleSerializer(Serializer):
    """ Pickle serializer """

    def __init__(self, app):
        self.app = app

    def encode(self, session):
        """ Serialize """
        try:
            return pickle.dumps(dict(session), protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as e:
            self.app.logger.error(f"Failed to serialize session data: {e}")
            raise

    def decode(self, serialized_data):
        """ Deserialize """
        try:
            return pickle.loads(serialized_data)
        except Exception as e:
            self.app.logger.error(f"Failed to deserialize session data: {e}")
            raise
