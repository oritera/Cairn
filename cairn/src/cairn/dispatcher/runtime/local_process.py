from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import uuid
from contextlib import suppress
from pathlib import Path

from cairn.dispatcher.runtime.process import ProcessResult

LOG = logging.getLogger(__name__)

READ_CHUNK_SIZE = 65536
STREAM_JOIN_TIMEOUT_SECONDS = 5.0
FORCE_KILL_REAP_TIMEOUT_SECONDS = 2.0
# 2_000_000 characters, not 2 MiB. 2 * 1024 * 1024 is 2_097_152 and would fail
# the retained-character contract. The budget does not grow with the stream.
DEFAULT_OUTPUT_LIMIT_CHARS = 2_000_000
OUTPUT_HEAD_CHARS = 16 * 1024
_TAIL_COMPACT_CHUNKS = 32


class LocalProcess:
    """Runs a worker command as a host subprocess.

    Mirrors the container ManagedProcess surface (start/communicate/kill/cancel) but
    executes on the dispatcher host: its own process group so children are killed as a
    group, a Python-enforced timeout instead of the ``timeout`` coreutil, and a
    SIGTERM -> grace -> SIGKILL shutdown so the CLI can flush its session before dying.
    """

    def __init__(
        self,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        timeout_seconds: int | None = None,
        term_grace_seconds: int = 5,
        output_limit_chars: int | None = None,
    ):
        self.command = command
        self.env = env
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._term_grace = max(1.0, float(term_grace_seconds))
        self._process: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._kill_lock = threading.Lock()
        self._output_limit = _output_limit(output_limit_chars)
        self._stdout_capture = _StreamCapture()
        self._stderr_capture = _StreamCapture()
        artifact_id = uuid.uuid4().hex[:12]
        spool_dir = _spool_dir(cwd)
        self.stdout_spool = spool_dir / f"{artifact_id}.stdout"
        self.stderr_spool = spool_dir / f"{artifact_id}.stderr"

    def start(self) -> None:
        self._process = subprocess.Popen(
            self.command,
            cwd=self._cwd,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        self._stdout_thread = threading.Thread(
            target=self._drain,
            args=(self._process.stdout, self._stdout_capture, self.stdout_spool),
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain,
            args=(self._process.stderr, self._stderr_capture, self.stderr_spool),
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._process is not None
        wait_for = float(self._timeout_seconds) if self._timeout_seconds is not None else timeout
        try:
            self._process.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            self._timed_out = True
            self._terminate()
        with suppress(subprocess.TimeoutExpired):
            self._process.wait(timeout=FORCE_KILL_REAP_TIMEOUT_SECONDS)
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
        returncode = self._process.returncode
        if returncode is None:
            returncode = 137 if self._timed_out else 1
        return ProcessResult(
            returncode=returncode,
            stdout=self._stdout_capture.materialize(self.stdout_spool),
            stderr=self._stderr_capture.materialize(self.stderr_spool),
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
            stdout_truncated=self._stdout_capture.truncated,
            stderr_truncated=self._stderr_capture.truncated,
            stdout_path=str(self.stdout_spool) if self.stdout_spool.exists() else None,
            stderr_path=str(self.stderr_spool) if self.stderr_spool.exists() else None,
            stdout_spool_incomplete=self._stdout_capture.spool_incomplete,
            stderr_spool_incomplete=self._stderr_capture.spool_incomplete,
        )

    def kill(self) -> None:
        self._terminate()

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self._terminate()

    def _terminate(self) -> None:
        with self._kill_lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=self._term_grace)
                return
            except subprocess.TimeoutExpired:
                pass
            self._signal_group(process, signal.SIGKILL)

    @staticmethod
    def _signal_group(process: subprocess.Popen[str], sig: int) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError):
            with suppress(ProcessLookupError, PermissionError, ValueError):
                process.send_signal(sig)

    def _drain(self, pipe, capture: "_StreamCapture", spool_path: Path) -> None:
        spool = None
        try:
            spool_path.parent.mkdir(parents=True, exist_ok=True)
            spool = spool_path.open("w", encoding="utf-8", errors="replace")
        except OSError as exc:
            LOG.warning("output spool unavailable path=%s error=%s", spool_path, exc)
            capture.note_spool_incomplete()
        try:
            for chunk in iter(lambda: pipe.read(READ_CHUNK_SIZE), ""):
                if spool is not None:
                    try:
                        spool.write(chunk)
                    except OSError as exc:
                        LOG.warning("output spool write failed path=%s error=%s", spool_path, exc)
                        capture.note_spool_incomplete()
                        with suppress(Exception):
                            spool.close()
                        spool = None
                capture.append(chunk, self._output_limit)
        except (ValueError, OSError):
            pass
        finally:
            if spool is not None:
                with suppress(Exception):
                    spool.close()
            with suppress(Exception):
                pipe.close()


class _StreamCapture:
    """Keeps a bounded head and tail while the full stream is written to disk.

    The retained character count never tracks the size of the child output.
    ``materialize`` joins only this bounded buffer. It does not join the spool.
    """

    def __init__(self) -> None:
        self._head = ""
        self._tail: list[str] = []
        self._tail_len = 0
        self._total = 0
        self._truncated = False
        self._spool_incomplete = False
        self._limit = DEFAULT_OUTPUT_LIMIT_CHARS
        self._lock = threading.Lock()

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def spool_incomplete(self) -> bool:
        return self._spool_incomplete

    def note_spool_incomplete(self) -> None:
        with self._lock:
            self._spool_incomplete = True

    def append(self, chunk: str, limit: int) -> None:
        if not chunk:
            return
        with self._lock:
            self._limit = limit
            head_keep = min(OUTPUT_HEAD_CHARS, max(0, limit // 4))
            tail_keep = max(0, limit - head_keep)
            self._total += len(chunk)
            if len(self._head) < head_keep:
                need = head_keep - len(self._head)
                self._head += chunk[:need]
                chunk = chunk[need:]
            if chunk:
                self._tail.append(chunk)
                self._tail_len += len(chunk)
                if len(self._tail) >= _TAIL_COMPACT_CHUNKS:
                    self._tail = ["".join(self._tail)]
            if self._total > head_keep + tail_keep:
                self._truncated = True
                self._trim_tail(tail_keep)

    def _trim_tail(self, tail_keep: int) -> None:
        if tail_keep <= 0:
            self._tail = []
            self._tail_len = 0
            return
        if self._tail_len > tail_keep and len(self._tail) > 1:
            self._tail = ["".join(self._tail)]
        if self._tail and self._tail_len > tail_keep:
            overflow = self._tail_len - tail_keep
            self._tail[0] = self._tail[0][overflow:]
            self._tail_len = tail_keep

    def materialize(self, spool_path: Path) -> str:
        with self._lock:
            tail = "".join(self._tail)
            if not self._truncated:
                return self._head + tail
            head = self._head
            marker = _truncation_marker(
                self._total, len(head) + len(tail), spool_path, incomplete=self._spool_incomplete
            )
            for _ in range(3):
                overflow = len(head) + len(marker) + len(tail) - self._limit
                if overflow <= 0:
                    break
                if len(tail) >= overflow:
                    tail = tail[overflow:]
                else:
                    head = head[: max(0, len(head) - (overflow - len(tail)))]
                    tail = ""
                marker = _truncation_marker(
                    self._total, len(head) + len(tail), spool_path, incomplete=self._spool_incomplete
                )
            if len(marker) >= self._limit:
                return marker[: self._limit]
            return head + marker + tail


def _truncation_marker(total: int, kept: int, spool_path: Path, *, incomplete: bool = False) -> str:
    state = "spool write failed, file is incomplete" if incomplete else "full output"
    return (
        f"\n[cairn output truncated: kept {kept} of {total} chars; "
        f"{state}: {spool_path}]\n"
    )


def spool_root(workspace: Path | None = None) -> Path:
    override = os.environ.get("CAIRN_LOCAL_OUTPUT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    base = workspace if workspace is not None else Path.cwd()
    return base / ".cairn-output"


def sweep_orphan_spools(root: Path) -> int:
    """Delete leftover stdout/stderr spool files. Safe only when no local worker is running."""
    if not root.is_dir():
        return 0
    removed = 0
    try:
        candidates = list(root.rglob("*"))
    except OSError as exc:
        LOG.warning("cannot list spool directory path=%s error=%s", root, exc)
        return 0
    for path in candidates:
        if not path.is_file() or path.suffix not in {".stdout", ".stderr"}:
            continue
        try:
            path.unlink()
        except OSError as exc:
            LOG.warning("cannot remove orphan spool path=%s error=%s", path, exc)
            continue
        removed += 1
    return removed


def _output_limit(explicit: int | None) -> int:
    if explicit is not None:
        return max(4096, explicit)
    raw = os.environ.get("CAIRN_LOCAL_OUTPUT_LIMIT", "")
    if raw.strip():
        try:
            return max(4096, int(raw))
        except ValueError:
            LOG.warning("ignoring invalid CAIRN_LOCAL_OUTPUT_LIMIT=%s", raw)
    return DEFAULT_OUTPUT_LIMIT_CHARS


def _spool_dir(cwd: str) -> Path:
    override = os.environ.get("CAIRN_LOCAL_OUTPUT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    project = Path(cwd)
    return project.parent / ".cairn-output" / project.name
