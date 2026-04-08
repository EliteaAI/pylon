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

import inspect
import threading

from pylon.core.tools import db_support
from pylon.core.tools.context import Context


def caller_module_name(skip=0):
    """ Find closest caller module """
    module_name = None
    #
    to_skip = skip
    frame = None
    #
    try:
        frame = inspect.currentframe()
        #
        while frame:
            caller_module = frame.f_globals["__name__"]
            #
            if caller_module.startswith("plugins.") and to_skip <= 0:
                module_name = caller_module.split(".")[1]
                break
            #
            to_skip -= 1
            frame = frame.f_back
    finally:
        if frame is not None:
            del frame
    #
    return module_name


class This:  # pylint: disable=R0903
    """ Module-specific tools/helpers """

    def __init__(self, context):
        self.__context = context
        self.__modules = {}
        self.__spaces = {}
        self.__lock = threading.Lock()

    def __getattr__(self, name):
        module_name = caller_module_name()
        #
        if module_name is None:
            raise RuntimeError("Caller is not a pylon module")
        #
        exact = self.for_module(module_name)
        return getattr(exact, name)

    def for_module(self, name, recreate=False):
        """ Get exact for known module name """
        with self.__lock:
            if name not in self.__modules or recreate:
                self.__modules[name] = ModuleThis(self.__context, self.__spaces, name)
        #
        return self.__modules[name]


class ModuleThis:  # pylint: disable=R0903
    """ Exact module-specific tools/helpers """

    def __init__(self, context, spaces, module_name):
        self.context = context
        self.spaces = spaces
        self.module_name = module_name
        #
        self.descriptor = self.context.module_manager.descriptors[self.module_name]
        #
        self.data = Context()
        self.lock = threading.Lock()
        #
        self.db = db_support.make_module_entities(self.context, self.module_name, self.spaces)

    @property
    def module(self):
        """ Get module instance """
        return self.descriptor.module
