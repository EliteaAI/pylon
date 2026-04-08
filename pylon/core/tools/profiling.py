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

""" Profiling """

import io
import pstats
import cProfile

from pylon.core.tools import log


def profiling_enabled(context, stage):
    """ Check if profiling is enabled for the stage """
    if stage.startswith("module:init:"):
        return context.profiling.get("stage", {}).get("module_inits", False)
    #
    if stage.startswith("module:deinit:"):
        return context.profiling.get("stage", {}).get("module_deinits", False)
    #
    return context.profiling.get("stage", {}).get(stage, False)


def profiling_start(context, stage):
    """ Enter profiling stage """
    if profiling_enabled(context, stage):
        log.info("Enabling profiling: %s", stage)
        #
        if "profile" not in context.profiling:
            context.profiling["profile"] = {}
        #
        context.profiling["profile"][stage] = cProfile.Profile()
        context.profiling["profile"][stage].enable()


def profiling_stop(context, stage):
    """ Exit profiling stage """
    if profiling_enabled(context, stage):
        context.profiling["profile"][stage].disable()
        #
        stats_stream = io.StringIO()
        #
        profile_stats = pstats.Stats(context.profiling["profile"][stage], stream=stats_stream)
        profile_stats.sort_stats(
            *context.profiling.get("sort", [
                pstats.SortKey.FILENAME, pstats.SortKey.NAME, pstats.SortKey.LINE,
            ]),
        )
        profile_stats.print_stats()
        #
        log.info("Profile stats: %s\n%s", stage, stats_stream.getvalue())
