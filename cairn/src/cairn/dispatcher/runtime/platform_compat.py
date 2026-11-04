"""Cross-platform process-spawning and process-tree termination helpers.

Local mode runs worker CLIs as host subprocesses and must be able to kill the whole
tree (the CLI plus whatever it spawns) on timeout or cancellation. POSIX does this with
a session/process group and ``os.killpg``; Windows has neither, so this module abstracts
the two so ``LocalProcess`` stays platform-agnostic.

* POSIX: start the child in its own session (``start_new_session``) and signal the group
  with ``SIGTERM`` -> grace -> ``SIGKILL``.
* Windows: start the child in a new process group (``CREATE_NEW_PROCESS_GROUP`` plus
  ``CREATE_NO_WINDOW``) and terminate the tree with ``taskkill /T``: ``/T`` first for a
  graceful close, then ``/T /F`` to force-kill anything still alive.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping
from contextlib import suppress

IS_WINDOWS = sys.platform.startswith("win")


def attach_missing_stdio() -> None:
    """Give a windowless child somewhere to log.

    A process started from a GUI (no console) can come up with ``sys.stdout`` /
    ``sys.stderr`` set to None, which silently drops logging and tracebacks. When the parent
    named a log file in ``CAIRN_CHILD_LOG``, append to it instead.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    target = os.environ.get("CAIRN_CHILD_LOG")
    if not target:
        return
    try:
        stream = open(target, "a", encoding="utf-8", errors="backslashreplace", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def ensure_utf8_stdio() -> None:
    """Make redirected stdout/stderr UTF-8 on Windows.

    When output goes to a file, pipe or service log, Python on Windows encodes it in the
    ANSI code page (e.g. cp1252): non-Latin text such as Chinese fact descriptions turns
    into ``\\u5df2\\u83b7`` escapes and symbols like an em dash become bytes that are not
    valid UTF-8. A real console already receives Unicode correctly and is left alone, as
    is an explicit PYTHONIOENCODING / UTF-8 mode choice by the user.
    """
    if not IS_WINDOWS or os.environ.get("PYTHONIOENCODING") or sys.flags.utf8_mode:
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if stream.isatty():
                continue
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass


def popen_creation_kwargs() -> dict:
    """Keyword args for ``subprocess.Popen`` that isolate the child's process group.

    On both platforms this guarantees the child (and its descendants) can be addressed and
    killed as a unit without touching the dispatcher itself.
    """
    if IS_WINDOWS:
        flags = 0
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def merge_env(base: Mapping[str, str], overrides: Mapping[str, str] | None) -> dict[str, str]:
    """Overlay ``overrides`` on ``base`` the way the OS will read the result.

    Windows environment names are case-insensitive and ``os.environ`` reports them
    upper-cased, so a naive ``{**os.environ, **env}`` can hold both ``HTTPS_PROXY`` (host)
    and ``https_proxy`` (worker config); which one the child sees is then undefined. Fold
    names to upper case on Windows so a worker override always wins. POSIX is unchanged.
    """
    if not IS_WINDOWS:
        return {**base, **(overrides or {})}
    merged = {key.upper(): value for key, value in base.items()}
    for key, value in (overrides or {}).items():
        merged[key.upper()] = value
    return merged


def resolve_executable(command: list[str]) -> list[str]:
    """Return ``command`` with argv[0] resolved to a concrete path when needed.

    On Windows a CLI installed as ``codex.cmd`` / ``pi.cmd`` is not found by
    ``CreateProcess`` from the bare name (it only appends ``.exe``), so resolve it through
    ``PATHEXT`` with ``shutil.which``; npm ``.cmd`` shims are then unwrapped to a direct
    ``node script.js`` launch (see ``unwrap_npm_cmd_shim``). On POSIX this is a PATH lookup
    that returns the same program the bare name would run.
    """
    if not command:
        return command
    exe = command[0]
    if os.path.isabs(exe) or os.sep in exe or (os.altsep and os.altsep in exe):
        resolved = exe
    else:
        resolved = shutil.which(exe) or exe
    if IS_WINDOWS and resolved.lower().endswith((".cmd", ".bat")):
        unwrapped = unwrap_npm_cmd_shim(resolved)
        if unwrapped is not None:
            return [*unwrapped, *command[1:]]
    return [resolved, *command[1:]]


# npm's cmd-shim ends with:  "%_prog%"  "%dp0%\node_modules\pkg\bin\cli.js" %*
_NPM_SHIM_TARGET = re.compile(r'"%dp0%\\([^"]+)"\s+%\*', re.IGNORECASE)
_JS_SUFFIXES = (".js", ".mjs", ".cjs")


def unwrap_npm_cmd_shim(shim_path: str) -> list[str] | None:
    """Turn an npm ``.cmd`` shim into the direct ``[node, script.js]`` it would run.

    Worker prompts are multi-line and full of quotes, ``&``, ``|`` and ``%``. Passed through
    a ``.cmd`` file they are re-parsed by ``cmd.exe``, which truncates at the first newline
    and mangles metacharacters. Launching node (or the shim's native target) directly skips
    ``cmd.exe`` entirely so the prompt arrives byte-for-byte. Returns None when the file is
    not a recognisable npm shim, in which case the caller falls back to the shim itself.
    """
    try:
        text = open(shim_path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    match = _NPM_SHIM_TARGET.search(text)
    if match is None:
        return None
    shim_dir = os.path.dirname(os.path.abspath(shim_path))
    target = os.path.normpath(os.path.join(shim_dir, match.group(1)))
    if not os.path.isfile(target):
        return None
    if not target.lower().endswith(_JS_SUFFIXES):
        return [target]  # shim wraps a native executable: run it directly
    bundled_node = os.path.join(shim_dir, "node.exe")
    node = bundled_node if os.path.isfile(bundled_node) else shutil.which("node")
    if node is None:
        return None
    return [node, target]


def terminate_tree(process: subprocess.Popen, grace_seconds: float) -> None:
    """Terminate ``process`` and all of its descendants.

    First asks politely so the CLI can flush its session, waits ``grace_seconds``, then
    forces the kill if anything survives. Safe to call when the process has already exited.
    """
    if process.poll() is not None:
        return
    if IS_WINDOWS:
        _terminate_tree_windows(process, grace_seconds)
    else:
        _terminate_tree_posix(process, grace_seconds)


def _terminate_tree_windows(process: subprocess.Popen, grace_seconds: float) -> None:
    taskkill = shutil.which("taskkill")
    if taskkill is None:
        # No taskkill (unusual): fall back to the single-process terminate/kill.
        _terminate_single(process, grace_seconds)
        return
    # Graceful: taskkill without /F closes the tree, letting the CLI exit cleanly.
    _run_taskkill(taskkill, process.pid, force=False)
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    # Force: /F kills the whole tree outright.
    _run_taskkill(taskkill, process.pid, force=True)


def _run_taskkill(taskkill: str, pid: int, *, force: bool) -> None:
    command = [taskkill, "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _terminate_tree_posix(process: subprocess.Popen, grace_seconds: float) -> None:
    if _signal_group(process, signal.SIGTERM):
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
    _signal_group(process, signal.SIGKILL)


def _signal_group(process: subprocess.Popen, sig: int) -> bool:
    try:
        os.killpg(os.getpgid(process.pid), sig)
        return True
    except (ProcessLookupError, PermissionError):
        with suppress(ProcessLookupError, PermissionError, ValueError):
            process.send_signal(sig)
    except (AttributeError, OSError):
        with suppress(ProcessLookupError, PermissionError, ValueError, OSError):
            process.send_signal(sig)
    return False


def _terminate_single(process: subprocess.Popen, grace_seconds: float) -> None:
    with suppress(OSError):
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(OSError):
        process.kill()
