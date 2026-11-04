"""The Cairn desktop application: main window, menu, settings window, process control."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import traceback
from importlib import resources
from pathlib import Path

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

from cairn import __version__
from cairn.desktop import paths
from cairn.desktop.processes import DispatcherProcess, ServerThread, pick_port
from cairn.desktop.settings import KEYRING_SERVICE, KeyringSecretStore, SettingsStore
from cairn.desktop.settings_api import SettingsApi

LOG = logging.getLogger("cairn.desktop")

MUTEX_NAME = "Local\\CairnDesktopSingleInstance"
STATUS_TEXT = {
    "running": "Dispatcher running",
    "stopping": "Stopping dispatcher…",
    "stopped": "Dispatcher stopped",
    "failed": "Dispatcher stopped unexpectedly",
}


class DesktopApp:
    def __init__(self) -> None:
        self.store = SettingsStore(paths.settings_file(), KeyringSecretStore(), paths.default_workspace_root())
        self.first_run = not self.store.exists
        self.settings = self.store.load()
        if self.first_run:
            self.store.save(self.settings)
        self.server: ServerThread | None = None
        self.dispatcher = DispatcherProcess(paths.logs_dir() / "dispatcher.log", on_change=self._on_dispatcher_change)
        self.main_window: webview.Window | None = None
        self.settings_window: webview.Window | None = None
        self._settings_lock = threading.Lock()
        self._closing = False

    # ------------------------------------------------------------------ lifecycle

    def run(self) -> None:
        port = pick_port(int(self.settings["desktop"].get("port", 8000)))
        if port != self.settings["desktop"].get("port"):
            LOG.warning("port %s is in use, using %s instead", self.settings["desktop"].get("port"), port)
        self.server = ServerThread(port)
        self.server.start()

        webview.settings["ALLOW_DOWNLOADS"] = True          # the UI's export feature
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        debug_port = os.environ.get("CAIRN_WEBVIEW_DEBUG_PORT")  # developer option: DevTools protocol
        if debug_port and debug_port.isdigit():
            webview.settings["REMOTE_DEBUGGING_PORT"] = int(debug_port)
        self.main_window = webview.create_window(
            "Cairn",
            url=self.server.url + "/",
            width=1440,
            height=920,
            min_size=(900, 600),
            menu=self._menu(),
            background_color="#0f172a",
        )
        self.main_window.events.closed += self._on_main_closed
        storage = paths.webview_storage_dir()
        storage.mkdir(parents=True, exist_ok=True)
        webview.start(self._after_start, private_mode=False, storage_path=str(storage),
                      icon=str(_icon_path()) if _icon_path() else None)
        self.shutdown()

    def _after_start(self) -> None:
        self._update_title()
        if self.first_run:
            self.open_settings()
        elif self.settings["desktop"].get("autostart_dispatcher", True):
            self.start_dispatcher(interactive=False)

    def _on_main_closed(self) -> None:
        self._closing = True
        if self.settings_window is not None:
            try:
                self.settings_window.destroy()
            except Exception:
                pass

    def shutdown(self) -> None:
        self._closing = True
        LOG.info("shutting down")
        self.dispatcher.stop(grace_seconds=1.0)
        if self.server is not None:
            self.server.stop()

    # ------------------------------------------------------------------ dispatcher

    def start_dispatcher(self, interactive: bool = True) -> bool:
        if self.dispatcher.is_running():
            return True
        self.settings = self.store.load()
        errors = self.store.validate(self.settings)
        if errors:
            LOG.warning("not starting dispatcher, settings invalid: %s", errors)
            if interactive or not self.first_run:
                self._threaded(self._confirm_open_settings,
                               "The dispatcher can't start until these settings are fixed:\n\n- "
                               + "\n- ".join(errors[:8]))
            return False
        assert self.server is not None
        config_yaml = self.store.dispatch_yaml(self.settings, self.server.url)
        dispatch = self.settings["dispatch"]
        if dispatch["runtime"].get("execution") == "local":
            cwd = Path(dispatch["local"].get("workspace_root") or paths.default_workspace_root())
        else:
            cwd = paths.data_dir()
        try:
            self.dispatcher.start(config_yaml, cwd, self.settings["desktop"].get("log_level", "INFO"))
        except Exception as exc:
            LOG.exception("could not start dispatcher")
            self._threaded(self._message, "Could not start the dispatcher", str(exc))
            return False
        return True

    def stop_dispatcher(self) -> None:
        self.dispatcher.stop()

    def restart_dispatcher(self) -> bool:
        self.dispatcher.stop()
        return self.start_dispatcher()

    def dispatcher_status(self) -> dict:
        return {
            "state": self.dispatcher.state,
            "text": STATUS_TEXT.get(self.dispatcher.state, self.dispatcher.state),
            "returncode": self.dispatcher.returncode,
            "server": self.server.url if self.server else None,
        }

    def _on_dispatcher_change(self, proc: DispatcherProcess) -> None:
        self._update_title()
        if proc.state == "failed" and not self._closing:
            tail = proc.tail(14)
            reason = "right after starting" if proc.early_exit else "unexpectedly"
            self._threaded(
                self._confirm_open_settings,
                f"The dispatcher stopped {reason} (exit code {proc.returncode}).\n\n"
                f"Last log lines:\n{tail[-1500:]}",
            )

    def _update_title(self) -> None:
        if self.main_window is None or self._closing:
            return
        status = STATUS_TEXT.get(self.dispatcher.state, self.dispatcher.state)
        if self.dispatcher.state == "failed" and self.dispatcher.returncode is not None:
            status += f" (exit {self.dispatcher.returncode})"
        try:
            self.main_window.set_title(f"Cairn — {status}")
        except Exception:
            pass

    # ------------------------------------------------------------------ settings

    def open_settings(self) -> None:
        with self._settings_lock:
            if self.settings_window is not None:
                try:
                    self.settings_window.restore()
                    self.settings_window.show()
                    return
                except Exception:
                    self.settings_window = None
            html = resources.files("cairn.desktop").joinpath("ui", "settings.html").read_text(encoding="utf-8")
            api = SettingsApi(self)
            window = webview.create_window(
                "Cairn Settings",
                html=html,
                js_api=api,
                width=1040,
                height=820,
                min_size=(760, 560),
                background_color="#f8fafc",
            )
            api.attach(window)
            window.events.closed += self._on_settings_closed
            self.settings_window = window

    def _on_settings_closed(self) -> None:
        self.settings_window = None

    def import_dispatch_yaml(self) -> None:
        assert self.main_window is not None
        picked = self.main_window.create_file_dialog(
            webview.FileDialog.OPEN, file_types=("YAML files (*.yaml;*.yml)", "All files (*.*)"))
        if not picked:
            return
        path = Path(picked[0])
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._message("Import failed", f"Could not read {path}:\n{exc}")
            return
        result = self.store.import_dispatch_yaml(self.store.load(), text)
        if not result.ok:
            self._message("Import failed", f"{path.name} was not imported:\n\n- " + "\n- ".join(result.errors[:10]))
            return
        self.settings = result.settings
        restart = self.main_window.create_confirmation_dialog(
            "Settings imported",
            f"Imported {path.name}. API keys from the file are now stored in Windows Credential "
            "Manager (you can delete the plain-text file).\n\nRestart the dispatcher to use the new settings?")
        if restart:
            self.restart_dispatcher()

    # ------------------------------------------------------------------ menu

    def _menu(self) -> list[Menu]:
        def bg(fn, *args):
            return lambda: self._threaded(fn, *args)

        return [
            Menu("Cairn", [
                MenuAction("Settings…", bg(self.open_settings)),
                MenuAction("Import dispatch.yaml…", bg(self.import_dispatch_yaml)),
                MenuSeparator(),
                MenuAction("Open projects folder", bg(self._open_projects_folder)),
                MenuAction("Open data folder", bg(paths.open_folder, paths.data_dir())),
                MenuSeparator(),
                MenuAction("Quit", bg(self._quit)),
            ]),
            Menu("Dispatcher", [
                MenuAction("Start", bg(self.start_dispatcher)),
                MenuAction("Stop", bg(self.stop_dispatcher)),
                MenuAction("Restart", bg(self.restart_dispatcher)),
                MenuSeparator(),
                MenuAction("View dispatcher log", bg(self._open_log, "dispatcher.log")),
                MenuAction("View app log", bg(self._open_log, "desktop.log")),
            ]),
            Menu("View", [
                MenuAction("Reload", bg(self._reload)),
                MenuAction("Open in browser", bg(self._open_in_browser)),
            ]),
            Menu("Help", [
                MenuAction("About Cairn", bg(self._about)),
            ]),
        ]

    def _open_projects_folder(self) -> None:
        root = self.store.load()["dispatch"]["local"].get("workspace_root") or str(paths.default_workspace_root())
        paths.open_folder(Path(root))

    def _open_log(self, name: str) -> None:
        if not paths.open_file(paths.logs_dir() / name):
            self._message("No log yet", f"{name} has not been written yet.")

    def _reload(self) -> None:
        if self.main_window is not None and self.server is not None:
            self.main_window.load_url(self.server.url + "/")

    def _open_in_browser(self) -> None:
        import webbrowser

        if self.server is not None:
            webbrowser.open(self.server.url + "/")

    def _about(self) -> None:
        server = self.server.url if self.server else "-"
        self._message(
            "About Cairn",
            f"Cairn {__version__} — Windows desktop app\n\n"
            f"Server: {server}\n"
            f"Data: {paths.data_dir()}\n"
            f"Settings: {paths.settings_file()}\n"
            f"Logs: {paths.logs_dir()}\n\n"
            f"API keys are stored in Windows Credential Manager under '{KEYRING_SERVICE}'.\n\n"
            "Licensed under GNU AGPLv3. Only use Cairn on systems you are authorized to work on.",
        )

    def _quit(self) -> None:
        if self.main_window is not None:
            self.main_window.destroy()

    # ------------------------------------------------------------------ dialogs

    def _confirm_open_settings(self, message: str) -> None:
        if self.main_window is None or self._closing:
            return
        if self.main_window.create_confirmation_dialog("Cairn", message + "\n\nOpen Settings now?"):
            self.open_settings()

    def _message(self, title: str, message: str) -> None:
        message_box(title, message)

    @staticmethod
    def _threaded(fn, *args) -> None:
        def run():
            try:
                fn(*args)
            except Exception:
                LOG.exception("menu action failed: %s", getattr(fn, "__name__", fn))

        threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------- helpers


def message_box(title: str, message: str, error: bool = False) -> None:
    if sys.platform.startswith("win"):
        import ctypes

        MB_ICONINFORMATION, MB_ICONERROR, MB_SETFOREGROUND, MB_TOPMOST = 0x40, 0x10, 0x10000, 0x40000
        flags = (MB_ICONERROR if error else MB_ICONINFORMATION) | MB_SETFOREGROUND | MB_TOPMOST
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
    else:  # pragma: no cover
        print(f"{title}: {message}", file=sys.stderr)


def _icon_path() -> Path | None:
    candidate = Path(str(resources.files("cairn.desktop").joinpath("assets", "cairn.ico")))
    return candidate if candidate.is_file() else None


def _acquire_single_instance() -> object | None:
    """Named mutex; returns a handle to keep alive, or None if another instance owns it."""
    if not sys.platform.startswith("win"):
        return object()
    import ctypes

    ERROR_ALREADY_EXISTS = 183
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def _setup_logging() -> None:
    logs = paths.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(logs / "desktop.log", maxBytes=1_000_000,
                                                   backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("pywebview").setLevel(logging.WARNING)

    def excepthook(exc_type, exc, tb):
        LOG.critical("uncaught error\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        message_box("Cairn crashed", f"{exc_type.__name__}: {exc}\n\nDetails are in {logs / 'desktop.log'}", error=True)

    sys.excepthook = excepthook


def run() -> None:
    _setup_logging()
    mutex = _acquire_single_instance()
    if mutex is None:
        message_box("Cairn", "Cairn is already running. Switch to its window from the taskbar.")
        return
    LOG.info("Cairn desktop %s starting", __version__)
    try:
        DesktopApp().run()
    except Exception as exc:
        LOG.exception("fatal error")
        message_box("Cairn could not start", f"{exc}\n\nDetails are in {paths.logs_dir() / 'desktop.log'}", error=True)
