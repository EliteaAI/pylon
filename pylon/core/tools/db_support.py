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
    DB support tools
"""

import os
import time
import threading
import importlib

import flask  # pylint: disable=E0401

import sqlalchemy  # pylint: disable=E0401
from sqlalchemy.orm import (  # pylint: disable=E0401
    Session,
    declarative_base,
)
from sqlalchemy.schema import CreateSchema  # pylint: disable=E0401

from arbiter.eventnode import hooks as eventnode_hooks  # pylint: disable=E0401

from pylon.core.tools import log
from pylon.core.tools.context import Context


#
# API
#


def basic_init(context):
    """ Init basic DB support """
    log.info("Initializing basic DB support")
    #
    # Pylon DB
    #
    context.pylon_db = Context()
    context.pylon_db.config = context.settings.get("pylon_db", {})
    #
    context.pylon_db.url = get_db_url(context.pylon_db)
    context.pylon_db.engine = make_engine(context.pylon_db)
    #
    context.pylon_db.schema_mapper = lambda schema: schema
    context.pylon_db.schema_enumerator = lambda schema: [schema]
    context.pylon_db.make_session = make_session_fn(context.pylon_db)
    #
    context.pylon_db.metadata = sqlalchemy.MetaData()
    context.pylon_db.Base = declarative_base(
        metadata=context.pylon_db.metadata,
    )
    #
    for model_resource in importlib.resources.contents(
            "pylon.framework.db.models"
    ):
        if model_resource.startswith("_") or not model_resource.endswith(".py"):
            continue
        #
        resource_name, _ = os.path.splitext(model_resource)
        #
        try:
            importlib.import_module(
                f"pylon.framework.db.models.{resource_name}"
            )
        except:  # pylint: disable=W0702
            log.exception(
                "Failed to import Pylon DB model module: %s",
                resource_name,
            )
            continue
    #
    try:
        context.pylon_db.metadata.create_all(bind=context.pylon_db.engine)
    except:  # pylint: disable=W0702
        log.exception("Failed to create Pylon DB entities")


def basic_deinit(context):
    """ De-init basic DB support """
    log.info("De-initializing basic DB support")
    #
    # Pylon DB
    #
    try:
        context.pylon_db.engine.dispose()
    except:  # pylint: disable=W0702
        pass


def init(context):
    """ Init DB support """
    if context.before_reloader:
        log.info(
            "Running in development mode before reloader is started. Skipping DB support init"
        )
        return
    #
    log.info("Initializing DB support")
    #
    # App DB
    #
    context.db = Context()
    context.db.config = context.settings.get("db", {})
    #
    context.db.url = get_db_url(context.db)
    context.db.engine = make_engine(context.db)
    #
    context.db.schema_mapper = lambda schema: schema
    context.db.schema_enumerator = lambda schema: [schema]
    context.db.make_session = make_session_fn(context.db)
    #
    # Local sessions
    #
    if context.db.config.get("auto_local_sessions", True):
        #
        # App hooks
        #
        context.app_manager.register_app_hook(
            lambda app: flask.appcontext_pushed.connect(db_app_setup, app)
        )
        context.app_manager.register_app_hook(
            lambda app: app.teardown_appcontext(db_app_teardown)
        )
        #
        # Runtime hooks
        #
        eventnode_hooks.before_callback_hooks.append(db_app_setup)
        eventnode_hooks.after_callback_hooks.append(db_app_teardown)


def deinit(context):
    """ De-init DB support """
    if context.before_reloader:
        log.info(
            "Running in development mode before reloader is started. Skipping DB support de-init"
        )
        return
    #
    log.info("De-initializing DB support")
    #
    # App DB
    #
    try:
        context.db.engine.dispose()
    except:  # pylint: disable=W0702
        pass
    #
    basic_deinit(context)


#
# Hooks
#


def db_app_setup(*args, **kwargs):
    """ Setup DB session """
    _ = args, kwargs
    #
    create_local_session()


def db_app_teardown(*args, **kwargs):
    """ Close request DB session """
    _ = args, kwargs
    #
    try:
        close_local_session()
    except:  # pylint: disable=W0702
        pass  # "Teardown functions must avoid raising exceptions."


#
# Tools: engine
#


def get_db_url(target_db):
    """ Get URL """
    return target_db.config.get("engine_url", "sqlite://")


def make_engine(
        target_db,
        mute_first_failed_connections=0,
        connection_retry_interval=3.0,
        max_failed_connections=None,
        log_errors=True,
):
    """ Make Engine and try to connect """
    #
    db_engine_url = target_db.url
    db_engine_kwargs = target_db.config.get("engine_kwargs", {}).copy()
    default_schema = None
    #
    if "default_schema" in target_db.config:
        default_schema = target_db.config["default_schema"]
        #
        if "execution_options" not in db_engine_kwargs:
            db_engine_kwargs["execution_options"] = {}
        else:
            db_engine_kwargs["execution_options"] = \
                db_engine_kwargs["execution_options"].copy()
        #
        execution_options = db_engine_kwargs["execution_options"]
        #
        if "schema_translate_map" not in execution_options:
            execution_options["schema_translate_map"] = {}
        else:
            execution_options["schema_translate_map"] = \
                execution_options["schema_translate_map"].copy()
        #
        execution_options["schema_translate_map"][None] = default_schema
    #
    engine = sqlalchemy.create_engine(
        db_engine_url, **db_engine_kwargs,
    )
    #
    # Managed identity
    if target_db.config.get("engine_use_managed_identity", False):
        from sqlalchemy import event  # pylint: disable=E0401,C0415
        from azure.identity import DefaultAzureCredential  # pylint: disable=E0401,C0415
        #
        @event.listens_for(engine, "do_connect")
        def _get_managed_token(dialect, conn_rec, cargs, cparams):  # pylint: disable=W0613
            credential = DefaultAzureCredential()
            token = credential.get_token("https://ossrdbms-aad.database.windows.net/.default").token
            cparams["password"] = token
    #
    failed_connections = 0
    #
    while True:
        try:
            connection = engine.connect()
            connection.close()
            #
            break
        except:  # pylint: disable=W0702
            if log_errors and \
                    failed_connections >= mute_first_failed_connections:
                #
                log.exception(
                    "Failed to create DB connection. Retrying in %s seconds",
                    connection_retry_interval,
                )
            #
            failed_connections += 1
            #
            if max_failed_connections and failed_connections > max_failed_connections:
                break
            #
            time.sleep(connection_retry_interval)
    #
    if default_schema is not None:
        with engine.connect() as connection:
            connection.execute(CreateSchema(default_schema, if_not_exists=True))
            connection.commit()
    #
    return engine


def make_session_fn(target_db):
    """ Create make_session() """
    _target_db = target_db
    _default_source_schema = target_db.config.get("default_source_schema", None)
    #
    def _make_session(schema=..., source_schema=_default_source_schema):
        target_schema = _target_db.schema_mapper(schema)
        #
        if target_schema is ...:
            target_engine = _target_db.engine
        else:
            execution_options = dict(_target_db.engine.get_execution_options())
            #
            if "schema_translate_map" not in execution_options:
                execution_options["schema_translate_map"] = {}
            else:
                execution_options["schema_translate_map"] = \
                    execution_options["schema_translate_map"].copy()
            #
            execution_options["schema_translate_map"][source_schema] = target_schema
            #
            target_engine = _target_db.engine.execution_options(
                **execution_options,
            )
        #
        return Session(
            bind=target_engine,
            expire_on_commit=False,
            autobegin=_target_db.config.get("session_autobegin", True),
        )
    #
    return _make_session


#
# Tools: local sessions
#


def check_local_entities():
    """ Validate or set entities in local """
    from tools import context  # pylint: disable=E0401,C0411,C0415
    #
    check_entities = {
        "db_session": None,
        "db_session_refs": 0,
    }
    #
    for key, default in check_entities.items():
        if key not in context.local.__dict__:
            setattr(context.local, key, default)


def create_local_session():
    """ Create and configure session, save in local """
    from tools import context  # pylint: disable=E0401,C0411,C0415
    #
    # Create DB session if needed
    #
    try:
        if context.local.db_session is None:
            context.local.db_session = LazyLocalSession(context)
        #
        context.local.db_session_refs += 1
    except:  # pylint: disable=W0702
        context.local.db_session = LazyLocalSession(context)
        context.local.db_session_refs = 1


def close_local_session():
    """ Finalize and close local session """
    from tools import context  # pylint: disable=E0401,C0411,C0415
    #
    # Get session / check present / lazy
    #
    try:
        session = context.local.db_session
    except:  # pylint: disable=W0702
        return
    #
    # Decrement refs count
    #
    if context.local.db_session_refs > 0:
        context.local.db_session_refs -= 1
    #
    if context.local.db_session_refs > 0:
        return  # We are in 'inner' close, leave session untouched
    #
    context.local.db_session_refs = 0
    #
    # Close session
    #
    if session is None:
        return  # Closed or broken elsewhere
    #
    context.local.db_session = None
    #
    if isinstance(session, LazyLocalSession):
        return
    #
    try:
        if session.is_active:
            try:
                session.commit()
            except:  # pylint: disable=W0702
                session.rollback()
                raise
        else:
            session.rollback()
    finally:
        session.close()


class local_session:  # pylint: disable=C0103
    """ Local session context manager """

    def __enter__(self):
        from tools import context  # pylint: disable=E0401,C0411,C0415
        #
        create_local_session()
        #
        return context.local.db_session

    def __exit__(self, exc_type, exc_value, exc_traceback):
        close_local_session()


class LazyLocalSessionMeta(type):
    """ Local session lazy meta class """

    def __getattr__(cls, name):
        log.debug("LazyLocalSession.cls.__getattr__(%s)", name)
        raise AttributeError("Not supported")


class LazyLocalSession(metaclass=LazyLocalSessionMeta):  # pylint: disable=R0903
    """ Local session lazy maker """

    def __init__(self, context):
        self.context = context

    def __getattr__(self, name):
        if isinstance(self.context.local.db_session, LazyLocalSession):
            self.context.local.db_session = self.context.db.make_session()
        #
        return getattr(self.context.local.db_session, name)


#
# Tools: module entities
#


class DbNamespaceHelper:  # pylint: disable=R0903
    """ Namespace-specific tools/helpers """

    def __init__(self, context):
        self.__context = context
        self.__namespaces = {}
        self.__lock = threading.Lock()

    def __getattr__(self, name):
        with self.__lock:
            if name not in self.__namespaces:
                self.__namespaces[name] = make_sqlalchemy_entities(self.__context)
            #
            try:
                from tools import this  # pylint: disable=E0401,C0411,C0415
                #
                this.db.ns_used.add(name)
            except:  # pylint: disable=W0702
                pass
            #
            return self.__namespaces[name]

    def get_namespaces(self):
        """ Get present namespaces """
        with self.__lock:
            return self.__namespaces


class DbSchemaHelper:  # pylint: disable=R0903
    """ Schema-specific tools/helpers """

    def __init__(self, context, entities):
        self.__context = context
        self.__entities = entities
        self.__schemas = {}
        self.__lock = threading.Lock()

    def __getattr__(self, name):
        with self.__lock:
            if name not in self.__schemas:
                self.__schemas[name] = make_sqlalchemy_entities(
                    self.__context, schema=name, add_schema_helper=False
                )
            #
            self.__entities.schema_used.add(name)
            #
            return self.__schemas[name]

    def get_schemas(self):
        """ Get present schemas """
        with self.__lock:
            return self.__schemas


class DbSessionHelper:  # pylint: disable=R0903
    """ Session helper """

    def __init__(self, context, module_name, make_session_args=None, make_session_kwargs=None):
        self.__context = context
        self.__module_name = module_name
        #
        self.__make_session_args = make_session_args if make_session_args is not None else []
        self.__make_session_kwargs = make_session_kwargs if make_session_kwargs is not None else {}
        #
        self.__local = threading.local()
        self.__lock = threading.Lock()

    def __ensure_local(self):
        with self.__lock:
            if "db_sessions" not in self.__local.__dict__:
                setattr(self.__local, "db_sessions", {})
            #
            if self.__module_name not in self.__local.db_sessions:
                self.__local.db_sessions[self.__module_name] = []

    #
    # Case: with this.db.session as session: ...
    #

    def __enter__(self):
        self.__ensure_local()
        #
        with self.__lock:
            session = self.__context.db.make_session(
                *self.__make_session_args,
                **self.__make_session_kwargs,
            )
            session_transaction = session.begin()
            #
            self.__local.db_sessions[self.__module_name].append(
                (session, session_transaction),
            )
            #
            session_transaction.__enter__()
            return session

    def __exit__(self, exc_type, exc_value, exc_traceback):
        with self.__lock:
            session, session_transaction = self.__local.db_sessions[self.__module_name].pop()
            #
            try:
                session_transaction.__exit__(exc_type, exc_value, exc_traceback)
            finally:
                session.close()

    #
    # Case: with this.db.session(schema, ...) as session: ...
    #

    def __call__(self, *args, **kwargs):
        return DbSessionHelper(self.__context, self.__module_name, args, kwargs)

    #
    # Case: this.db.session.add(...)
    #

    def __getattr__(self, name):
        self.__ensure_local()
        #
        if self.__local.db_sessions[self.__module_name]:
            latest_session = self.__local.db_sessions[self.__module_name][-1][0]
            return getattr(latest_session, name)
        #
        if not hasattr(self.__context.local, "db_session"):
            raise AttributeError("Local session is not present")
        #
        return getattr(self.__context.local.db_session, name)


def make_sqlalchemy_entities(context, schema=None, add_schema_helper=True):
    """ Make namespace-specific entities """
    _ = context
    result = Context()
    #
    # Base
    #
    result.metadata = sqlalchemy.MetaData(
        schema=schema,
        # naming_convention - should take from db.config
    )
    #
    result.Base = declarative_base(
        metadata=result.metadata,
    )
    #
    # Schema
    #
    if add_schema_helper:
        result.schema = DbSchemaHelper(context, result)
        result.schema_used = set()
    #
    return result


class DbUtils:  # pylint: disable=R0903
    """ DB utils """

    def __init__(self, context, module_name, spaces, entities):
        self.context = context
        self.module_name = module_name
        self.spaces = spaces
        self.entities = entities

    # def get_metadata(self, schemas=...): TODO


def make_module_entities(context, module_name, spaces):
    """ Make module-specific entities """
    result = make_sqlalchemy_entities(context)
    #
    # Namespaces
    #
    if "db_namespace_helper" not in spaces:
        spaces["db_namespace_helper"] = DbNamespaceHelper(context)
    #
    result.ns = spaces["db_namespace_helper"]
    result.ns_used = set()
    #
    # Session
    #
    result.session = DbSessionHelper(context, module_name)
    #
    # Utils
    #
    result.utils = DbUtils(context, module_name, spaces, result)
    #
    return result
