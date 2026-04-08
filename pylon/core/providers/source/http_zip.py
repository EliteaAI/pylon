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

""" SourceProvider """

import io
import os
import zipfile
import tempfile

import requests  # pylint: disable=E0401

from . import SourceProviderModel


class Provider(SourceProviderModel):  # pylint: disable=R0902
    """ Provider model """

    def __init__(self, context, settings):
        self.context = context
        self.settings = settings
        #
        self.username = self.settings.get("username", None)
        self.password = self.settings.get("password", None)
        self.verify = self.settings.get("verify", True)
        #
        self.use_inner_dir = self.settings.get("use_inner_dir", True)

    def init(self):
        """ Initialize provider """

    def deinit(self):
        """ De-initialize provider """

    def get_source(self, target):
        """ Get plugin source """
        target_path = tempfile.mkdtemp()
        self.context.module_manager.temporary_objects.append(target_path)
        #
        username = target.get("username", self.username)
        password = target.get("password", self.password)
        verify = target.get("verify", self.verify)
        #
        auth = None
        if username is not None and password is not None:
            auth = (username, password)
        #
        response = requests.get(
            target.get("source"),
            auth=auth,
            verify=verify,
        )
        #
        with zipfile.ZipFile(io.BytesIO(response.content)) as zip_file:
            zip_file.extractall(target_path)
        #
        if self.use_inner_dir:
            return os.path.join(target_path, os.listdir(target_path)[0])
        #
        return target_path

    def get_multiple_source(self, targets):
        """ Get plugins source """
        result = []
        #
        for target in targets:
            result.append(self.get_source(target))
        #
        return result
