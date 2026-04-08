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

""" MetadataProvider """

import os
import json
import shutil
import tempfile

from pylon.core.tools import git

from . import MetadataProviderModel


class Provider(MetadataProviderModel):  # pylint: disable=R0902
    """ Provider model """

    def __init__(self, context, settings):
        self.context = context
        self.settings = settings
        #
        self.file = self.settings.get("file", "metadata.json")
        self.branch = self.settings.get("branch", "main")
        self.depth = self.settings.get("depth", 1)
        self.delete_git_dir = self.settings.get("delete_git_dir", True)
        self.username = self.settings.get("username", None)
        self.password = self.settings.get("password", None)
        self.key_filename = self.settings.get("key_filename", None)
        self.key_data = self.settings.get("key_data", None)
        self.add_source_data = self.settings.get("add_source_data", False)
        self.add_head_data = self.settings.get("add_head_data", False)

    def init(self):
        """ Initialize provider """

    def deinit(self):
        """ De-initialize provider """

    def get_metadata(self, target):
        """ Get plugin metadata """
        target_path = tempfile.mkdtemp()
        #
        try:
            _, head_data = git.clone(
                target.get("source"),
                target_path,
                target.get("branch", self.branch),
                target.get("depth", self.depth),
                target.get("delete_git_dir", self.delete_git_dir),
                target.get("username", self.username),
                target.get("password", self.password),
                target.get("key_filename", self.key_filename),
                target.get("key_data", self.key_data),
                return_head_data=True,
            )
            #
            with open(os.path.join(target_path, target.get("file", self.file)), "rb") as file:
                result = json.load(file)
            #
            if target.get("add_source_data", self.add_source_data):
                result["git_source"] = target.get("source")
            #
            if target.get("add_head_data", self.add_head_data):
                result["git_head"] = head_data
        finally:
            shutil.rmtree(target_path)
        #
        return result

    def get_multiple_metadata(self, targets):
        """ Get plugins metadata """
        result = []
        #
        for target in targets:
            result.append(self.get_metadata(target))
        #
        return result
