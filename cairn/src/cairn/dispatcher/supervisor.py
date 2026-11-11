"""Dispatcher supervisor — the always-on anchor that owns the dispatcher process.

The server (and the config wizard) cannot restart the dispatcher directly: the
server only writes files. The supervisor closes that gap. It runs as its own
long-lived process (started by autostart instead of `cairn dispatch`), keeps the
dispatcher as a managed child, and reacts to a tiny file-based control protocol
in ``~/.local/share/cairn/supervisor/``:

    active.config    text file: path to the config the dispatcher should run.
    restart.request  touch file: a newer mtime than last handled means "restart".
    status.json      written by the supervisor: its state for the dashboard.
    dispatch.log     the managed dispatcher's stdout/stderr.

Crucially it stays alive even with no usable config (fresh machine, default config
with no model): it parks in ``waiting`` instead of crash-looping, so the server /
dashboard / wizard stay fully usable and can bootstrap the first real config, then
flip ``active.config`` + touch ``restart.request`` to bring the dispatcher up.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cairn.dispatcher.config import DispatchConfig

LOG = logging.getLogger(__name__)

DEFAULT_SUPERVISOR_DIR = Path.home() / ".local" / "share" / "cairn" / "supervisor"

POLL_INTERVAL_S = 1.5
STOP_GRACE_S = 10
# crash-loop guard: a child that dies in under this many seconds counts as a fast
# failure; after this many consecutive fast failures the supervisor parks in `error`
# until the config changes or a restart is requested.
FAST_FAIL_S = 15
MAX_FAST_FAILS = 4
BACKOFF_S = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cairn_bin() -> list[str]:
    """How to invoke the dispatcher child — the same `cairn` entry we run under."""
    found = shutil.which("cairn")
    if found:
        return [found]
    # fall back to however this process was launched
    if sys.argv and sys.argv[0] and Path(sys.argv[0]).name.startswith("cairn"):
        return [sys.argv[0]]
    return [sys.executable, "-m", "cairn.cli"]


class Supervisor:
    def __init__(self, control_dir: Path, initial_config: Path | None = None) -> None:
        self.dir = control_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.active_file = self.dir / "active.config"
        self.restart_file = self.dir / "restart.request"
        self.status_file = self.dir / "status.json"
        self.child_log = self.dir / "dispatch.log"

        # seed the active config on first run if one was supplied and none is set yet
        if initial_config is not None and not self._read_active():
            self._write_active(initial_config)

        self.since = _now()
        self.child: subprocess.Popen | None = None
        self.child_config: str | None = None
        self.child_started: float = 0.0
        self.child_started_iso: str | None = None
        self.restarts = 0
        self.last_exit_code: int | None = None
        self.fast_fails = 0
        self.backoff_until = 0.0
        self._last_restart_mtime = self._restart_mtime()
        self._stop = False

    # ---- control files -------------------------------------------------- #
    def _read_active(self) -> str | None:
        try:
            value = self.active_file.read_text(encoding="utf-8").strip()
            return value or None
        except OSError:
            return None

    def _write_active(self, path: Path | str) -> None:
        try:
            self.active_file.write_text(str(path), encoding="utf-8")
        except OSError as exc:
            LOG.warning("cannot write active.config: %s", exc)

    def _restart_mtime(self) -> float:
        try:
            return self.restart_file.stat().st_mtime
        except OSError:
            return 0.0

    def _restart_requested(self) -> bool:
        mtime = self._restart_mtime()
        if mtime > self._last_restart_mtime:
            self._last_restart_mtime = mtime
            return True
        return False

    # ---- config validation --------------------------------------------- #
    @staticmethod
    def _validate(path_str: str | None) -> tuple[bool, str, Path | None]:
        if not path_str:
            return False, "no config selected — use the wizard to create one", None
        path = Path(path_str).expanduser()
        if not path.is_file():
            return False, f"config not found: {path}", path
        try:
            DispatchConfig.load(path)
        except Exception as exc:  # pydantic/validation/prompt errors
            return False, f"invalid config: {exc}", path
        return True, "", path

    # ---- child lifecycle ------------------------------------------------ #
    def _child_alive(self) -> bool:
        return self.child is not None and self.child.poll() is None

    def _start_child(self, config_path: Path) -> None:
        argv = [*_cairn_bin(), "dispatch", "--config", str(config_path)]
        LOG.info("starting dispatcher child: %s", " ".join(argv))
        logf = open(self.child_log, "ab", buffering=0)
        logf.write(f"\n=== supervisor start {_now()} :: {config_path} ===\n".encode())
        self.child = subprocess.Popen(
            argv,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # own process group so we can kill the whole tree
            env=os.environ.copy(),
        )
        self.child_config = str(config_path)
        self.child_started = time.monotonic()
        self.child_started_iso = _now()
        self.restarts += 1

    def _stop_child(self) -> None:
        if self.child is None:
            return
        if self.child.poll() is None:
            LOG.info("stopping dispatcher child pid=%s", self.child.pid)
            try:
                os.killpg(os.getpgid(self.child.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.child.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.child.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.child = None
        self.child_config = None
        self.child_started_iso = None

    # ---- status --------------------------------------------------------- #
    def _write_status(self, state: str, reason: str, active: Path | None) -> None:
        status = {
            "state": state,
            "reason": reason,
            "active_config": str(active) if active else None,
            "child_pid": self.child.pid if self._child_alive() else None,
            "child_started_at": self.child_started_iso if self._child_alive() else None,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit_code,
            "updated_at": _now(),
            "since": self.since,
            "log_path": str(self.child_log),
        }
        tmp = self.status_file.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(status), encoding="utf-8")
            tmp.replace(self.status_file)
        except OSError as exc:
            LOG.debug("cannot write status.json: %s", exc)

    # ---- main loop ------------------------------------------------------ #
    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)
        LOG.info("supervisor up; control dir=%s", self.dir)
        while not self._stop:
            try:
                self._tick()
            except Exception:
                LOG.exception("supervisor tick failed")
            time.sleep(POLL_INTERVAL_S)
        self._stop_child()
        self._write_status("stopped", "supervisor exiting", self._validate(self._read_active())[2])
        LOG.info("supervisor stopped")

    def _handle_stop(self, *_a) -> None:
        self._stop = True

    def _tick(self) -> None:
        desired = self._read_active()
        ok, reason, path = self._validate(desired)
        restart = self._restart_requested()

        # reap a child that died on its own
        if self.child is not None and self.child.poll() is not None:
            self.last_exit_code = self.child.returncode
            ran = time.monotonic() - self.child_started
            LOG.warning("dispatcher child exited code=%s after %.1fs", self.last_exit_code, ran)
            self.child = None
            self.child_config = None
            self.child_started_iso = None
            if ran < FAST_FAIL_S:
                self.fast_fails += 1
                self.backoff_until = time.monotonic() + BACKOFF_S
            else:
                self.fast_fails = 0

        if not ok:
            # no usable config: park, don't crash. clear crash-loop state.
            if self._child_alive():
                self._stop_child()
            self.fast_fails = 0
            self._write_status("waiting", reason, path)
            return

        # an explicit restart, or a change of active config, forces a fresh child
        if restart or (self._child_alive() and self.child_config != str(path)):
            self._stop_child()
            self.fast_fails = 0
            self.backoff_until = 0.0

        if self._child_alive():
            self._write_status("running", "", path)
            return

        # need to (re)start — unless we're in crash-loop backoff
        if self.fast_fails >= MAX_FAST_FAILS:
            self._write_status(
                "error",
                f"dispatcher keeps exiting (code={self.last_exit_code}); see {self.child_log.name} — "
                f"fix the config and Apply again, or touch restart.request",
                path,
            )
            return
        if time.monotonic() < self.backoff_until:
            self._write_status("error", f"restarting after crash (code={self.last_exit_code})…", path)
            return

        self._start_child(path)
        self._write_status("running", "", path)


def run_supervisor(control_dir: Path | None = None, initial_config: Path | None = None) -> None:
    env_dir = os.environ.get("CAIRN_SUPERVISOR_DIR")
    base = control_dir or (Path(env_dir) if env_dir else DEFAULT_SUPERVISOR_DIR)
    Supervisor(base, initial_config=initial_config).run()
