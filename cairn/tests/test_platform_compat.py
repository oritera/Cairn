"""Windows-port regression tests: process launching, path defaults and workdir naming."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.runtime import platform_compat
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.local_process import LocalProcess

windows_only = pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-specific behaviour")

# The exact text npm's cmd-shim generates for a global package bin.
NPM_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\fakecli\bin\fakecli.js" %*
"""

# A prompt shaped like the real ones: multi-line, quotes, and every cmd.exe metacharacter.
NASTY_PROMPT = 'line one\nline "two" & three | four\n100% ^done <ok> %PATH%\n\ttabbed'


def _make_npm_shim(tmp_path: Path, js_body: str) -> Path:
    bin_dir = tmp_path / "npm"
    js = bin_dir / "node_modules" / "fakecli" / "bin" / "fakecli.js"
    js.parent.mkdir(parents=True)
    js.write_text(js_body, encoding="utf-8")
    shim = bin_dir / "fakecli.cmd"
    shim.write_text(NPM_SHIM, encoding="ascii")
    return shim


@windows_only
def test_npm_shim_is_unwrapped_to_node_and_script(tmp_path: Path, monkeypatch) -> None:
    shim = _make_npm_shim(tmp_path, "")
    monkeypatch.setattr(platform_compat.shutil, "which", lambda name: r"C:\node\node.exe" if name == "node" else None)

    argv = platform_compat.unwrap_npm_cmd_shim(str(shim))

    assert argv == [r"C:\node\node.exe", str(tmp_path / "npm" / "node_modules" / "fakecli" / "bin" / "fakecli.js")]


@windows_only
def test_npm_shim_prefers_bundled_node(tmp_path: Path) -> None:
    shim = _make_npm_shim(tmp_path, "")
    bundled = shim.parent / "node.exe"
    bundled.write_bytes(b"")

    argv = platform_compat.unwrap_npm_cmd_shim(str(shim))

    assert argv is not None and argv[0] == str(bundled)


@windows_only
def test_non_npm_cmd_file_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "tool.cmd"
    script.write_text("@echo off\r\necho hi\r\n", encoding="ascii")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    assert platform_compat.unwrap_npm_cmd_shim(str(script)) is None
    assert Path(platform_compat.resolve_executable(["tool", "x"])[0]) == script
    assert platform_compat.resolve_executable(["tool", "x"])[1:] == ["x"]


@windows_only
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_multiline_prompt_survives_npm_shim_end_to_end(tmp_path: Path, monkeypatch) -> None:
    # The fake CLI echoes its last argument back as JSON so we can compare byte-for-byte.
    _make_npm_shim(tmp_path, "process.stdout.write(JSON.stringify(process.argv[process.argv.length - 1]));\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'npm'}{os.pathsep}{os.environ['PATH']}")

    process = LocalProcess(
        ["fakecli", "-p", "--", NASTY_PROMPT],
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_seconds=30,
    )
    process.start()
    result = process.communicate(timeout=40)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == NASTY_PROMPT


@windows_only
def test_default_db_lives_under_localappdata(monkeypatch, tmp_path: Path) -> None:
    from cairn.server import db

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert db._default_data_dir() == tmp_path / "cairn"


def test_default_db_is_not_a_posix_dotlocal_path_on_windows() -> None:
    from cairn.server import db

    if sys.platform.startswith("win"):
        assert ".local" not in db.DEFAULT_DB.parts
    else:
        assert db.DEFAULT_DB.name == "cairn.db"


def test_project_dir_strips_characters_illegal_on_windows(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))

    name = Path(backend.container_name('team/a:b*c?"d<e>f|g\\h'))

    assert name.parent == tmp_path
    assert name.name == "team-a-b-c--d-e-f-g-h"


def test_graph_snapshot_root_is_a_native_absolute_path(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    workdir = backend.ensure_running("proj_001")

    root = Path(backend.graph_snapshot_root(workdir))

    assert root.is_absolute()  # "/tmp/..." would fail this on Windows
    assert root.parent == Path(workdir)


@windows_only
def test_worker_env_override_wins_regardless_of_case(tmp_path: Path, monkeypatch) -> None:
    # Host has HTTPS_PROXY; the worker config sets https_proxy. On Windows these are the
    # same variable, and the worker's value must be the one the child process sees.
    monkeypatch.setenv("HTTPS_PROXY", "http://host-proxy:1")
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    process = backend.build_exec_process(
        backend.ensure_running("proj_001"),
        {"https_proxy": "http://worker-proxy:2"},
        [sys.executable, "-c", "import os,sys; sys.stdout.write(os.environ['HTTPS_PROXY'])"],
        timeout_seconds=10,
    )
    assert sum(1 for key in process.env if key.upper() == "HTTPS_PROXY") == 1
    process.start()
    result = process.communicate(timeout=20)

    assert result.stdout == "http://worker-proxy:2"


@windows_only
def test_redirected_output_is_utf8(tmp_path: Path) -> None:
    # Redirected stderr defaults to the ANSI code page on Windows; Cairn's CLI switches it
    # to UTF-8 so non-Latin fact text stays readable in log files.
    import subprocess

    env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    code = (
        "from cairn.dispatcher.runtime.platform_compat import ensure_utf8_stdio; ensure_utf8_stdio(); "
        "import sys; sys.stderr.write('\\u5df2\\u83b7\\u53d6 \\u2713 \\u2014 ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], stderr=subprocess.PIPE, env=env, timeout=30)

    assert result.stderr.decode("utf-8") == "已获取 ✓ — ok"


def test_popen_kwargs_match_platform() -> None:
    kwargs = platform_compat.popen_creation_kwargs()
    if sys.platform.startswith("win"):
        assert "start_new_session" not in kwargs
        assert kwargs["creationflags"] & platform_compat.subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs == {"start_new_session": True}
