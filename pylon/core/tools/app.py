#!/usr/bin/python
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

""" App manager """

import uuid
import threading

import flask  # pylint: disable=E0401
import flask_restful  # pylint: disable=E0401
import socketio  # pylint: disable=E0401

from werkzeug.middleware.proxy_fix import ProxyFix  # pylint: disable=E0401

from pylon.core.tools import log
from pylon.core.tools import session
from pylon.core.tools.module.this import caller_module_name

from .server.socketio import create_socketio_instance
from .server.waitress import WaitressSocket
from .server.logging import LoggingMiddleware
from .app_shim import AppShim, ShimLoader


class AppManager:  # pylint: disable=R0903,R0902
    """ App manager: manages app instances """

    def __init__(self, context):
        self.context = context
        #
        self.app_config_defaults = {
            "PERMANENT_SESSION_LIFETIME": 2678400,  # 31 days, actual session lifetime will be less
            "SESSION_REFRESH_EACH_REQUEST": False,  # auth is already set only during login/logout
        }
        #
        self.managed_apps = []
        self.managed_api = None
        #
        self.session_interface = None
        #
        self.app_hooks = {}
        self.api_hooks = {}
        #
        self.module_app_refs = {}
        self.module_api_refs = {}
        #
        self.lock = threading.Lock()
        #
        self.can_run_hooks = True

    def init_hierarchy(self):
        """ Init A/WSGI app hierarchy """
        log.info("Initializing hierarchy")
        # SocketIO
        self.add_socketio_app()
        # App router
        self.add_app_router()
        # Session interface
        self.session_interface = session.make_session_interface(self.context)
        # API
        self.add_api_instance()
        # AppShim
        self.context.app = AppShim(self.context)

    def make_app_instance(self, *args, **kwargs):
        """ Make flask app instance """
        app = flask.Flask(*args, **kwargs)
        #
        app.config["CONTEXT"] = self.context
        app.config.from_mapping(self.app_config_defaults)
        app.config.from_mapping(self.context.settings.get("application", {}))
        #
        app.session_interface = self.session_interface(app)
        #
        app.url_build_error_handlers.append(self.url_build_error_handler(app))
        app.jinja_loader = ShimLoader(self.context, app)
        #
        with self.lock:
            hooks = list(self.app_hooks.values())
            #
            for hook in hooks:
                hook(app)
            #
            self.managed_apps.append(app)
            return app

    def url_build_error_handler(self, app):
        """ Redirect URL building to correct app """
        _app = app
        #
        def _handler(error, endpoint, values):
            _ = error
            #
            module_name = endpoint.split(".", 1)[0]
            #
            if module_name == "api":
                target_app = self.context.app_router.map.get("/api/", None)
            elif module_name in self.context.module_manager.descriptors:
                target_app = self.context.module_manager.descriptors[module_name].app
            else:
                target_app = None
            #
            if target_app is None:
                return None
            #
            if target_app is _app:
                from flask.globals import _cv_request  # pylint: disable=E0401,C0415
                req_ctx = _cv_request.get(None)
                #
                if req_ctx is None or req_ctx.app is target_app:
                    return None
            #
            if endpoint not in target_app.view_functions:
                return None
            #
            if flask.has_request_context():
                with target_app.request_context(flask.request.environ):
                    return target_app.url_for(endpoint, **values)
            #
            with target_app.app_context():
                return target_app.url_for(endpoint, **values)
        #
        return _handler

    def register_app_hook(self, hook_lambda):
        """ Register app creation hook """
        with self.lock:
            while True:
                hook_uuid = str(uuid.uuid4())
                if hook_uuid not in self.app_hooks:
                    break
            #
            self.app_hooks[hook_uuid] = hook_lambda
            #
            module_name = caller_module_name()
            #
            if module_name not in self.module_app_refs:
                self.module_app_refs[module_name] = []
            #
            self.module_app_refs[module_name].append(hook_uuid)
            #
            if self.can_run_hooks:
                for app in self.managed_apps:
                    hook_lambda(app)
            #
            return hook_uuid

    def unregister_app_hook(self, hook_uuid):
        """ Unregister app creation hook """
        with self.lock:
            self.app_hooks.pop(hook_uuid, None)

    def register_api_hook(self, hook_lambda):
        """ Register api creation hook """
        with self.lock:
            while True:
                hook_uuid = str(uuid.uuid4())
                if hook_uuid not in self.api_hooks:
                    break
            #
            self.api_hooks[hook_uuid] = hook_lambda
            #
            module_name = caller_module_name()
            #
            if module_name not in self.module_api_refs:
                self.module_api_refs[module_name] = []
            #
            self.module_api_refs[module_name].append(hook_uuid)
            #
            if self.can_run_hooks:
                hook_lambda(self.managed_api)
            #
            return hook_uuid

    def unregister_api_hook(self, hook_uuid):
        """ Unregister api creation hook """
        with self.lock:
            self.api_hooks.pop(hook_uuid, None)

    def add_socketio_app(self):
        """ Create SIO """
        log.info("Creating SocketIO instance")
        #
        create_socketio_instance(self.context)
        #
        socketio_path="socket.io"
        if self.context.url_prefix:
            socketio_path = f"{self.context.url_prefix}/{socketio_path}"
        #
        if self.context.is_async:
            socketio_app = socketio.ASGIApp(
                socketio_server=self.context.sio_async,
                socketio_path="/",
            )
        else:
            socketio_app = socketio.WSGIApp(
                socketio_app=self.context.sio,
                socketio_path="/",
            )
        #
        if self.context.web_runtime == "waitress":
            socketio_app = WaitressSocket(socketio_app)
        #
        socketio_log = self.context.settings.get("socketio", {}).get("log", False)
        socketio_route = f'/{socketio_path.strip("/")}/'
        #
        self.context.socketio_route = socketio_route
        self.context.root_router.map[socketio_route] = socketio_app
        #
        if socketio_log and self.context.is_async:
            log.warning("SocketIO logs are not supported with %s", self.context.web_runtime)
        #
        if not socketio_log:
            for method in ["GET", "POST"]:
                self.context.server_log_filter.filter_strings.append(f"{method} {socketio_route}")

    def add_app_router(self):
        """ Create router """
        from .server.wsgi import RouterApp  # pylint: disable=C0415
        #
        self.context.app_router = RouterApp(update_path=False)
        app_router = self.context.app_router
        #
        if self.context.web_runtime in ["waitress", "hypercorn"]:
            app_router = LoggingMiddleware(app_router)
        #
        proxy_settings = self.context.settings.get("server", {}).get("proxy", False)
        #
        if isinstance(proxy_settings, dict):
            app_router = ProxyFix(
                app_router,
                x_for=proxy_settings.get("x_for", 1),
                x_proto=proxy_settings.get("x_proto", 1),
                x_host=proxy_settings.get("x_host", 0),
                x_port=proxy_settings.get("x_port", 0),
                x_prefix=proxy_settings.get("x_prefix", 0),
            )
        elif proxy_settings:
            app_router = ProxyFix(
                app_router, x_for=1, x_proto=1,
            )
        #
        self.context.app_router_wsgi = app_router
        #
        if self.context.is_async:
            import asgiref.wsgi  # pylint: disable=E0401,C0412,C0415
            app_router = asgiref.wsgi.WsgiToAsgi(app_router)
        #
        if self.context.url_prefix:
            app_route = f'/{self.context.url_prefix.strip("/")}/'
        else:
            app_route="/"
        #
        self.context.root_router.map[app_route] = app_router

    def add_api_instance(self):
        """ Create API """
        log.info("Creating API instance")
        #
        api_app = self.make_app_instance("pylon")
        self.managed_api = flask_restful.Api(api_app, catch_all_404s=True)
        #
        with self.lock:
            hooks = list(self.api_hooks.values())
            #
            for hook in hooks:
                hook(self.managed_api)
        #
        self.context.app_router.map["/api/"] = api_app
