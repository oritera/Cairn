# PyInstaller spec for the Cairn desktop app. Build with desktop\build.ps1.
# -*- mode: python ; coding: utf-8 -*-
import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

HERE = Path(SPECPATH)
ROOT = HERE.parent
SRC = ROOT / "cairn" / "src"
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', (SRC / "cairn" / "__init__.py").read_text()).group(1)
NUMS = tuple(int(x) for x in (VERSION.split(".") + ["0", "0", "0"])[:4])

datas = collect_data_files("cairn", includes=["**/*.html", "**/*.js", "**/*.css", "**/*.svg", "**/*.md",
                                               "**/*.ico", "**/*.png"])
datas += collect_data_files("webview")
datas += [(str(ROOT / "README.md"), "."), (str(ROOT / "LICENSE"), ".")]

hiddenimports = (
    collect_submodules("cairn")
    + collect_submodules("uvicorn")          # loops/protocols are imported by name at runtime
    + collect_submodules("webview.platforms.edgechromium")
    + ["webview.platforms.winforms", "keyring.backends.Windows", "win32ctypes.core",
       "clr", "clr_loader", "pythonnet"]
)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMS, prodvers=NUMS),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "Cairn"),
            StringStruct("FileDescription", "Cairn"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "Cairn"),
            StringStruct("LegalCopyright", "GNU AGPLv3"),
            StringStruct("OriginalFilename", "Cairn.exe"),
            StringStruct("ProductName", "Cairn"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

a = Analysis(
    [str(HERE / "cairn_entry.py")],
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PyInstaller", "PIL", "matplotlib", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Cairn",
    icon=str(SRC / "cairn" / "desktop" / "assets" / "cairn.ico"),
    console=False,             # windowed: no console window
    version=version_info,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Cairn", upx=False)
