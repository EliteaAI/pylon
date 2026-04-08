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
    Process tools
"""

import os
import shlex
import signal
import subprocess
import sys
import threading
import time

from pylon.core.tools import log


def run_command(*args, **kwargs):
    """ Run command and log output """
    target_kwargs = kwargs.copy()
    for key in ["stdout", "stderr"]:
        if key in target_kwargs:
            target_kwargs.pop(key)
    #
    with subprocess.Popen(
        *args, **target_kwargs,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as proc:
        #
        while proc.poll() is None:
            while True:
                line = proc.stdout.readline().decode().strip()
                #
                if not line:
                    break
                #
                log.info(line)
        #
        if proc.returncode != 0:
            raise RuntimeError(f"Command failed, return code={proc.returncode}")


def start_subpylons(context):
    """ Start sub-pylons """
    context.subpylons = []
    #
    subpylon_configs = context.settings.get("subpylons", [])
    #
    for subpylon_config in subpylon_configs:
        instance = SubpylonInstance(context, subpylon_config)
        context.subpylons.append(instance)
        instance.start()


def stop_subpylons(context):
    """ Stop sub-pylons """
    while context.subpylons:
        instance = context.subpylons.pop(0)
        instance.stop()


class SubpylonInstance:
    """ Subpylon """

    def __init__(self, context, config):
        self.context = context
        self.config = config
        #
        self.name = self.config.get("name", "subpylon")
        self.cwd = self.config.get("cwd", os.getcwd())
        self.restart_delay = float(self.config.get("restart_delay", 1.0))
        self.stop_timeout = float(self.config.get("stop_timeout", 10.0))
        self.oneshot_wait = float(self.config.get("oneshot_wait", 60.0 * 15))
        #
        self.process = None
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._monitor_thread = None

    def _build_command(self):
        command = self.config.get("command", None)
        args = self.config.get("args", [])
        #
        if command is None:
            command = [sys.executable, "-m", "pylon.main"]
        elif isinstance(command, str):
            command = shlex.split(command)
        else:
            command = list(command)
        #
        if isinstance(args, str):
            args = shlex.split(args)
        #
        return command + list(args)

    def _build_env(self):
        target_env = os.environ.copy()
        target_env.update(self.config.get("env", {}))
        return target_env

    def _should_restart(self):
        if self.config.get("restart") is not None:
            return bool(self.config.get("restart"))
        #
        return self.context.server_mode != "oneshot"

    def _set_external_pid(self, pid, present):
        if not hasattr(self.context, "zombie_reaper"):
            return
        #
        if present:
            self.context.zombie_reaper.external_pids.add(pid)
        else:
            self.context.zombie_reaper.external_pids.discard(pid)

    def _spawn_once(self):
        command = self._build_command()
        target_env = self._build_env()
        #
        log.info("Starting %s: %s", self.name, " ".join(command))
        #
        process = subprocess.Popen(  # pylint: disable=R1732
            command,
            cwd=self.cwd,
            env=target_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        #
        with self._state_lock:
            self.process = process

        self._set_external_pid(process.pid, True)
        #
        try:
            while not self._stop_event.is_set():
                line = process.stdout.readline()
                if not line:
                    break
                #
                log.info("[%s] %s", self.name, line.decode(errors="replace").rstrip())
            #
            return_code = process.wait()
        finally:
            self._set_external_pid(process.pid, False)
            with self._state_lock:
                if self.process is process:
                    self.process = None
        #
        log.info("%s exited with return code: %s", self.name, return_code)
        return return_code

    def _run_monitor(self):
        while not self._stop_event.is_set():
            try:
                return_code = self._spawn_once()
            except:  # pylint: disable=W0702
                log.exception("Failed to run %s", self.name)
                return_code = -1
            #
            if self._stop_event.is_set() or self.context.stop_event.is_set():
                return
            #
            if not self._should_restart():
                return
            #
            log.warning(
                "%s stopped (code=%s), restarting in %.1fs",
                self.name,
                return_code,
                self.restart_delay,
            )
            time.sleep(self.restart_delay)

    def start(self):
        """ Start """
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        #
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._run_monitor,
            daemon=True,
            name=f"{self.name}-monitor",
        )
        self._monitor_thread.start()

    def stop(self):
        """ Stop """
        self._stop_event.set()
        #
        with self._state_lock:
            process = self.process
        #
        if process is not None:
            if self.context.server_mode == "oneshot":
                log.info("Waiting for %s to exit (oneshot mode)", self.name)
                try:
                    process.wait(timeout=self.oneshot_wait)
                    return
                except:  # pylint: disable=W0702
                    pass
            #
            log.info("Stopping %s (pid=%s)", self.name, process.pid)
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except:  # pylint: disable=W0702
                pass
            #
            try:
                process.wait(timeout=self.stop_timeout)
            except:  # pylint: disable=W0702
                log.warning("%s did not exit in time, killing", self.name)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except:  # pylint: disable=W0702
                    pass
        #
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=self.stop_timeout)
