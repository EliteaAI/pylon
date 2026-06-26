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
    Seed tools
"""

import os
import importlib

import yaml  # pylint: disable=E0401

from pylon.core.tools import log
from pylon.core.tools import env
from pylon.core.tools import config

from pylon.core.tools.dict import recursive_merge


def load_settings(return_data_first=False):
    """ Load settings from seed from env """
    settings_seed = env.get_var("CONFIG_SEED", None)
    #
    return load_settings_from_seed(settings_seed, return_data_first=return_data_first)


def load_settings_from_seed(settings_seed, return_data_first=False):
    """ Load settings from seed """
    if not settings_seed or ":" not in settings_seed:
        if return_data_first:
            return None, None
        #
        return None
    #
    settings_seed_tag = settings_seed[:settings_seed.find(":")]
    settings_seed_data = settings_seed[len(settings_seed_tag) + 1:]
    try:
        seed = importlib.import_module(f"pylon.core.seeds.{settings_seed_tag}")
        settings_data = seed.unseed(settings_seed_data)
    except:  # pylint: disable=W0702
        log.exception("Failed to unseed settings")
    #
    if not settings_data:
        if return_data_first:
            return None, None
        #
        return None
    #
    if return_data_first:
        return settings_data, parse_settings(settings_data)
    #
    return parse_settings(settings_data)


def parse_settings(settings_data):
    """ Parse settings from data """
    try:
        settings = yaml.load(config.env_vars_expansion(settings_data), Loader=yaml.SafeLoader)
        settings = config.config_substitution(settings, config.vault_secrets(settings))
    except:  # pylint: disable=W0702
        log.exception("Failed to parse settings")
        return None
    #
    return settings


def apply_tunable_settings(context):
    """ Apply tunable settings """
    tunable_settings_data = config.tunable_get("pylon_settings", None)
    if tunable_settings_data is not None:
        log.info("Loading and parsing tunable settings")
        tunable_settings = parse_settings(tunable_settings_data)
        if tunable_settings:
            context.settings_data = tunable_settings_data
            tunable_settings_mode = tunable_settings.get("pylon", {}).get(
                "tunable_settings_mode", "override"
            )
            #
            if tunable_settings_mode == "merge":
                context.settings = recursive_merge(context.settings, tunable_settings)
            elif tunable_settings_mode == "update":
                context.settings.update(tunable_settings)
            else:
                context.settings = tunable_settings
