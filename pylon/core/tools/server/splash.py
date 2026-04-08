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

""" Splash """

import flask  # pylint: disable=E0401

from pylon.core.tools import config


DEFAULT_SPLASH_TEMPLATE = b"""
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <style>
      html, body {
        height: 100%;
        margin: 0px;
      }
      body {
        margin: 0px;
        color: rgb(169, 183, 193);
        font-family: Montserrat, Roboto, Arial, sans-serif;
        font-weight: 400;
        font-size: 1rem;
        line-height: 1.5;
        background-color: rgb(14, 19, 29);
        display: flex;
        align-items: center;
        justify-content: center;
      }
      #container {
        border: 1px solid #ccc;
        border-radius: 16px;
        padding: 5%;
        background-color: rgb(9, 12, 23);
      }
    </style>
    <title>Pylon - Booting</title>
  </head>
  <body>
    <div id="container">
      <span>Engine is starting, please wait...</span>
      <br>
      <span>This page will refresh in: <span id="counter">30</span> seconds</span>
    </div>
    <script>
      let count = 30;
      const counter = document.getElementById("counter");
      const timer = setInterval(() => {
        count--;
        counter.textContent = count;
        if (count <= 0) {
          clearInterval(timer);
          window.location.reload();
        }
      }, 1000);
    </script>
  </body>
</html>
"""


def boot_splash_hook(router, environ, _start_response):
    """ Router hook """
    # Construct request
    req = flask.Request(environ)
    # Collect data
    source_uri = req.full_path
    if not req.query_string and source_uri.endswith("?"):
        source_uri = source_uri[:-1]
    #
    for endpoint in ["healthz", "livez", "readyz"]:
        if source_uri.startswith(f"/{endpoint}") and f"/{endpoint}/" in router.map:
            return None
    #
    return boot_splash_app


def boot_splash_app(_environ, start_response):
    """ Splash app """
    splash_template = config.tunable_get("splash_template", DEFAULT_SPLASH_TEMPLATE)
    #
    start_response("503 Service Unavailable", [
        ("Content-type", "text/html; charset=utf-8"),
        ("Cache-Control", "no-store, no-cache, max-age=0, must-revalidate, proxy-revalidate"),
        ("Expires", "0"),
        ("Refresh", "30"),
        ("Retry-After", "30"),
    ])
    #
    return [splash_template.strip()]
