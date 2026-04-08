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

import sys

from pylon.core.tools import log


def init(context):
    """ Init """
    # Config
    framework_config = context.settings.get("framework", {})
    # Router
    router_config = framework_config.get("router", {})
    #
    if router_config.get("enabled", True):
        log.info("Creating router instance")
        #
        from .router import Router
        #
        router_app = context.app_manager.make_app_instance("pylon.framework.router")
        context.router = Router(context, router_config, router_app)
        #
        add_router_routes(context.router)
        context.app_router.map["/"] = router_app
        #
        if router_config.get("enable_context_processor_tools", True):
            import tools  # pylint: disable=E0401
            context.app.context_processor(lambda: {"tools": tools})
        #
        if router_config.get("enable_before_request_hook", True):
            context.app.before_request(context.router.before_request_hook)
        #
        if router_config.get("enable_after_request_hook", True):
            context.app.after_request(context.router.after_request_hook)
        #
        if router_config.get("enable_error_handler_hook", True):
            context.app.errorhandler(Exception)(context.router.error_handler_hook)
        #
        setattr(sys.modules["tools"], "router", context.router)


def add_router_routes(router):
    """ Init routes """
    #
    # Index
    #
    router.app.add_url_rule(
        rule="/",
        endpoint="route_index",
        view_func=router.route,
        defaults={
            "mode": None,
            "parameter": None,
            "section": None,
            "subsection": None,
            "page": None,
            "slash": True,
        },
    )
    #
    router.app.add_url_rule(
        rule="/~/<mode>",
        endpoint="route_mode_index",
        view_func=router.route,
        defaults={
            "parameter": None,
            "section": None,
            "subsection": None,
            "page": None,
            "slash": False,
        },
    )
    router.app.add_url_rule(
        rule="/~/<mode>/",
        endpoint="route_mode_index_slash",
        view_func=router.route,
        defaults={
            "parameter": None,
            "section": None,
            "subsection": None,
            "page": None,
            "slash": True,
        },
    )
    #
    router.app.add_url_rule(
        rule="/@/<path:parameter>",
        endpoint="route_index_parameter",
        view_func=router.route,
        defaults={
            "mode": None,
            "section": None,
            "subsection": None,
            "page": None,
            "slash": None,
        },
    )
    #
    router.app.add_url_rule(
        rule="/~/<mode>/@/<path:parameter>",
        endpoint="route_mode_index_parameter",
        view_func=router.route,
        defaults={
            "section": None,
            "subsection": None,
            "page": None,
            "slash": None,
        },
    )
    #
    # Section
    #
    router.app.add_url_rule(
        rule="/-/<section>",
        endpoint="route_section",
        view_func=router.route,
        defaults={
            "mode": None,
            "parameter": None,
            "subsection": None,
            "page": None,
            "slash": False,
        },
    )
    router.app.add_url_rule(
        rule="/-/<section>/",
        endpoint="route_section_slash",
        view_func=router.route,
        defaults={
            "mode": None,
            "parameter": None,
            "subsection": None,
            "page": None,
            "slash": True,
        },
    )
    #
    router.app.add_url_rule(
        rule="/~/<mode>/-/<section>",
        endpoint="route_mode_section",
        view_func=router.route,
        defaults={
            "parameter": None,
            "subsection": None,
            "page": None,
            "slash": False,
        },
    )
    router.app.add_url_rule(
        rule="/~/<mode>/-/<section>/",
        endpoint="route_mode_section_slash",
        view_func=router.route,
        defaults={
            "parameter": None,
            "subsection": None,
            "page": None,
            "slash": True,
        },
    )
    #
    router.app.add_url_rule(
        rule="/-/<section>/@/<path:parameter>",
        endpoint="route_section_parameter",
        view_func=router.route,
        defaults={
            "mode": None,
            "subsection": None,
            "page": None,
            "slash": None,
        },
    )
    #
    router.app.add_url_rule(
        rule="/~/<mode>/-/<section>/@/<path:parameter>",
        endpoint="route_mode_section_parameter",
        view_func=router.route,
        defaults={
            "subsection": None,
            "page": None,
            "slash": None,
        },
    )
    #
    # Subsection
    #
    router.app.add_url_rule(
        rule="/-/<section>/<subsection>",
        endpoint="route_subsection",
        view_func=router.route,
        defaults={
            "mode": None,
            "parameter": None,
            "page": None,
            "slash": False,
        },
    )
    router.app.add_url_rule(
        rule="/-/<section>/<subsection>/",
        endpoint="route_subsection_slash",
        view_func=router.route,
        defaults={
            "mode": None,
            "parameter": None,
            "page": None,
            "slash": True,
        },
    )
    #
    router.app.add_url_rule(
        rule="/~/<mode>/-/<section>/<subsection>",
        endpoint="route_mode_subsection",
        view_func=router.route,
        defaults={
            "parameter": None,
            "page": None,
            "slash": False,
        },
    )
    router.app.add_url_rule(
        rule="/~/<mode>/-/<section>/<subsection>/",
        endpoint="route_mode_subsection_slash",
        view_func=router.route,
        defaults={
            "parameter": None,
            "page": None,
            "slash": True,
        },
    )
    #
    router.app.add_url_rule(
        rule="/-/<section>/<subsection>/@/<path:parameter>",
        endpoint="route_subsection_parameter",
        view_func=router.route,
        defaults={
            "mode": None,
            "page": None,
            "slash": None,
        },
    )
    #
    router.app.add_url_rule(
        rule="/~/<mode>/-/<section>/<subsection>/@/<path:parameter>",
        endpoint="route_mode_subsection_parameter",
        view_func=router.route,
        defaults={
            "page": None,
            "slash": None,
        },
    )
    #
    # Page
    #
    router.app.add_url_rule(
        rule="/-/<section>/<subsection>/<page>",
        endpoint="route_page",
        view_func=router.route,
        defaults={
            "mode": None,
            "parameter": None,
            "slash": False,
        },
    )
    router.app.add_url_rule(
        rule="/-/<section>/<subsection>/<page>/",
        endpoint="route_page_slash",
        view_func=router.route,
        defaults={
            "mode": None,
            "parameter": None,
            "slash": True,
        },
    )
    #
    router.app.add_url_rule(
        rule="/~/<mode>/-/<section>/<subsection>/<page>",
        endpoint="route_mode_page",
        view_func=router.route,
        defaults={
            "parameter": None,
            "slash": False,
        },
    )
    router.app.add_url_rule(
        rule="/~/<mode>/-/<section>/<subsection>/<page>/",
        endpoint="route_mode_page_slash",
        view_func=router.route,
        defaults={
            "parameter": None,
            "slash": True,
        },
    )
    #
    router.app.add_url_rule(
        rule="/-/<section>/<subsection>/<page>/@/<path:parameter>",
        endpoint="route_page_parameter",
        view_func=router.route,
        defaults={
            "mode": None,
            "slash": None,
        },
    )
    #
    router.app.add_url_rule(
        rule="/~/<mode>/-/<section>/<subsection>/<page>/@/<path:parameter>",
        endpoint="route_mode_page_parameter",
        view_func=router.route,
        defaults={
            "slash": None,
        },
    )
