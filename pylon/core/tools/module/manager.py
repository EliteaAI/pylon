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

import os
import sys
import site
import json
import time
import types
import shutil
import hashlib
import tempfile
import subprocess
import importlib

import pkg_resources
import packaging.requirements

from pylon.core.tools import (
    log,
    process,
    dependency,
    db_support,
    ssl,
    config,
    profiling,
)

from .proxy import (
    ModuleProxy,
    ModuleDescriptorProxy,
)
from .loader import (
    DataModuleLoader,
    DataModuleProvider,
)
from .descriptor import ModuleDescriptor
from .overrides import PYLON_MODULE_REQUIREMENTS_OVERRIDES


class ModuleManager:  # pylint: disable=R0902
    """ Manages modules """

    def __init__(self, context):
        self.context = context
        #
        self.settings = self.context.settings.get("modules", {})
        self.setting_overrides = {}  # allow to override without using live config
        #
        self.providers = {}  # object_type -> provider_instance
        self.descriptors = {}  # module_name -> module_descriptor (all)
        self.modules = {}  # module_name -> module_descriptor (enabled)
        #
        self.temporary_objects = []
        self.activated_paths = []
        self.activated_bases = []
        #
        self.descriptor = ModuleDescriptorProxy(self)
        self.module = ModuleProxy(self)
        #
        self.load_order = []
        #
        self.pylon_requirements_hash = hashlib.sha256(
            self.context.pylon_requirements.encode()
        ).hexdigest()
        self.previous_requirements_hash = config.tunable_get(
            "pylon_requirements_hash", b""
        ).decode()
        #
        self.pylon_requirements_changed = \
            self.pylon_requirements_hash != self.previous_requirements_hash
        if self.pylon_requirements_changed:
            log.info(
                "Pylon requirements changed: %s -> %s",
                self.previous_requirements_hash, self.pylon_requirements_hash,
            )
            config.tunable_set("pylon_requirements_hash", self.pylon_requirements_hash.encode())

    def resolve_settings(self, key, default=None):
        """ Get settings value with overrides """
        if key in self.setting_overrides:
            return self.setting_overrides[key]
        #
        key_path = key.split(".")
        current_settings = self.settings
        #
        while key_path:
            current_key = key_path.pop(0)
            #
            if not key_path:  # last key
                return current_settings.get(current_key, default)
            #
            current_settings = current_settings.get(current_key, {})
        #
        return default

    def init_modules(self):
        """ Load and init modules """
        # Configure bytecode caching
        pycache_path = self.settings.get("plugins", {}).get("pycache", None)
        if pycache_path is not None:
            try:
                os.makedirs(pycache_path, exist_ok=True)
            except:  # pylint: disable=W0702
                pass
            sys.pycache_prefix = pycache_path
            sys.dont_write_bytecode = False
        else:
            sys.dont_write_bytecode = True
        # Register resource providers
        pkg_resources.register_loader_type(DataModuleLoader, DataModuleProvider)
        # Make plugins holder
        if "plugins" not in sys.modules:
            sys.modules["plugins"] = types.ModuleType("plugins")
            sys.modules["plugins"].__path__ = []
        # Check if actions are needed
        if self.context.before_reloader:
            log.info(
                "Running in development mode before reloader is started. Skipping module loading"
            )
            return
        # Make providers
        self._init_providers()
        #
        # Preload
        #
        log.info("Preloading modules")
        # Create loaders for preload modules
        preload_module_meta_map = self._make_preload_module_meta_map()
        # Resolve preload module load order
        preload_module_order = self._resolve_depencies(
            preload_module_meta_map, list(self.modules),
        )
        # Make preload module descriptors
        preload_module_descriptors = self._make_descriptors(
            preload_module_meta_map, preload_module_order,
        )
        # Install/get/activate requirements and initialize preload modules
        preloaded_items = self._prepare_modules(preload_module_descriptors)
        self._activate_modules(preload_module_descriptors)
        #
        # Target
        #
        log.info("Preparing modules")
        # Create loaders for target modules
        target_module_meta_map = self._make_target_module_meta_map()
        # Resolve target module load order
        target_module_order = self._resolve_depencies(
            target_module_meta_map, list(self.modules),
        )
        # Make target module descriptors
        target_module_descriptors = self._make_descriptors(
            target_module_meta_map, target_module_order,
        )
        # Install/get requirements
        self._prepare_modules(target_module_descriptors, preloaded_items)
        # Activate and init/prealod modules
        log.info("Activating modules")
        self._activate_modules(target_module_descriptors)
        #
        if self.context.server_mode != "preload":
            # Run ready callbacks
            log.info("Running ready callbacks")
            self._run_ready_callbacks()

    def _run_ready_callbacks(self):
        for module_name in list(self.modules):
            try:
                db_support.create_local_session()
                try:
                    self.modules[module_name].module.ready()
                finally:
                    db_support.close_local_session()
            except:  # pylint: disable=W0702
                pass

    def _run_unready_callbacks(self):
        for module_name in reversed(list(self.modules)):
            try:
                db_support.create_local_session()
                try:
                    self.modules[module_name].module.unready()
                finally:
                    db_support.close_local_session()
            except:  # pylint: disable=W0702
                pass

    def _resolve_depencies(self, module_map, present_modules=None):
        local_module_map = module_map.copy()
        #
        while True:
            try:
                return dependency.resolve_depencies(local_module_map, present_modules)
            except dependency.DependencyNotPresentError as e:
                log.error(
                    "Excluding module: '%s' (missing dependency: '%s')",
                    e.required_by,
                    e.missing_dependency,
                )
                #
                local_module_map.pop(e.required_by, None)
            except dependency.CircularDependencyError as e:
                log.error(
                    "Excluding modules: '%s', '%s' (circular dependency)",
                    e.dependency_a,
                    e.dependency_b,
                )
                #
                local_module_map.pop(e.dependency_a, None)
                local_module_map.pop(e.dependency_b, None)

    def _make_preload_module_meta_map(self):
        if "preload" not in self.settings:
            return {}
        #
        meta_items = []
        #
        for module_name in self.settings["preload"]:
            if not self.providers["plugins"].plugin_exists(module_name):
                module_target = self.settings["preload"][module_name].copy()
                #
                if "provider" not in module_target or \
                        "type" not in module_target["provider"]:
                    continue
                #
                provider_config = module_target.pop("provider").copy()
                provider_type = provider_config.pop("type")
                #
                preload_retries = self.settings.get("preload_retries", 5)
                preload_retry_delay = self.settings.get("preload_retry_delay", 5)
                #
                for retry in range(preload_retries):
                    try:
                        provider = importlib.import_module(
                            f"pylon.core.providers.source.{provider_type}"
                        ).Provider(self.context, provider_config)
                        provider.init()
                        #
                        module_source = provider.get_source(module_target)
                        #
                        provider.deinit()
                    except:  # pylint: disable=W0702
                        log.exception(
                            "Could not preload module (retry=%s, delay=%s): %s",
                            retry, preload_retry_delay, module_name,
                        )
                        time.sleep(preload_retry_delay)
                    else:
                        self.providers["plugins"].add_plugin(module_name, module_source)
                        break
            #
            try:
                module_loader, module_metadata = self._make_loader_and_metadata(module_name)
            except:  # pylint: disable=W0702
                log.exception("Could not make module loader: %s", module_name)
                continue
            #
            meta_items.append((module_name, module_metadata, module_loader))
        #
        meta_items.sort(key=lambda item: item[1].get("order_weight", 0))
        module_meta_map = {}  # module_name -> (metadata, loader)
        #
        for module_name, module_metadata, module_loader in meta_items:
            module_meta_map[module_name] = (module_metadata, module_loader)
        #
        return module_meta_map

    def _make_target_module_meta_map(self):
        meta_items = []
        #
        for module_name in self.providers["plugins"].list_plugins(exclude=list(self.modules)):
            try:
                module_loader, module_metadata = self._make_loader_and_metadata(module_name)
            except:  # pylint: disable=W0702
                log.exception("Could not make module loader: %s", module_name)
                continue
            #
            meta_items.append((module_name, module_metadata, module_loader))
        #
        meta_items.sort(key=lambda item: item[1].get("order_weight", 0))
        module_meta_map = {}  # module_name -> (metadata, loader)
        #
        for module_name, module_metadata, module_loader in meta_items:
            module_meta_map[module_name] = (module_metadata, module_loader)
        #
        return module_meta_map

    def _make_loader_and_metadata(self, module_name):
        module_loader = self.providers["plugins"].get_plugin_loader(module_name)
        #
        if not module_loader.has_file("metadata.json"):
            raise ValueError(f"Module has no metadata: {module_name}")
        #
        module_metadata = json.loads(module_loader.get_data("metadata.json"))
        #
        if module_loader.has_directory("static") or module_metadata.get("extract", False):
            module_loader = module_loader.get_local_loader(self.temporary_objects)
        #
        return module_loader, module_metadata

    def _make_descriptors(self, module_meta_map, module_order):
        module_descriptors = []
        #
        for module_name in module_order:
            module_metadata, module_loader = module_meta_map[module_name]
            # Get module requirements
            if module_loader.has_file("requirements.txt"):
                module_requirements = module_loader.get_data("requirements.txt").decode()
            else:
                module_requirements = ""
            # Make descriptor
            module_descriptor = ModuleDescriptor(
                self.context, module_name, module_loader, module_metadata, module_requirements
            )
            # Preload config
            module_descriptor.load_config()
            # Preload state
            module_descriptor.load_state()
            #
            module_descriptors.append(module_descriptor)
            self.descriptors[module_name] = module_descriptor
        #
        return module_descriptors

    def _prepare_modules(self, module_descriptors, prepared_items=None):  # pylint: disable=R0914,R0915
        if prepared_items is None:
            cache_hash_chunks = []
            module_site_paths = []
            module_constraint_paths = []
            #
            cache_hash_chunks.append(self.pylon_requirements_hash)
        else:
            cache_hash_chunks, module_site_paths, module_constraint_paths = prepared_items
        #
        for module_descriptor in module_descriptors:
            if module_descriptor.name in self.settings.get("skip", []):
                log.warning("Skipping module prepare: %s", module_descriptor.name)
                continue
            #
            module_name = module_descriptor.name
            #
            module_requirements = self._apply_requirements_overrides(
                module_name, module_descriptor.requirements
            )
            #
            requirements_hash = hashlib.sha256(module_requirements).hexdigest()
            cache_hash_chunks.append(requirements_hash)
            cache_hash = hashlib.sha256("_".join(cache_hash_chunks).encode()).hexdigest()
            #
            requirements_txt_fd, requirements_txt = tempfile.mkstemp(".txt")
            self.temporary_objects.append(requirements_txt)
            os.close(requirements_txt_fd)
            #
            with open(requirements_txt, "wb") as file:
                file.write(module_requirements)
            #
            if not self.providers["requirements"].requirements_exist(module_name, cache_hash):
                requirements_install_base = self.providers["requirements"].get_requirements(
                    module_name, None, self.temporary_objects,
                )
                #
                install_kwargs = {}
                #
                if requirements_install_base is None:
                    requirements_install_base = tempfile.mkdtemp()
                    self.temporary_objects.append(requirements_install_base)
                elif self.pylon_requirements_changed:
                    install_kwargs["delete_on_failure"] = True
                    install_kwargs["module_name"] = module_name
                    install_kwargs["provider"] = self.providers["requirements"]
                #
                log.info("Installing requirements for: %s", module_name)
                #
                try:
                    self.install_requirements(
                        requirements_path=requirements_txt,
                        target_site_base=requirements_install_base,
                        additional_site_paths=module_site_paths,
                        constraint_paths=module_constraint_paths,
                        #
                        **install_kwargs,
                    )
                except:  # pylint: disable=W0702
                    log.exception("Failed to install requirements for: %s", module_descriptor.name)
                    continue
                #
                self.providers["requirements"].add_requirements(
                    module_name, cache_hash, requirements_install_base,
                )
            #
            requirements_base = \
                self.providers["requirements"].get_requirements(
                    module_name, cache_hash, self.temporary_objects,
                )
            #
            requirements_path = self.get_user_site_path(requirements_base)
            module_site_paths.append(requirements_path)
            #
            module_descriptor.requirements_base = requirements_base
            module_descriptor.requirements_path = requirements_path
            #
            requirements_mode = self.settings["requirements"].get("mode", "relaxed")
            if requirements_mode == "constrained":
                module_constraint_paths.append(requirements_txt)
            elif requirements_mode == "strict":
                frozen_module_requirements = self.freeze_site_requirements(
                    target_site_base=requirements_base,
                    requirements_path=requirements_txt,
                    additional_site_paths=module_site_paths,
                )
                #
                frozen_requirements_fd, frozen_requirements = tempfile.mkstemp(".txt")
                self.temporary_objects.append(frozen_requirements)
                os.close(frozen_requirements_fd)
                #
                with open(frozen_requirements, "wb") as file:
                    file.write(frozen_module_requirements.encode())
                #
                module_constraint_paths.append(frozen_requirements)
            #
            module_descriptor.prepared = True
        #
        return cache_hash_chunks, module_site_paths, module_constraint_paths

    def _activate_modules(self, module_descriptors):  # pylint: disable=R0912,R0914,R0915
        requirements_activation = self.settings["requirements"].get("activation", "steps")
        #
        if requirements_activation == "bulk":
            log.info("Using bulk module requirements activation mode")
            for module_descriptor in module_descriptors:
                if module_descriptor.prepared:
                    self.activate_path(module_descriptor.requirements_path)
                    self.activated_paths.append(module_descriptor.requirements_path)
                    self.activated_bases.append(module_descriptor.requirements_base)
        #
        can_skip_pylon_check = self.settings.get("plugins", {}).get("can_skip_pylon_check", False)
        check_pylon_version = True
        #
        if self.context.pylon_version == "unknown" and can_skip_pylon_check:
            log.warning("Failed to resolve installed pylon version, skipping version checks")
            check_pylon_version = False
        #
        for module_descriptor in module_descriptors:
            if not module_descriptor.prepared:
                log.warning("Skipping un-prepared module: %s", module_descriptor.name)
                continue
            #
            all_required_dependencies_present = True
            #
            for required_dependency in module_descriptor.metadata.get("depends_on", []):
                if required_dependency not in self.modules:
                    log.error(
                        "Required dependency is not present: %s (required by %s)",
                        required_dependency, module_descriptor.name,
                    )
                    all_required_dependencies_present = False
            #
            version_requirements = module_descriptor.metadata.get("version_requirements", {})
            #
            if check_pylon_version and "pylon" in version_requirements:
                pylon_requirement = packaging.requirements.Requirement(
                    f'pylon {version_requirements["pylon"]}'
                )
                if not pylon_requirement.specifier.contains(
                        self.context.pylon_version,
                        prereleases=True,
                ):
                    log.error(
                        "Pylon (%s) version is not satisfied: %s (required by %s)",
                        self.context.pylon_version,
                        version_requirements["pylon"], module_descriptor.name,
                    )
                    all_required_dependencies_present = False
            #
            for plugin_name, plugin_req_data in version_requirements.get("plugins", {}).items():
                if plugin_name in self.descriptors:
                    plugin_descriptor = self.descriptors[plugin_name]
                    plugin_requirement = packaging.requirements.Requirement(
                        f"{plugin_name} {plugin_req_data}"
                    )
                    plugin_version = plugin_descriptor.metadata.get("version", "0.0.0")
                    if not plugin_requirement.specifier.contains(
                            plugin_version,
                            prereleases=True,
                    ):
                        log.error(
                            "Plugin %s (%s) version is not satisfied: %s (required by %s)",
                            plugin_name, plugin_version, plugin_req_data, module_descriptor.name,
                        )
                        all_required_dependencies_present = False
            #
            if not all_required_dependencies_present:
                log.error("Skipping module: %s", module_descriptor.name)
                continue
            #
            if requirements_activation != "bulk":
                self.activate_path(module_descriptor.requirements_path)
                self.activated_paths.append(module_descriptor.requirements_path)
                self.activated_bases.append(module_descriptor.requirements_base)
            #
            module_descriptor.activated_paths = self.activated_paths.copy()
            module_descriptor.activated_bases = self.activated_bases.copy()
            self.activate_loader(module_descriptor.loader)
            #
            log.debug("Initializing module: %s", module_descriptor.name)
            profiling.profiling_start(self.context, f"module:init:{module_descriptor.name}")
            #
            try:
                module_pkg = importlib.import_module(f"plugins.{module_descriptor.name}.module")
                module_obj = module_pkg.Module(
                    context=self.context,
                    descriptor=module_descriptor,
                )
                module_descriptor.module = module_obj
                #
                if self.context.server_mode == "preload":
                    module_obj.preload()
                else:
                    with db_support.local_session():
                        module_obj.init()
                    #
                    if not module_descriptor.state.get("installed", False):
                        with db_support.local_session():
                            module_obj.install()
                        #
                        module_descriptor.state["installed"] = True
                        module_descriptor.save_state()
            except:  # pylint: disable=W0702
                log.exception("Failed to enable module: %s", module_descriptor.name)
                continue
            finally:
                profiling.profiling_stop(self.context, f"module:init:{module_descriptor.name}")
            #
            self.modules[module_descriptor.name] = module_descriptor
            module_descriptor.activated = True
            #
            self.load_order.append(module_descriptor.name)

    def deinit_modules(self):
        """ De-init and unload modules """
        if self.context.before_reloader:
            log.info(
                "Running in development mode before reloader is started. Skipping module unloading"
            )
            return
        #
        self._run_unready_callbacks()
        #
        for module_name in reversed(list(self.modules)):
            log.debug("De-initializing module: %s", module_name)
            profiling.profiling_start(self.context, f"module:deinit:{module_name}")
            #
            try:
                if self.context.server_mode == "preload":
                    pass
                else:
                    db_support.create_local_session()
                    try:
                        self.modules[module_name].module.deinit()
                    finally:
                        db_support.close_local_session()
            except:  # pylint: disable=W0702
                log.exception("Failed to de-init module: %s", module_name)
            finally:
                profiling.profiling_stop(self.context, f"module:deinit:{module_name}")
        #
        # TODO: should we save plugin state after deinit automatically?
        #
        self._deinit_providers()
        #
        for obj in self.temporary_objects:
            try:
                if os.path.isdir(obj):
                    shutil.rmtree(obj)
                else:
                    os.remove(obj)
            except:  # pylint: disable=W0702
                pass

    def _init_providers(self):
        internal_providers = {
            "config": "pylon.core.providers.internal.db_config",
        }
        #
        for key in ["plugins", "requirements", "config"]:
            log.info("Initializing %s provider", key)
            #
            if key not in self.settings or \
                    "provider" not in self.settings[key] or \
                    "type" not in self.settings[key]["provider"]:
                raise RuntimeError(f"No {key} provider set in config")
            #
            provider_config = self.settings[key]["provider"].copy()
            provider_type = provider_config.pop("type")
            #
            provider = importlib.import_module(
                f"pylon.core.providers.{key}.{provider_type}"
            ).Provider(self.context, provider_config)
            #
            if key in internal_providers:
                backend_provider = provider
                #
                provider = importlib.import_module(
                    internal_providers[key]
                ).Provider(self.context, backend_provider)
            #
            provider.init()
            self.providers[key] = provider

    def _deinit_providers(self):
        for key, provider in self.providers.items():
            log.info("Deinitializing %s provider", key)
            try:
                provider.deinit()
            except:  # pylint: disable=W0702
                pass

    @staticmethod
    def activate_loader(loader):
        """ Activate loader """
        sys.meta_path.insert(0, loader)
        importlib.invalidate_caches()
        pkg_resources._initialize_master_working_set()  # pylint: disable=W0212

    @staticmethod
    def activate_path(path):
        """ Activate path """
        sys.path.insert(0, path)
        importlib.invalidate_caches()
        pkg_resources._initialize_master_working_set()  # pylint: disable=W0212

    @staticmethod
    def get_user_site_path(base):
        """ Get site path for specific site base """
        return site.getsitepackages([base])[0]

    def _apply_requirements_overrides(self, module_name, requirements):
        """ Apply requirements overrides for backward compatibility """
        result = requirements.encode()
        #
        if module_name not in PYLON_MODULE_REQUIREMENTS_OVERRIDES:
            return result
        #
        module_overrides = PYLON_MODULE_REQUIREMENTS_OVERRIDES[module_name]
        #
        stripped_requirements = requirements.strip()
        if module_overrides["value_from_stripped"] != stripped_requirements:
            return result
        #
        for mod_name, mod_checks in module_overrides["if_module_requires"].items():
            if mod_name not in self.descriptors:
                return result
            #
            mod_reqs = {}
            #
            for req_obj in pkg_resources.parse_requirements(
                    self.descriptors[mod_name].requirements
            ):
                mod_reqs[req_obj.key] = req_obj
            #
            for check_key, check_value in mod_checks.items():
                if check_key not in mod_reqs:
                    return result
                #
                if check_value not in mod_reqs[check_key]:
                    return result
        #
        log.info("Applying requirements overrides for module: %s", module_name)
        #
        result = module_overrides["value_to"].encode()
        return result

    def install_requirements(  # pylint: disable=R
            self, requirements_path, target_site_base,
            additional_site_paths=None, constraint_paths=None,
            *,
            retries=2, retry_delay=5,
            delete_on_failure=False, module_name=None, provider=None,
        ):
        """ Install requirements into target site """
        cache_dir = self.settings["requirements"].get("cache", "/tmp/pylon_pip_cache")
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except:  # pylint: disable=W0702
            pass
        #
        if constraint_paths is None:
            constraint_paths = []
        #
        environ = os.environ.copy()
        environ["PYTHONUSERBASE"] = target_site_base
        #
        if additional_site_paths is not None:
            environ["PYTHONPATH"] = os.pathsep.join(additional_site_paths)
        #
        target_module = "pip"
        target_args = []
        #
        if self.resolve_settings("requirements.install_via_prefix", False):
            target_args.append("--prefix")
            target_args.append(target_site_base)
            #
            target_module = "pylon.pip"
        else:
            target_args.append("--user")
        #
        c_args = []
        #
        additional_args = self.resolve_settings("requirements.additional_args", [])
        for a_arg in additional_args:
            c_args.append(a_arg)
        #
        for const in constraint_paths:
            c_args.append("-c")
            c_args.append(const)
        #
        if ssl.custom_ca_bundle is not None:
            c_args.append("--cert")
            c_args.append(ssl.custom_ca_bundle)
        #
        trusted_hosts = self.resolve_settings("requirements.trusted_hosts", [])
        for trusted_host in trusted_hosts:
            c_args.append("--trusted-host")
            c_args.append(trusted_host)
        #
        index_url = self.resolve_settings("requirements.index_url", None)
        extra_index_url = self.resolve_settings("requirements.extra_index_url", None)
        no_index = self.resolve_settings("requirements.no_index", False)
        find_links = self.resolve_settings("requirements.find_links", None)
        require_hashes = self.resolve_settings("requirements.require_hashes", False)
        #
        if index_url is not None:
            c_args.append("--index-url")
            c_args.append(index_url)
        #
        if extra_index_url is not None:
            c_args.append("--extra-index-url")
            c_args.append(extra_index_url)
        #
        if no_index:
            c_args.append("--no-index")
        #
        if find_links is not None:
            c_args.append("--find-links")
            c_args.append(find_links)
        #
        if require_hashes:
            c_args.append("--require-hashes")
        #
        for retry in range(retries):
            try:
                return process.run_command(
                    [
                        sys.executable,
                        "-m", target_module, "install",
                    ] + target_args + [
                        "--no-warn-script-location",
                        "--disable-pip-version-check",
                        "--root-user-action=ignore",
                        "--cache-dir", cache_dir,
                    ] + c_args + [
                        "-r", requirements_path,
                    ],
                    env=environ,
                )
            except: # pylint: disable=W0702
                no_more_retries = retry == retries - 1
                if no_more_retries:
                    raise
                #
                log.exception("Failed to install requirements")
                #
                if delete_on_failure and module_name is not None and provider is not None:
                    log.info("Deleting requirements for module: %s", module_name)
                    provider.delete_requirements(module_name, recreate=True)
                #
                log.info("Waiting for retry")
                time.sleep(retry_delay)

    def freeze_site_requirements(
            self, target_site_base, requirements_path=None, additional_site_paths=None
        ):
        """ Get installed requirements (a.k.a pip freeze) """
        cache_dir = self.settings["requirements"].get("cache", "/tmp/pylon_pip_cache")
        #
        environ = os.environ.copy()
        environ["PYTHONUSERBASE"] = target_site_base
        #
        if additional_site_paths is not None:
            environ["PYTHONPATH"] = os.pathsep.join(additional_site_paths)
        #
        target_args = []
        if self.resolve_settings("requirements.install_via_prefix", False):
            target_args.append("--path")
            target_args.append(self.get_user_site_path(target_site_base))
        else:
            target_args.append("--user")
        #
        opt_args = []
        if requirements_path is not None:
            opt_args.append("-r")
            opt_args.append(requirements_path)
        #
        return subprocess.check_output(
            [
                sys.executable,
                "-m", "pip", "freeze",
            ] + target_args + [
                "--disable-pip-version-check",
                "--cache-dir", cache_dir,
            ] + opt_args,
            env=environ,
        ).decode()
