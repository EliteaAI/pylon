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

""" Server """

import urllib
import logging
import datetime


class LoggingMiddleware:  # pylint: disable=R0903
    """ Log requests """

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        #
        def log_and_start_response(status, headers, *args, **kwargs):
            request_uri = urllib.parse.quote(
                f'{environ.get("SCRIPT_NAME", "")}{environ.get("PATH_INFO", "")}'
            )
            if "QUERY_STRING" in environ and environ["QUERY_STRING"]:
                request_uri = f'{request_uri}?{environ["QUERY_STRING"]}'
            #
            response_size = "-"
            for key, value in headers:
                if key.lower() == "content-length":
                    response_size = str(value)
                    break
            #
            logger = logging.getLogger("server")
            logger.info(
                '%s - - [%s] "%s %s %s" %s %s',
                environ.get("REMOTE_ADDR", "-"),
                datetime.datetime.now().strftime("%d/%b/%Y %H:%M:%S"),
                environ.get("REQUEST_METHOD", "-"),
                request_uri,
                environ.get("SERVER_PROTOCOL", "-"),
                status.split(None, 1)[0],
                response_size,
            )
            #
            return start_response(status, headers, *args, **kwargs)
        #
        return self.app(environ, log_and_start_response)
