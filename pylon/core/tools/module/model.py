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


class ModuleModel:
    """ Module model """

    def __init__(self, context, descriptor):
        self.context = context
        self.descriptor = descriptor

    def init(self):
        """ Initialize module """
        self.descriptor.init_all()

    def deinit(self):
        """ De-initialize module """
        self.descriptor.deinit_all()

    def ready(self):
        """ Ready callback """

    def unready(self):
        """ Unready callback """

    def reconfig(self):
        """ Re-config module """
        self.descriptor.load_config()

    def preload(self):
        """ Preload handler """

    def install(self):
        """ Install handler """
        self.descriptor.install_all()

    # def uninstall(self):
    #     """ Uninstall handler """

    # def upgrade(self, version_from):
    #     """ Upgrade handler """

    # def downgrade(self, version_to):
    #     """ Downgrade handler """

    # def backup(self, backup_path):
    #     """ Backup util """

    # def restore(self, backup_path):
    #     """ Restore util """
