"""Memory bounds for local process output and preview.

The cases that generate 100 MiB streams run in a child process with
RLIMIT_AS. A failing bound should abort that child, not the host.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import threading
import tracemalloc
from pathlib import Path

from cairn.dispatcher.output_parser import extract_json_object, extract_json_object_from_file
from cairn.dispatcher.runtime.local_process import _StreamCapture
from cairn.dispatcher.tasks.common import preview
from cairn.dispatcher.workers.base import search_first_group_in_file
from cairn.dispatcher.workers.adapters.codex import CodexDriver

STREAM_BYTES = 100 * 1024 * 1024
RETAINED_CHARS = 2_000_000
PEAK_ONE_STREAM = 24 * 1024 * 1024
PEAK_BOTH_STREAMS = 40 * 1024 * 1024
PREVIEW_PEAK = 256 * 1024
PREVIEW_INPUT_CHARS = 5_000_000
RSS_DELTA_ONE = 96 * 1024 * 1024
RSS_DELTA_BOTH = 128 * 1024 * 1024
ADDRESS_LIMIT = 512 * 1024 * 1024
LINE = "0123456789abcde\n"  # 16 bytes
SESSION_ID = "abcdefab-1234-5678-9abc-def012345678"
FACT_JSON = '{"accepted":true,"data":{"description":"spool fact"}}'


def _legacy_preview(text: str, limit: int = 1200) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def test_preview_collapses_whitespace_without_a_full_token_list() -> None:
    samples = [
        "  a \n\t b  ",
        "abcd",
        "a b c",
        "ab cd",
        "",
        "   ",
        "word " * 40,
    ]
    for sample in samples:
        assert preview(sample) == _legacy_preview(sample)
        assert preview(sample, limit=3) == _legacy_preview(sample, limit=3)
    assert len(preview("z" * 5000)) <= 1203


def test_capture_compacts_many_short_appends() -> None:
    capture = _StreamCapture()
    tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        for _ in range(100_000):
            capture.append(LINE, 50_000)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    rendered = capture.materialize(Path("/tmp/cairn-short-lines"))
    assert capture.truncated
    assert len(rendered) <= 50_000
    assert "cairn output truncated" in rendered
    assert peak < 1_000_000


def test_json_spool_scan_finds_the_first_object(tmp_path: Path) -> None:
    text = 'noise {not-json} prefix {"accepted": true, "data": {"description": "ok"}} tail'
    path = tmp_path / "out.txt"
    path.write_text(text, encoding="utf-8")
    assert extract_json_object_from_file(str(path)) == extract_json_object(text)


def test_session_pattern_spans_a_chunk_boundary(tmp_path: Path) -> None:
    from cairn.dispatcher.workers.base import RegexSessionDriver

    pattern = RegexSessionDriver.session_pattern
    head = "x" * (65536 - len("session id: "))
    body = head + "session id: " + SESSION_ID + "\n" + ("y" * 100)
    path = tmp_path / "stderr.txt"
    path.write_text(body, encoding="utf-8")
    assert search_first_group_in_file(str(path), pattern) == SESSION_ID


def test_bounded_100mib_stdout(tmp_path: Path) -> None:
    data = _probe("stdout", tmp_path)
    _assert_stream(data, streams=1)


def test_bounded_100mib_stderr(tmp_path: Path) -> None:
    data = _probe("stderr", tmp_path)
    _assert_stream(data, streams=1)


def test_bounded_100mib_short_lines(tmp_path: Path) -> None:
    data = _probe("short-lines", tmp_path)
    _assert_stream(data, streams=1)
    assert data["spool_size"] == STREAM_BYTES


def test_bounded_stdout_and_stderr_together(tmp_path: Path) -> None:
    data = _probe("both", tmp_path)
    _assert_stream(data, streams=2)


def test_timeout_keeps_draining_under_the_cap(tmp_path: Path) -> None:
    data = _probe("timeout", tmp_path)
    assert data["timed_out"] is True
    assert data["child_alive"] is False
    assert data["tracemalloc_peak"] < PEAK_ONE_STREAM
    assert data["rss_delta"] < RSS_DELTA_ONE
    assert data["retained"] <= RETAINED_CHARS
    assert data["spool_size"] > data["retained"]


def test_preview_5_million_solid_characters(tmp_path: Path) -> None:
    data = _probe("preview-solid", tmp_path)
    assert data["length"] <= 1203
    assert data["text"] == ("x" * 1200) + "..."
    assert data["tracemalloc_peak"] < PREVIEW_PEAK
    assert data["rss_delta"] < 8 * 1024 * 1024


def test_preview_5_million_short_words(tmp_path: Path) -> None:
    data = _probe("preview-words", tmp_path)
    assert data["length"] <= 1203
    assert data["text"].startswith("word word")
    assert data["text"].endswith("...")
    assert data["tracemalloc_peak"] < PREVIEW_PEAK
    assert data["rss_delta"] < 8 * 1024 * 1024


def test_codex_session_id_outside_the_retained_window(tmp_path: Path) -> None:
    data = _probe("codex-session", tmp_path)
    assert data["found"] == SESSION_ID
    assert data["in_memory"] is False
    assert data["tracemalloc_peak"] < PEAK_ONE_STREAM
    assert data["rss_delta"] < RSS_DELTA_ONE


def test_codex_json_outside_the_retained_window(tmp_path: Path) -> None:
    data = _probe("codex-json", tmp_path)
    assert data["description"] == "spool fact"
    assert data["json_in_memory"] is False
    assert data["tracemalloc_peak"] < PEAK_ONE_STREAM
    assert data["rss_delta"] < RSS_DELTA_ONE


def test_conclude_fallback_keeps_spools_until_parse_finishes(tmp_path: Path) -> None:
    data = _probe("conclude", tmp_path)
    assert data["outcome"] == "success"
    assert data["description"] == "lifecycle fact"
    assert data["files_seen"] >= 3
    assert data["files_present_at_release"] == data["files_seen"]
    assert data["files_remaining"] == 0
    assert data["child_alive"] is False


def _assert_stream(data: dict, *, streams: int) -> None:
    cap = PEAK_ONE_STREAM if streams == 1 else PEAK_BOTH_STREAMS
    rss = RSS_DELTA_ONE if streams == 1 else RSS_DELTA_BOTH
    assert data["tracemalloc_peak"] < cap
    assert data["rss_delta"] < rss
    assert data["retained"] <= RETAINED_CHARS
    if streams == 2:
        assert data["retained_stderr"] <= RETAINED_CHARS
        assert data["stdout_size"] == STREAM_BYTES
        assert data["stderr_size"] == STREAM_BYTES
    else:
        assert data["spool_size"] == STREAM_BYTES
    assert data["address_limit"] == ADDRESS_LIMIT


def _probe(mode: str, tmp_path: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), mode, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"probe {mode} exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)


def _rss() -> int:
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return 0


def _install_limit() -> int:
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_LIMIT, ADDRESS_LIMIT))
    return ADDRESS_LIMIT


def _measure(fn):
    tracemalloc.start()
    tracemalloc.reset_peak()
    stop = threading.Event()
    samples: list[int] = []

    def sample() -> None:
        while not stop.is_set():
            samples.append(_rss())
            stop.wait(0.05)

    baseline = _rss()
    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        value = fn()
    finally:
        stop.set()
        thread.join(timeout=1)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_peak = max([baseline, *samples, _rss()])
    return value, peak, rss_peak - baseline


def _run_local(command: list[str], cwd: Path, *, timeout_seconds: int = 60, term_grace_seconds: int = 5):
    from cairn.dispatcher.runtime.local_process import LocalProcess

    process = LocalProcess(
        command,
        cwd=str(cwd),
        env=os.environ.copy(),
        timeout_seconds=timeout_seconds,
        term_grace_seconds=term_grace_seconds,
    )
    process.start()
    try:
        result = process.communicate(timeout=timeout_seconds + 30)
    finally:
        if process._process is not None and process._process.poll() is None:
            process.kill()
    return process, result


def _write_bytes_source(stream: str, total: int, *, short_lines: bool = False) -> str:
    line = LINE.encode().decode()
    if short_lines:
        block = (line * (65536 // len(line))).replace("\\", "\\\\")
        # The block is plain ASCII. Embed it as a repeated literal built in the child.
        return f"""
import sys
line = {line!r}
block = (line * (65536 // len(line))).encode()
left = {total}
out = sys.{stream}.buffer
while left:
    take = block if left >= len(block) else (line.encode() * (left // len(line)))
    out.write(take)
    left -= len(take)
"""
    return f"""
import sys
block = b"x" * 65536
left = {total}
out = sys.{stream}.buffer
while left:
    take = block if left >= len(block) else block[:left]
    out.write(take)
    left -= len(take)
"""


def _probe_stream(mode: str, tmp: Path) -> dict:
    if mode == "stdout":
        code = _write_bytes_source("stdout", STREAM_BYTES)
        field = "stdout"
    elif mode == "stderr":
        code = _write_bytes_source("stderr", STREAM_BYTES)
        field = "stderr"
    elif mode == "short-lines":
        code = _write_bytes_source("stdout", STREAM_BYTES, short_lines=True)
        field = "stdout"
    else:
        code = f"""
import sys, threading
def dump(stream):
    block = b"z" * 65536
    left = {STREAM_BYTES}
    while left:
        take = block if left >= len(block) else block[:left]
        stream.write(take)
        left -= len(take)
stdout_thread = threading.Thread(target=dump, args=(sys.stdout.buffer,))
stdout_thread.start()
dump(sys.stderr.buffer)
stdout_thread.join()
"""
        field = "both"
    (value, peak, rss_delta) = _measure(
        lambda: _run_local([sys.executable, "-c", code], tmp)
    )
    process, result = value
    report = {
        "tracemalloc_peak": peak,
        "rss_delta": rss_delta,
        "address_limit": ADDRESS_LIMIT,
    }
    if field == "both":
        report["retained"] = len(result.stdout)
        report["retained_stderr"] = len(result.stderr)
        report["stdout_size"] = Path(result.stdout_path).stat().st_size
        report["stderr_size"] = Path(result.stderr_path).stat().st_size
    else:
        path = Path(result.stdout_path if field == "stdout" else result.stderr_path)
        text = result.stdout if field == "stdout" else result.stderr
        report["retained"] = len(text)
        report["spool_size"] = path.stat().st_size
        report["truncated"] = result.stdout_truncated if field == "stdout" else result.stderr_truncated
    from cairn.dispatcher.tasks.common import release_output_files

    release_output_files(result)
    return report


def _probe_timeout(tmp: Path) -> dict:
    code = """
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
chunk = b"y" * 65536
while True:
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
    time.sleep(0.01)
"""
    def run():
        return _run_local([sys.executable, "-c", code], tmp, timeout_seconds=1, term_grace_seconds=1)

    (process, result), peak, rss_delta = _measure(run)
    pid = process._process.pid if process._process is not None else None
    alive = False
    if pid is not None:
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
    spool_size = Path(result.stdout_path).stat().st_size if result.stdout_path else 0
    from cairn.dispatcher.tasks.common import release_output_files

    release_output_files(result)
    return {
        "timed_out": result.timed_out,
        "child_alive": alive,
        "tracemalloc_peak": peak,
        "rss_delta": rss_delta,
        "retained": len(result.stdout),
        "spool_size": spool_size,
        "address_limit": ADDRESS_LIMIT,
    }


def _probe_codex_session(tmp: Path) -> dict:
    from cairn.dispatcher.tasks.common import release_output_files, resolve_session

    code = f"""
import sys
sys.stderr.write(("a" * 3_000_000) + "session id: {SESSION_ID}\\n" + ("b" * 3_000_000))
"""
    (process, result), peak, rss_delta = _measure(lambda: _run_local([sys.executable, "-c", code], tmp))
    found = resolve_session(CodexDriver(local=True), None, result)
    in_memory = SESSION_ID in result.stderr
    release_output_files(result)
    return {
        "found": found,
        "in_memory": in_memory,
        "tracemalloc_peak": peak,
        "rss_delta": rss_delta,
        "address_limit": ADDRESS_LIMIT,
    }


def _probe_codex_json(tmp: Path) -> dict:
    from cairn.dispatcher.tasks.common import parse_worker_json, release_output_files

    code = f"""
import sys
sys.stdout.write(("a" * 3_000_000) + {FACT_JSON!r} + ("b" * 3_000_000))
"""
    (process, result), peak, rss_delta = _measure(lambda: _run_local([sys.executable, "-c", code], tmp))
    payload = parse_worker_json(CodexDriver(local=True), result)
    release_output_files(result)
    return {
        "description": payload["data"]["description"],
        "json_in_memory": FACT_JSON in result.stdout,
        "tracemalloc_peak": peak,
        "rss_delta": rss_delta,
        "address_limit": ADDRESS_LIMIT,
    }


def _probe_preview(kind: str) -> dict:
    if kind == "preview-solid":
        text = "x" * PREVIEW_INPUT_CHARS
    else:
        text = "word " * (PREVIEW_INPUT_CHARS // 5)
    rendered, peak, rss_delta = _measure(lambda: preview(text))
    return {
        "text": rendered,
        "length": len(rendered),
        "tracemalloc_peak": peak,
        "rss_delta": rss_delta,
        "address_limit": ADDRESS_LIMIT,
    }


def _probe_conclude(tmp: Path) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from conftest import FakeClient, make_intent, make_project

    from cairn.dispatcher.config import DispatchConfig
    from cairn.dispatcher.runtime.cancellation import TaskCancellation
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from cairn.dispatcher.config import LocalConfig
    from cairn.dispatcher.tasks import common, explore

    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(
        f"""#!/usr/bin/env python3
import sys
session = {SESSION_ID!r}
payload = {FACT_JSON.replace("spool fact", "lifecycle fact")!r}
prefix = "a" * 3_000_000
suffix = "b" * 3_000_000
if "resume" in sys.argv:
    if session not in sys.argv:
        sys.stderr.write("missing session\\n")
        raise SystemExit(3)
    sys.stdout.write(prefix + payload + suffix)
    raise SystemExit(0)
sys.stderr.write(prefix + "session id: " + session + "\\n" + suffix)
sys.stdout.write(prefix + suffix)
raise SystemExit(0)
"""
    )
    script.chmod(0o755)
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    common.GRAPH_SNAPSHOT_ROOT = str(tmp / "prompts")
    seen: list[tuple[str, bool, int]] = []
    original = explore.release_output_files

    def wrapped(result) -> None:
        for path in (result.stdout_path, result.stderr_path):
            if not path:
                continue
            file_path = Path(path)
            exists = file_path.exists()
            size = file_path.stat().st_size if exists else 0
            seen.append((path, exists, size))
        original(result)

    explore.release_output_files = wrapped
    config = DispatchConfig.model_validate(
        {
            "server": "in-process",
            "runtime": {
                "execution": "local",
                "worker_healthcheck": "disabled",
                "interval": 60,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 5,
                "prompt_group": "default",
            },
            "tasks": {
                "bootstrap": {"timeout": 30, "conclude_timeout": 10},
                "reason": {"timeout": 30, "max_intents": 3},
                "explore": {"timeout": 30, "conclude_timeout": 10},
            },
            "workers": [
                {
                    "name": "test-worker",
                    "type": "codex",
                    "task_types": ["explore"],
                    "max_running": 1,
                    "priority": 0,
                }
            ],
        }
    )
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp / "work")))
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    outcome = explore.run_explore_task(
        config,
        client,
        backend,
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )
    remaining = 0
    spool_dir = Path(os.environ["CAIRN_LOCAL_OUTPUT_DIR"])
    if spool_dir.exists():
        remaining = len(list(spool_dir.glob("*.stdout"))) + len(list(spool_dir.glob("*.stderr")))
    description = client.concluded[0][3] if client.concluded else ""
    return {
        "outcome": outcome,
        "description": description,
        "files_seen": len(seen),
        "files_present_at_release": sum(1 for _path, exists, _size in seen if exists),
        "files_remaining": remaining,
        "child_alive": False,
        "address_limit": ADDRESS_LIMIT,
    }


def _dispatch(mode: str, tmp: Path) -> dict:
    os.environ["CAIRN_LOCAL_OUTPUT_DIR"] = str(tmp / "spool")
    Path(os.environ["CAIRN_LOCAL_OUTPUT_DIR"]).mkdir(parents=True, exist_ok=True)
    _install_limit()
    if mode in {"stdout", "stderr", "short-lines", "both"}:
        return _probe_stream(mode, tmp)
    if mode == "timeout":
        return _probe_timeout(tmp)
    if mode in {"preview-solid", "preview-words"}:
        return _probe_preview(mode)
    if mode == "codex-session":
        return _probe_codex_session(tmp)
    if mode == "codex-json":
        return _probe_codex_json(tmp)
    if mode == "conclude":
        return _probe_conclude(tmp)
    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    try:
        report = _dispatch(sys.argv[1], Path(sys.argv[2]))
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(1)
    print(json.dumps(report))
