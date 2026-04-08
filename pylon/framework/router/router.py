#!/usr/bin/python3
# coding=utf-8
# pylint: disable=C0415

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

""" Toolkit """

import flask  # pylint: disable=E0401

from pylon.core.tools import log  # pylint: disable=E0611,E0401,W0611
from pylon.core.tools.context import Context  # pylint: disable=E0401


#
# TODO:
# - static files
# - RPC kind with request wrapping
# - register default mode
# - before request - https redirect
# - after request - CORS
# - public routes?
# - API classes?
# - use caller_module_name for template prefixes?
#


# pylint: disable=R0902,R0904
class Router:
    """ Router """

    def __init__(self, context, config, app):
        self.context = context
        self.config = config
        self.app = app
        #
        self.default_mode = self.config.get("default_mode", "default")
        #
        self.registry = {}  # mode -> section -> subsection -> page (all via 'items')
        #
        self.before_request_handler = None
        self.after_request_handler = None
        self.error_handler = None
        #
        self.access_denied_handler = None
        self.not_found_handler = None
        self.bad_request_handler = None
        #
        self.register_hook = None
        self.unregister_hook = None
        #
        self.target_auth_processor = None
        self.target_visibility_processor = None
        #
        self.parameter_processor = None
        self.parameter_enumerator = None
        #
        self.default_template = self.config.get("default_template", "theme:index.html")
        self.default_template_kwargs = self.config.get("default_template_kwargs", {})
        #
        self.slot_template = self.config.get("slot_template", "theme:index.html")
        self.slot_logout_url = self.config.get("slot_logout_url", "#")

    #
    # Replies
    #

    # pylint: disable=W0201,R0912
    def access_denied_reply(self):
        """ Reply """
        if self.access_denied_handler is not None:
            return self.access_denied_handler()
        #
        return "Access Denied", 403

    # pylint: disable=W0201,R0912
    def not_found_reply(self):
        """ Reply """
        if self.config.get("errors_are_access_denied", False):
            return self.access_denied_reply()
        #
        if self.not_found_handler is not None:
            return self.not_found_handler()
        #
        return "Not Found", 404

    # pylint: disable=W0201,R0912
    def bad_request_reply(self):
        """ Reply """
        if self.config.get("errors_are_access_denied", False):
            return self.access_denied_reply()
        #
        if self.bad_request_handler is not None:
            return self.bad_request_handler()
        #
        return "Bad Request", 400

    #
    # Hooks
    #

    def before_request_hook(self):
        """ Hook """
        if self.before_request_handler is not None:
            self.before_request_handler()

    def after_request_hook(self, response):
        """ Hook """
        additional_headers = self.config.get(
            "additional_headers", {}
        )
        for key, value in additional_headers.items():
            response.headers[key] = value
        #
        additional_default_headers = self.config.get(
            "additional_default_headers", {}
        )
        for key, value in additional_default_headers.items():
            if key not in response.headers:
                response.headers[key] = value
        #
        if self.after_request_handler is not None:
            return self.after_request_handler(response)
        #
        return response

    def error_handler_hook(self, error):
        """ Hook """
        if self.error_handler is not None:
            return self.error_handler(error)
        #
        return error

    #
    # Visible entities
    #

    # pylint: disable=E1102
    def visible_modes(self, router_state=None):
        """ Method """
        result = []
        #
        if router_state is None:
            try:
                router_state = flask.g.router
            except:  # pylint: disable=W0702
                router_state = None
        #
        for mode in self.registry.values():
            if self.target_visibility_processor is not None and not \
                    self.target_visibility_processor(mode, router_state):
                continue
            #
            result.append(mode)
        #
        return result

    # pylint: disable=E1102
    def visible_sections(self, mode=None, router_state=None):
        """ Method """
        result = []
        #
        if router_state is None:
            try:
                router_state = flask.g.router
            except:  # pylint: disable=W0702
                router_state = None
        #
        if mode is None and router_state is not None:
            mode = router_state.mode
        #
        if mode is None:
            mode = self.default_mode
        #
        if mode not in self.registry:
            return result
        #
        for section in self.registry[mode].get("items", {}).values():
            if self.target_visibility_processor is not None and not \
                    self.target_visibility_processor(section, router_state):
                continue
            #
            result.append(section)
        #
        return result

    # pylint: disable=E1102
    def visible_subsections(self, mode=None, section=None, router_state=None):
        """ Method """
        result = []
        #
        if router_state is None:
            try:
                router_state = flask.g.router
            except:  # pylint: disable=W0702
                router_state = None
        #
        if mode is None and router_state is not None:
            mode = router_state.mode
        #
        if mode is None:
            mode = self.default_mode
        #
        if mode not in self.registry:
            return result
        #
        if section is None and router_state is not None:
            section = router_state.section
        #
        if section not in self.registry[mode].get("items", {}):
            return result
        #
        for subsection in self.registry[mode]["items"][section].get("items", {}).values():
            if self.target_visibility_processor is not None and not \
                    self.target_visibility_processor(subsection, router_state):
                continue
            #
            result.append(subsection)
        #
        return result

    # pylint: disable=E1102
    def visible_parameters(self, router_state=None):
        """ Method """
        if router_state is None:
            try:
                router_state = flask.g.router
            except:  # pylint: disable=W0702
                router_state = None
        #
        if self.parameter_enumerator is not None:
            return self.parameter_enumerator(router_state)
        #
        return []

    #
    # Registry
    #

    def register_mode(self, key=None, title=None, meta=None, kind="holder", **kind_kwargs):
        """ Registry """
        if key is None:
            key = self.default_mode
        #
        if title is None:
            title = key.capitalize()
        #
        if meta is None:
            meta = {}
        #
        if key in self.registry:
            log.warning("Overriding mode: %s", key)
            #
            items = self.registry[key].get("items", {})
        else:
            items = {}
        #
        item = {
            "key": key,
            "type": "mode",
            "items": items,
            "title": title,
            "kind": kind,
            "kind_kwargs": kind_kwargs.copy(),
        }
        #
        if self.register_hook is not None:
            self.register_hook(item)
        #
        self.registry[key] = item

    def unregister_mode(self, key=None):
        """ Registry """
        self.registry.pop(key, None)

    # pylint: disable=R0913
    def register_section(
            self, mode=None, key=None, title=None, meta=None, kind="holder", **kind_kwargs,
    ):
        """ Registry """
        if key is None:
            raise ValueError("Invalid registration data")
        #
        if title is None:
            title = key.capitalize()
        #
        if meta is None:
            meta = {}
        #
        parent = self.resolve_target(mode=mode)
        #
        if parent is None:
            raise RuntimeError(f"Parent not found: {mode}")
        #
        if key in parent.get("items", {}):
            log.warning("Overriding section: %s:%s", mode, key)
            #
            items = parent["items"][key].get("items", {})
        else:
            items = {}
        #
        item = {
            "key": key,
            "type": "section",
            "items": items,
            "title": title,
            "kind": kind,
            "kind_kwargs": kind_kwargs.copy(),
        }
        #
        if self.register_hook is not None:
            self.register_hook(item)
        #
        if "items" not in parent:
            parent["items"] = {}
        #
        parent["items"][key] = item

    def unregister_section(self, mode=None, key=None):
        """ Registry """
        parent = self.resolve_target(mode=mode)
        #
        if parent is None:
            return
        #
        parent.get("items", {}).pop(key, None)

    # pylint: disable=R0913
    def register_subsection(
            self,
            mode=None, section=None, key=None, title=None, meta=None, kind="holder", **kind_kwargs,
    ):
        """ Registry """
        if key is None:
            raise ValueError("Invalid registration data")
        #
        if title is None:
            title = key.capitalize()
        #
        if meta is None:
            meta = {}
        #
        parent = self.resolve_target(mode=mode, section=section)
        #
        if parent is None:
            raise RuntimeError(f"Parent not found: {mode}:{section}")
        #
        if key in parent.get("items", {}):
            log.warning("Overriding subsection: %s:%s:%s", mode, section, key)
            #
            items = parent["items"][key].get("items", {})
        else:
            items = {}
        #
        item = {
            "key": key,
            "type": "subsection",
            "items": items,
            "title": title,
            "kind": kind,
            "kind_kwargs": kind_kwargs.copy(),
        }
        #
        if self.register_hook is not None:
            self.register_hook(item)
        #
        if "items" not in parent:
            parent["items"] = {}
        #
        parent["items"][key] = item

    def unregister_subsection(self, mode=None, section=None, key=None):
        """ Registry """
        parent = self.resolve_target(mode=mode, section=section)
        #
        if parent is None:
            return
        #
        parent.get("items", {}).pop(key, None)

    # pylint: disable=R0913
    def register_page(
            self,
            mode=None, section=None, subsection=None,
            key=None, title=None, meta=None, kind="holder", **kind_kwargs,
    ):
        """ Registry """
        if key is None:
            raise ValueError("Invalid registration data")
        #
        if title is None:
            title = key.capitalize()
        #
        if meta is None:
            meta = {}
        #
        parent = self.resolve_target(mode=mode, section=section, subsection=subsection)
        #
        if parent is None:
            raise RuntimeError(f"Parent not found: {mode}:{section}:{subsection}")
        #
        if key in parent.get("items", {}):
            log.warning("Overriding page: %s:%s:%s:%s", mode, section, subsection, key)
            #
            items = parent["items"][key].get("items", {})
        else:
            items = {}
        #
        item = {
            "key": key,
            "type": "page",
            "items": items,
            "title": title,
            "kind": kind,
            "kind_kwargs": kind_kwargs.copy(),
        }
        #
        if self.register_hook is not None:
            self.register_hook(item)
        #
        if "items" not in parent:
            parent["items"] = {}
        #
        parent["items"][key] = item

    def unregister_page(self, mode=None, section=None, subsection=None, key=None):
        """ Registry """
        parent = self.resolve_target(mode=mode, section=section, subsection=subsection)
        #
        if parent is None:
            return
        #
        parent.get("items", {}).pop(key, None)

    #
    # Target routing
    #

    # pylint: disable=W0201,R0912
    def resolve_target(self, mode=None, section=None, subsection=None, page=None):
        """ Method """
        if mode is None:
            mode = self.default_mode
        #
        if mode not in self.registry:
            return None
        #
        target = self.registry[mode]
        items = [section, subsection, page, None]
        #
        for item in items:
            if item is None:
                return target
            #
            if item not in target.get("items", {}):
                return None
            #
            target = target["items"][item]
        #
        return None

    # pylint: disable=R0911,R0913,R0915
    def make_url(
            self,
            mode=..., section=..., subsection=..., page=...,
            parameter=...,
            slash=...,
            router_state=None,
    ):
        """ Method """
        if router_state is None:
            try:
                router_state = flask.g.router
            except:  # pylint: disable=W0702
                router_state = None
        #
        if mode is ... and router_state is not None:
            mode = router_state.raw_mode
        #
        if section is ... and router_state is not None:
            section = router_state.section
        #
        if subsection is ... and router_state is not None:
            subsection = router_state.subsection
        #
        if page is ... and router_state is not None:
            page = router_state.page
        #
        if parameter is ... and router_state is not None:
            parameter = router_state.parameter
        #
        if slash is ... and router_state is not None:
            slash = router_state.slash
        #
        return self.make_target_url(
            mode=mode, section=section, subsection=subsection, page=page,
            parameter=parameter,
            slash=slash,
        )

    # pylint: disable=R0911,R0913,R0915
    def make_target_url(
            self,
            mode=None, section=None, subsection=None, page=None,
            parameter=None,
            slash=None,
    ):
        """ Method """
        mode_none = mode is None
        section_none = section is None
        subsection_none = subsection is None
        page_none = page is None
        parameter_none = parameter is None
        #
        if slash is None:
            slash = page_none
        #
        if not parameter_none and slash and not parameter.endswith("/"):
            parameter = f"{parameter}/"
        #
        # Index
        #
        if mode_none and section_none and subsection_none and page_none:
            if parameter_none:
                return flask.url_for("route_index")
            #
            return flask.url_for(
                "route_index_parameter",
                parameter=parameter,
            )
        #
        # Mode
        #
        if section_none and subsection_none and page_none:
            if parameter_none:
                if slash:
                    endpoint = "route_mode_index_slash"
                else:
                    endpoint = "route_mode_index"
                #
                return flask.url_for(
                    endpoint,
                    mode=mode,
                )
            #
            return flask.url_for(
                "route_mode_index_parameter",
                mode=mode,
                parameter=parameter,
            )
        #
        # Section
        #
        if subsection_none and page_none:
            if mode_none:
                if parameter_none:
                    if slash:
                        endpoint = "route_section_slash"
                    else:
                        endpoint = "route_section"
                    #
                    return flask.url_for(
                        endpoint,
                        section=section,
                    )
                #
                return flask.url_for(
                    "route_section_parameter",
                    section=section,
                    parameter=parameter,
                )
            #
            if parameter_none:
                if slash:
                    endpoint = "route_mode_section_slash"
                else:
                    endpoint = "route_mode_section"
                #
                return flask.url_for(
                    endpoint,
                    mode=mode,
                    section=section,
                )
            #
            return flask.url_for(
                "route_mode_section_parameter",
                mode=mode,
                section=section,
                parameter=parameter,
            )
        #
        # Subsection
        #
        if page_none:
            if mode_none:
                if parameter_none:
                    if slash:
                        endpoint = "route_subsection_slash"
                    else:
                        endpoint = "route_subsection"
                    #
                    return flask.url_for(
                        endpoint,
                        section=section,
                        subsection=subsection,
                    )
                #
                return flask.url_for(
                    "route_subsection_parameter",
                    section=section,
                    subsection=subsection,
                    parameter=parameter,
                )
            #
            if parameter_none:
                if slash:
                    endpoint = "route_mode_subsection_slash"
                else:
                    endpoint = "route_mode_subsection"
                #
                return flask.url_for(
                    endpoint,
                    mode=mode,
                    section=section,
                    subsection=subsection,
                )
            #
            return flask.url_for(
                "route_mode_subsection_parameter",
                mode=mode,
                section=section,
                subsection=subsection,
                parameter=parameter,
            )
        #
        # Page
        #
        if mode_none:
            if parameter_none:
                if slash:
                    endpoint = "route_page_slash"
                else:
                    endpoint = "route_page"
                #
                return flask.url_for(
                    endpoint,
                    section=section,
                    subsection=subsection,
                    page=page,
                )
            #
            return flask.url_for(
                "route_page_parameter",
                section=section,
                subsection=subsection,
                page=page,
                parameter=parameter,
            )
        #
        if parameter_none:
            if slash:
                endpoint = "route_mode_page_slash"
            else:
                endpoint = "route_mode_page"
            #
            return flask.url_for(
                endpoint,
                mode=mode,
                section=section,
                subsection=subsection,
                page=page,
            )
        #
        return flask.url_for(
            "route_mode_page_parameter",
            mode=mode,
            section=section,
            subsection=subsection,
            page=page,
            parameter=parameter,
        )

    # pylint: disable=R0911,R0913,R0914,E1102
    def route(self, mode, section, subsection, page, parameter, slash):
        """ Router route """
        router_state = Context()
        #
        if slash is None and parameter:
            slash = parameter.endswith("/")
        #
        if slash is None:
            slash = flask.request.base_url.endswith("/")
        #
        if parameter:
            parameter = parameter.rstrip("/")
        #
        router_state.raw_mode = mode
        #
        if mode is None:
            mode = self.default_mode
        #
        router_state.mode = mode
        router_state.section = section
        router_state.subsection = subsection
        router_state.page = page
        router_state.parameter = parameter
        router_state.slash = slash
        #
        flask.g.router = router_state
        #
        target = self.resolve_target(mode, section, subsection, page)
        #
        if target is None:
            return self.not_found_reply()
        #
        if self.parameter_processor is not None and not \
                self.parameter_processor(target, router_state):
            return self.bad_request_reply()
        #
        if self.target_auth_processor is not None and not \
                self.target_auth_processor(target, router_state):
            return self.access_denied_reply()
        #
        flask.g.router.target = target
        #
        target_type = target.get("type", None)
        target_kind = target.get("kind", None)
        #
        if target_kind == "redirect" and "url" in target.get("kind_kwargs", {}):
            target_url = target["kind_kwargs"]["url"]
            return flask.redirect(target_url)
        #
        if target_kind == "route" and "route" in target.get("kind_kwargs", {}):
            target_route = target["kind_kwargs"]["route"]
            target_kwargs = target["kind_kwargs"].get("route_kwargs", {})
            return flask.redirect(flask.url_for(target_route, **target_kwargs))
        #
        if target_kind == "method" and "method" in target.get("kind_kwargs", {}):
            target_method = target["kind_kwargs"]["method"]
            target_kwargs = target["kind_kwargs"].get("method_kwargs", {})
            return target_method(**target_kwargs)
        #
        if target_kind == "template":
            kind_kwargs = target.get("kind_kwargs", {})
            # TODO: resolve template from hierarchy
            target_template = kind_kwargs.get("template", self.default_template)
            target_kwargs = self.default_template_kwargs.copy()
            target_kwargs.update(kind_kwargs.get("template_kwargs", {}))
            return flask.render_template(target_template, **target_kwargs)
        #
        if target_kind == "holder":
            if target_type == "mode":
                visible_sections = self.visible_sections(mode=mode)
                if visible_sections:
                    target_url = self.make_target_url(
                        mode=router_state.raw_mode,
                        section=visible_sections[0]["key"],
                        subsection=None,
                        page=None,
                        parameter=parameter,
                        slash=slash,
                    )
                    return flask.redirect(target_url)
            #
            if target_type == "section":
                visible_subsections = self.visible_subsections(mode=mode, section=section)
                if visible_subsections:
                    target_url = self.make_target_url(
                        mode=router_state.raw_mode,
                        section=section,
                        subsection=visible_subsections[0]["key"],
                        page=None,
                        parameter=parameter,
                        slash=slash,
                    )
                    return flask.redirect(target_url)
        #
        if target_kind == "slot":
            location_key = "unknown"
            prefix_parts = []
            #
            for item in [mode, section, subsection, page]:
                if item is None:
                    break
                #
                location_key = item
                prefix_parts.append(f"{item}_")
            #
            default_prefix = "".join(prefix_parts)
            default_title = location_key.capitalize()
            #
            return flask.render_template(
                self.slot_template,
                logout_url=self.slot_logout_url,
                prefix=target.get("kind_kwargs", {}).get("prefix", default_prefix),
                title=target.get("title", default_title),
            )
        #
        return self.not_found_reply()  # unknown kind
