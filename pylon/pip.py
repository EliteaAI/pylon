#!/usr/bin/python3
# coding=utf-8
# pylint: disable=C0411,C0412,C0413,C0415,W0212

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
    Patched "pip" entry point
"""

import os
import sys


def main():  # pylint: disable=R0912,R0914,R0915
    """ Entry point """
    # Patch
    import pip._internal.req.req_uninstall
    pip._internal.req.req_uninstall.UninstallPathSet._permitted = \
        patched_pip_req_uninstall_permitted(
            pip._internal.req.req_uninstall.UninstallPathSet._permitted
        )
    # Run
    import pip.__main__  # pylint: disable=W0611
    from pip._internal.cli.main import main as _main
    sys.exit(_main())


def patched_pip_req_uninstall_permitted(original_pip_req_uninstall_permitted):
    """ Allow only inside PYTHONUSERBASE """
    def patched_function(self, path):
        if "PYTHONUSERBASE" not in os.environ:
            return original_pip_req_uninstall_permitted(self, path)
        #
        return path.startswith(self._normalize_path_cached(os.environ["PYTHONUSERBASE"]))
    #
    return patched_function


if __name__ == "__main__":
    # Call entry point
    main()
