"""Supervision of the two moving parts: the API server (in-process) and the dispatcher
(a child process, so it can be restarted after a settings change and so stopping it takes
its agent processes down with it)."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from cairn.dispatcher.runtime.platform_compat import IS_WINDOWS, terminate_tree

LOG = logging.getLogger("cairn.desktop")

# A dispatcher that exits within this many seconds is reported as a failed start
# (bad config, missing CLI, ...) rather than as a normal stop.
EARLY_EXIT_SECONDS = 8
LOG_GENERATIONS = 3


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(preferred: int, host: str = "127.0.0.1") -> int:
    if port_is_free(preferred, host):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


class ServerThread:
    """Runs the FastAPI app with uvicorn on a background thread (localhost only)."""

    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.host = host
        self.port = port
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 30.0) -> None:
        import uvicorn

        from cairn.server.app import app

        config = uvicorn.Config(app, host=self.host, port=self.port, log_config=None,
                                access_log=False, lifespan="on")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="cairn-server", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server.started:
                LOG.info("server listening on %s", self.url)
                return
            if not self._thread.is_alive():
                break
            time.sleep(0.05)
        raise RuntimeError(f"the Cairn server did not start on {self.url} (see desktop.log)")

    def stop(self, timeout: float = 5.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)


def dispatcher_command() -> list[str]:
    """How to launch `cairn dispatch` as a child: the frozen app re-invokes its own exe
    (which routes CLI sub-commands), a source checkout uses `python -m cairn`."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "dispatch"]
    python = Path(sys.executable)
    # A gui-script runs under pythonw.exe; the child should use the same interpreter.
    return [str(python), "-m", "cairn", "dispatch"]


class DispatcherProcess:
    """Starts/stops `cairn dispatch --config -` and reports its state.

    States: stopped, running, stopping, failed (exited on its own, with an exit code).
    """

    def __init__(self, log_path: Path, on_change: Callable[["DispatcherProcess"], None] | None = None):
        self.log_path = log_path
        self.on_change = on_change
        self.state = "stopped"
        self.returncode: int | None = None
        self.started_at: float | None = None
        self.early_exit = False
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._stopping = False

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, config_yaml: str, cwd: Path, log_level: str = "INFO") -> None:
        with self._lock:
            if self.is_running():
                raise RuntimeError("the dispatcher is already running")
            cwd.mkdir(parents=True, exist_ok=True)
            self._rotate_logs()
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(self.log_path, "ab")
            env = dict(os.environ)
            env["CAIRN_CHILD_LOG"] = str(self.log_path)
            env.pop("PYTHONIOENCODING", None)
            flags = 0
            if IS_WINDOWS:
                flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            command = [*dispatcher_command(), "--config", "-", "--log-level", log_level]
            LOG.info("starting dispatcher: %s (cwd=%s)", " ".join(command[:-4]) + " ...", cwd)
            try:
                self._proc = subprocess.Popen(
                    command,
                    cwd=str(cwd),
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
            finally:
                log_handle.close()  # the child holds its own handle
            assert self._proc.stdin is not None
            try:
                self._proc.stdin.write(config_yaml.encode("utf-8"))
                self._proc.stdin.close()
            except OSError:
                pass  # the child died immediately; the monitor reports it
            self.state = "running"
            self.returncode = None
            self.early_exit = False
            self.started_at = time.monotonic()
            self._stopping = False
            threading.Thread(target=self._monitor, args=(self._proc,), daemon=True,
                             name="cairn-dispatcher-monitor").start()
        self._notify()

    def stop(self, grace_seconds: float = 2.0) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self.state = "stopped"
                return
            self._stopping = True
            self.state = "stopping"
        self._notify()
        terminate_tree(proc, grace_seconds)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            LOG.warning("dispatcher pid=%s did not exit after kill", proc.pid)

    def _monitor(self, proc: subprocess.Popen) -> None:
        returncode = proc.wait()
        with self._lock:
            if proc is not self._proc:
                return
            self.returncode = returncode
            ran_for = time.monotonic() - (self.started_at or time.monotonic())
            if self._stopping:
                self.state = "stopped"
            else:
                self.state = "failed"
                self.early_exit = ran_for < EARLY_EXIT_SECONDS
                LOG.warning("dispatcher exited on its own rc=%s after %.1fs", returncode, ran_for)
        self._notify()

    def _notify(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self)
            except Exception:  # never let a UI callback kill the monitor
                LOG.exception("dispatcher state callback failed")

    def _rotate_logs(self) -> None:
        if not self.log_path.exists():
            return
        for generation in range(LOG_GENERATIONS - 1, 0, -1):
            older = self.log_path.with_name(f"{self.log_path.name}.{generation}")
            newer = self.log_path.with_name(f"{self.log_path.name}.{generation - 1}") if generation > 1 else self.log_path
            if newer.exists():
                try:
                    os.replace(newer, older)
                except OSError:
                    pass

    def tail(self, max_lines: int = 30) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-max_lines:])
