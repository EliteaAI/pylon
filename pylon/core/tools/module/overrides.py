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

""" Overrides """


PYLON_MODULE_REQUIREMENTS_OVERRIDES = {
    "prompt_lib": {
        "value_from_stripped": "langchain-openai==0.0.8",
        "value_to": "langchain-openai==0.0.8\npydantic==1.10.11\n",
        "if_module_requires": {
            "shared": {
                "pydantic": "1.10.11",
            }
        }
    }
}
