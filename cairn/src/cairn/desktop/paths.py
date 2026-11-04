"""Where the desktop app keeps its files (all per-user, no admin rights needed)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from cairn.server.db import _default_data_dir

APP_NAME = "Cairn"


def data_dir() -> Path:
    """%LOCALAPPDATA%\\cairn — shared with the CLI, so both use the same database."""
    return _default_data_dir()


def settings_file() -> Path:
    return data_dir() / "desktop" / "settings.json"


def logs_dir() -> Path:
    return data_dir() / "logs"


def webview_storage_dir() -> Path:
    """Persistent browser storage for the embedded UI (keeps its layout preferences)."""
    return data_dir() / "webview"


def documents_dir() -> Path:
    """The user's real Documents folder (handles OneDrive / redirected folders)."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes
            import uuid

            folder_id = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            guid = GUID(folder_id.fields[0], folder_id.fields[1], folder_id.fields[2],
                        (ctypes.c_ubyte * 8)(*folder_id.bytes[8:]))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
                try:
                    return Path(out.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(out)
        except Exception:
            pass
    return Path.home() / "Documents"


def default_workspace_root() -> Path:
    return documents_dir() / "Cairn Projects"


def open_folder(path: Path) -> None:
    """Open a folder in Explorer, creating it first if needed."""
    path.mkdir(parents=True, exist_ok=True)
    _shell_open(path)


def open_file(path: Path) -> bool:
    """Open a file with its default app. Returns False if it does not exist."""
    if not path.is_file():
        return False
    _shell_open(path)
    return True


def _shell_open(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # noqa: S606 - a local path the user chose from the menu
    else:  # pragma: no cover - the desktop app targets Windows
        import subprocess

        subprocess.Popen(["xdg-open", str(path)])
