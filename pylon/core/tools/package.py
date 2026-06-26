#!/usr/bin/python3
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

"""
    Package tools
"""


import warnings

warnings.filterwarnings(
    action="ignore",
    message="pkg_resources.*"
)

import pkg_resources



def collect_runtime_versions(context):
    """ Collect runtime versions """
    # Get pylon version + requirements
    try:
        pylon_pkg = pkg_resources.require("pylon")[0]
        context.pylon_requirements = "\n".join(str(req) for req in pylon_pkg.requires())
        context.pylon_version = pylon_pkg.version
    except:  # pylint: disable=W0702
        context.pylon_requirements = ""
        context.pylon_version = "unknown"
    # Get arbiter version
    try:
        context.arbiter_version = pkg_resources.require("arbiter")[0].version
    except:  # pylint: disable=W0702
        context.arbiter_version = "unknown"
