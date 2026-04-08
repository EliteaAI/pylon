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

# pylint: disable=C0116
""" App compatibility shim """

import jinja2  # pylint: disable=E0401

from pylon.core.tools import log


class AppShimMeta(type):
    """ AppShim meta class """

    def __getattr__(cls, name):
        log.debug("AppShim.cls.__getattr__(%s)", name)
        raise AttributeError("Not supported")


class AppShim(metaclass=AppShimMeta):  # pylint: disable=R0903
    """ App shim: compatibility proxy adapter """

    def __init__(self, context):
        self.context = context

    @property
    def session_cookie_name(self):
        """ Shim """
        from tools import this  # pylint: disable=E0401,C0415
        return this.descriptor.app.config["SESSION_COOKIE_NAME"]

    def __getattr__(self, name):
        hook_decorators = [
            "errorhandler",
            "template_filter",
        ]
        #
        if name in hook_decorators:
            return self.__make_hook_decorator(name)
        #
        hook_functions = [
            "before_request",
            "after_request",
            "context_processor",
        ]
        #
        if name in hook_functions:
            return self.__make_hook_function(name)
        #
        this_proxies = [
            "app_context",
            "secret_key",
            "session_interface",
            "config",
        ]
        #
        if name in this_proxies:
            from tools import this  # pylint: disable=E0401,C0415
            return getattr(this.descriptor.app, name)
        #
        log.debug("AppShim.__getattr__(%s)", name)
        raise RuntimeError("Not supported")

    def __make_hook_function(self, name):
        _name = name
        #
        def _function(*args, **kwargs):
            _args = args
            _kwargs = kwargs
            #
            def _hook(app):
                getattr(app, _name)(*_args, **_kwargs)
            #
            self.context.app_manager.register_app_hook(_hook)
        #
        return _function

    def __make_hook_decorator(self, name):
        _name = name
        #
        def _decorator(*i_args, **i_kwargs):
            _i_args = i_args
            _i_kwargs = i_kwargs
            #
            def _decorate(*j_args, **j_kwargs):
                _j_args = j_args
                _j_kwargs = j_kwargs
                #
                def _hook(app):
                    getattr(app, _name)(*_i_args, **_i_kwargs)(*_j_args, **_j_kwargs)
                #
                self.context.app_manager.register_app_hook(_hook)
            #
            return _decorate
        #
        return _decorator


class ShimLoader(jinja2.BaseLoader):
    """ Shim template loader """

    def __init__(self, context, app):
        self.context = context
        self.app = app

    def get_source(self, environment, template):
        """ Shim method """
        if ":" in template:
            module_name = template.split(":")[0]
            return self._get_source_from_module(module_name, environment, template)
        #
        apps = []
        apps.append(self.app)
        for app in self.context.app_manager.managed_apps:
            if app not in apps:
                apps.append(app)
        #
        for app in apps:
            try:
                return self._get_source_from_app(app, environment, template)
            except jinja2.TemplateNotFound:
                continue
        #
        raise jinja2.TemplateNotFound(template)

    def _get_source_from_module(self, module_name, environment, template):
        if module_name not in self.context.module_manager.descriptors:
            raise jinja2.TemplateNotFound(template)
        #
        target_app = self.context.module_manager.descriptors[module_name].app
        #
        if target_app is None:
            raise jinja2.TemplateNotFound(template)
        #
        return self._get_source_from_app(target_app, environment, template)

    def _get_source_from_app(self, app, environment, template):
        for blueprint in app.iter_blueprints():
            blueprint_loader = blueprint.jinja_loader
            #
            if blueprint_loader is None:
                continue
            #
            try:
                return blueprint_loader.get_source(environment, template)
            except jinja2.TemplateNotFound:
                continue
        #
        raise jinja2.TemplateNotFound(template)

    def list_templates(self):
        """ Shim method """
        result = set()
        #
        apps = []
        apps.append(self.app)
        for app in self.context.app_manager.managed_apps:
            if app not in apps:
                apps.append(app)
        #
        for app in apps:
            for blueprint in app.iter_blueprints():
                blueprint_loader = blueprint.jinja_loader
                #
                if blueprint_loader is None:
                    continue
                #
                for template in blueprint_loader.list_templates():
                    result.add(template)
        #
        return list(result)
