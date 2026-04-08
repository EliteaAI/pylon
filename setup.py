#!/usr/bin/python3
# coding=utf-8
# pylint: disable=C0103,C0413

#  Copyright (c) 2026 EPAM Systems
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""
    Setup script
"""

with open("requirements.txt", "r", encoding="utf-8") as f:
    required = f.read().splitlines()

with open("version.txt", "r", encoding="utf-8") as f:
    version = f.read().splitlines()[0].strip()

try:
    import subprocess
    tag = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"])
    version = f"{version}+git.{tag.decode('utf-8').strip()}"
except:  # pylint: disable=W0702
    pass

from setuptools import setup, find_packages

setup(
    name="pylon",
    version=version,
    description="Core for plugin-based applications",
    long_description="Application framework with built-in tools for distributed workloads",
    url="https://getcarrier.io",
    license="Apache License 2.0",
    author="LifeDJIK, arozumenko",
    author_email="ivan_krakhmaliuk@epam.com, artem_rozumenko@epam.com",
    packages=find_packages(),
    install_requires=required
)
