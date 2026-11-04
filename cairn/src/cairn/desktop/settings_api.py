"""Python side of the Settings window (exposed to its JavaScript as ``pywebview.api``).

Only public methods are callable from the page. Stored API keys are never returned: the page
only sees whether a key is saved.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cairn import __version__
from cairn.desktop import paths
from cairn.desktop.settings import TASK_TYPES, WORKER_TYPES, is_secret_key

if TYPE_CHECKING:  # pragma: no cover
    from cairn.desktop.app import DesktopApp

LOG = logging.getLogger("cairn.desktop.settings")


class SettingsApi:
    def __init__(self, app: "DesktopApp"):
        self._app = app
        self._window = None

    def attach(self, window) -> None:
        self._window = window

    # ------------------------------------------------------------------ data

    def get_state(self) -> dict[str, Any]:
        settings = self._app.store.load()
        return {
            "settings": self._app.store.public_view(settings),
            "meta": {
                "version": __version__,
                "first_run": self._app.first_run,
                "worker_types": WORKER_TYPES,
                "task_types": list(TASK_TYPES),
                "default_workspace": str(paths.default_workspace_root()),
                "settings_file": str(paths.settings_file()),
                "logs_dir": str(paths.logs_dir()),
                "cli": {t: _which(spec["binary"]) for t, spec in WORKER_TYPES.items() if spec["binary"]},
            },
            "status": self._app.dispatcher_status(),
        }

    def is_secret(self, key: str) -> bool:
        return is_secret_key(key)

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self._app.store.load()
        result = self._app.store.apply_update(current, payload)
        if not result.ok:
            return {"ok": False, "errors": result.errors}
        self._app.settings = result.settings
        self._app.first_run = False
        port_changed = result.settings["desktop"]["port"] != current["desktop"].get("port")
        return {
            "ok": True,
            "dispatcher_running": self._app.dispatcher.is_running(),
            "port_changed": port_changed,
            "state": self.get_state(),
        }

    # ------------------------------------------------------------------ dispatcher

    def status(self) -> dict[str, Any]:
        return self._app.dispatcher_status()

    def start_dispatcher(self) -> dict[str, Any]:
        self._app.start_dispatcher()
        return self._app.dispatcher_status()

    def restart_dispatcher(self) -> dict[str, Any]:
        self._app.restart_dispatcher()
        return self._app.dispatcher_status()

    def stop_dispatcher(self) -> dict[str, Any]:
        self._app.stop_dispatcher()
        return self._app.dispatcher_status()

    # ------------------------------------------------------------------ helpers

    def browse_folder(self, current: str = "") -> str | None:
        if self._window is None:
            return None
        import webview

        start = current if current and Path(current).is_dir() else str(paths.documents_dir())
        picked = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        if not picked:
            return None
        return picked[0] if isinstance(picked, (list, tuple)) else str(picked)

    def open_folder(self, which: str) -> None:
        if which == "logs":
            paths.open_folder(paths.logs_dir())
        elif which == "data":
            paths.open_folder(paths.data_dir())

    def open_path(self, path: str) -> None:
        if path:
            paths.open_folder(Path(path))

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()

    def test_worker(self, worker: dict[str, Any], execution: str) -> dict[str, Any]:
        """Check one worker as currently edited (unsaved values included)."""
        try:
            return _test_worker(self._app, worker, execution)
        except Exception as exc:  # report, never crash the window
            LOG.exception("worker test failed")
            return {"ok": False, "message": f"Test failed: {exc}"}


# ---------------------------------------------------------------------- worker test


def _which(binary: str | None) -> str | None:
    return shutil.which(binary) if binary else None


def _test_worker(app: "DesktopApp", worker: dict[str, Any], execution: str) -> dict[str, Any]:
    from cairn.dispatcher.config import WORKER_ENV_KEYS, WorkerConfig
    from cairn.dispatcher.runtime.platform_compat import resolve_executable
    from cairn.dispatcher.workers.registry import get_driver

    wtype = worker.get("type")
    if wtype not in WORKER_TYPES:
        return {"ok": False, "message": f"Unknown worker type {wtype!r}."}
    if wtype == "mock":
        return {"ok": True, "message": "Mock workers need no CLI, account or API key."}

    env = app.store.preview_worker_env(app.store.load(), worker)
    lines: list[str] = []
    ok = True
    driver = get_driver(wtype, execution)
    required = WORKER_ENV_KEYS[wtype]
    have_api = all(env.get(k) for k in required)

    if execution == "local":
        binary = WORKER_TYPES[wtype]["binary"]
        path = shutil.which(binary)
        if path is None:
            return {"ok": False, "message": f"'{binary}' was not found on PATH. Install it and make sure "
                                            f"'{binary} --version' works in a new PowerShell window, then restart Cairn."}
        version = _cli_version(resolve_executable([binary, "--version"]))
        lines.append(f"✓ {binary} found: {path}" + (f" ({version})" if version else ""))
        if wtype == "claudecode" and not any(env.get(k) for k in required):
            lines.append("Uses the CLI's own login (no API settings given).")
        if not (wtype == "claudecode" and have_api):
            return {"ok": True, "message": "\n".join(lines)}
    else:
        missing = [k for k in required if not env.get(k)]
        if missing:
            return {"ok": False, "message": "Container mode needs: " + ", ".join(missing)}

    cfg = WorkerConfig.model_validate({
        "name": worker.get("name") or "test", "type": wtype, "task_types": ["explore"],
        "max_running": 1, "priority": 0, "env": env,
    })
    result = driver.check_health(cfg, timeout=20)
    if result.ok:
        lines.append(f"✓ API reachable and key accepted (HTTP {result.status}).")
    else:
        ok = False
        lines.append(f"✗ API check failed (HTTP {result.status or '-'}): {result.detail or 'no details'}")
    return {"ok": ok, "message": "\n".join(lines)}


def _cli_version(command: list[str]) -> str:
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                             encoding="utf-8", errors="replace",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (out.stdout or out.stderr or "").strip().splitlines()
    return text[0][:80] if text else ""
