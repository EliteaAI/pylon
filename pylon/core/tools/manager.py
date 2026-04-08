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

""" Pylon manager """

import sys
import importlib
import threading

from pylon.core.tools import log
from pylon.core.tools import db_support
from pylon.core.tools.module.descriptor import ModuleDescriptor


class Manager:  # pylint: disable=R0903,R0902
    """ Manager: manages pylon """

    def __init__(self, context):
        self.context = context
        self.lock = threading.Lock()
        #
        self.reload_hooks = []

    def register_reload_hook(self, hook):
        """ Register hook """
        with self.lock:
            if hook not in self.reload_hooks:
                self.reload_hooks.append(hook)

    def unregister_reload_hook(self, hook):
        """ Unregister hook """
        with self.lock:
            if hook in self.reload_hooks:
                self.reload_hooks.remove(hook)

    def reload_plugin(self, name):
        """ Plugin hot reload """
        with self.lock:
            return self._reload_plugin(name)

    def _reload_plugin(self, name):  # pylint: disable=R0912,R0915
        log.info("Reloading plugin: %s", name)
        #
        prev_descriptor = self.context.module_manager.descriptors.pop(name, None)
        #
        if prev_descriptor is None:
            log.error("Plugin is not loaded: %s", name)
            return
        #
        module_package = f"plugins.{name}"
        #
        # De-init and unload
        #
        prev_modules = {}
        #
        for mod_name in list(sys.modules.keys()):
            if mod_name == module_package or mod_name.startswith(f"{module_package}."):
                prev_modules[mod_name] = sys.modules.pop(mod_name)
        #
        if prev_descriptor.module is not None:
            try:
                prev_descriptor.module.unready()
                prev_descriptor.module.deinit()
            except:  # pylint: disable=W0702
                log.exception("Error during plugin deinit: %s", name)
        #
        self.context.module_manager.modules.pop(name, None)
        #
        try:
            prev_descriptor.deinit_all()
        except:  # pylint: disable=W0702
            log.exception("Error during descriptor deinit: %s", name)
        #
        # Hooks
        #
        for hook in self.reload_hooks:
            try:
                hook(name)
            except:  # pylint: disable=W0702
                log.exception("Reload hook failed, skipping")
        #
        # Load and init
        #
        next_descriptor = ModuleDescriptor(
            self.context,
            prev_descriptor.name,
            prev_descriptor.loader,
            prev_descriptor.metadata,
            prev_descriptor.requirements,
        )
        #
        next_descriptor.requirements_base = prev_descriptor.requirements_base
        next_descriptor.requirements_path = prev_descriptor.requirements_path
        next_descriptor.activated_paths = prev_descriptor.activated_paths
        next_descriptor.activated_bases = prev_descriptor.activated_bases
        next_descriptor.prepared = prev_descriptor.prepared
        #
        self.context.module_manager.descriptors[name] = next_descriptor
        #
        next_descriptor.load_config()
        #
        if next_descriptor.prepared:
            self.context.app_manager.can_run_hooks = False
            #
            try:
                module_pkg = importlib.import_module(f"plugins.{next_descriptor.name}.module")
                module_obj = module_pkg.Module(
                    context=self.context,
                    descriptor=next_descriptor,
                )
                next_descriptor.module = module_obj
                #
                db_support.create_local_session()
                try:
                    module_obj.init()
                    module_obj.ready()
                finally:
                    db_support.close_local_session()
                #
            except:  # pylint: disable=W0702
                log.exception("Failed to enable module: %s", next_descriptor.name)
            else:
                self.context.module_manager.modules[next_descriptor.name] = next_descriptor
                next_descriptor.activated = True
            #
            self.context.app_manager.can_run_hooks = True
        #
        next_modules = {}
        #
        for mod_name in list(sys.modules.keys()):
            if mod_name == module_package or mod_name.startswith(f"{module_package}."):
                next_modules[mod_name] = sys.modules.pop(mod_name)
        #
        for mod_name, mod_obj in next_modules.items():
            if mod_name in prev_modules:
                target = prev_modules.pop(mod_name)
                target.__dict__.clear()
                target.__dict__.update(mod_obj.__dict__)
            else:
                target = mod_obj
            #
            sys.modules[mod_name] = target
        #
        # Cleanup
        #
        del prev_modules
        del prev_descriptor
        #
        # Reload other entities
        #
        self.context.app_manager.add_api_instance()
        self.reload_apps()

    def reload_apps(self):
        """ Reload all apps """
        for name, descriptor in self.context.module_manager.descriptors.items():
            log.info("Reloading app: %s", name)
            #
            descriptor.app = self.context.app_manager.make_app_instance(f"plugins.{name}")
            #
            if descriptor.blueprint is not None:
                descriptor.app.register_blueprint(descriptor.blueprint)
            #
            if descriptor.router_path is not None:
                self.context.app_router.map[descriptor.router_path] = descriptor.app
