from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import logging
import socket
import threading
import time
from typing import Any

from docker.errors import APIError, DockerException
from docker.models.containers import Container

LOG = logging.getLogger(__name__)
EXEC_KILL_JOIN_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None


class ManagedProcess:
    def __init__(self, container: Container, command: list[str], env: dict[str, str], *, stdin: str | None = None):
        self.command = command
        self.env = env
        self.stdin = stdin
        self._container = container
        self._api = container.client.api
        self._exec_id: str | None = None
        self._reader: threading.Thread | None = None
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._returncode: int | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._read_error: str | None = None
        self._done = threading.Event()

    def start(self) -> None:
        exec_info = self._api.exec_create(
            self._container.id,
            self.command,
            stdout=True,
            stderr=True,
            stdin=self.stdin is not None,
            tty=False,
            environment=self.env,
        )
        self._exec_id = exec_info["Id"]
        target = self._read_stream if self.stdin is None else self._read_socket_stream
        self._reader = threading.Thread(target=target, daemon=True)
        self._reader.start()

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._reader is not None
        self._reader.join(timeout=timeout)
        if self._reader.is_alive():
            self._timed_out = True
            self.kill()
            self._reader.join(timeout=EXEC_KILL_JOIN_TIMEOUT_SECONDS)
        if self._reader.is_alive():
            if self._returncode is None:
                self._returncode = 137
            self._done.set()
        self._done.wait(timeout=0)
        if self._read_error and not self._stderr:
            self._stderr.append(self._read_error)
        return ProcessResult(
            returncode=self._returncode if self._returncode is not None else 1,
            stdout="".join(self._stdout),
            stderr="".join(self._stderr),
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        if self._exec_id is None:
            return
        try:
            details = self._api.exec_inspect(self._exec_id)
        except DockerException as exc:
            LOG.warning("failed to inspect exec before kill exec_id=%s error=%s", self._exec_id, exc)
            return
        if not details.get("Running"):
            return
        pid = details.get("Pid")
        if not pid:
            LOG.warning("container exec missing pid for kill exec_id=%s", self._exec_id)
            return
        self._kill_pid(int(pid))

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self.kill()

    def _read_stream(self) -> None:
        assert self._exec_id is not None
        try:
            stream = self._api.exec_start(
                self._exec_id,
                detach=False,
                tty=False,
                stream=True,
                demux=True,
            )
            for chunk in stream:
                stdout, stderr = self._split_chunk(chunk)
                if stdout:
                    self._stdout.append(stdout)
                if stderr:
                    self._stderr.append(stderr)
        except DockerException as exc:
            self._read_error = str(exc)
        finally:
            self._returncode = self._resolve_exit_code()
            self._done.set()

    def _read_socket_stream(self) -> None:
        assert self._exec_id is not None
        sock: Any | None = None
        try:
            sock = self._api.exec_start(
                self._exec_id,
                detach=False,
                tty=False,
                socket=True,
            )
            self._set_socket_timeout(sock, None)
            self._send_stdin(sock)
            self._read_multiplexed_socket(sock)
        except DockerException as exc:
            self._read_error = str(exc)
        except OSError as exc:
            self._read_error = str(exc)
        finally:
            if sock is not None:
                self._close_socket_response(sock)
                with suppress(OSError, ValueError):
                    sock.close()
            self._returncode = self._resolve_exit_code()
            self._done.set()

    def _send_stdin(self, sock: Any) -> None:
        data = (self.stdin or "").encode("utf-8")
        if data:
            self._raw_socket(sock).sendall(data)
        try:
            self._raw_socket(sock).shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def _read_multiplexed_socket(self, sock: Any) -> None:
        buffer = b""
        while True:
            raw_sock = self._raw_socket(sock)
            chunk = raw_sock.recv(4096)
            if not chunk:
                return
            buffer += chunk
            while len(buffer) >= 8:
                stream_type = buffer[0]
                frame_size = int.from_bytes(buffer[4:8], byteorder="big")
                if len(buffer) < 8 + frame_size:
                    break
                payload = buffer[8 : 8 + frame_size]
                buffer = buffer[8 + frame_size :]
                if stream_type == 1:
                    self._stdout.append(self._decode(payload))
                elif stream_type == 2:
                    self._stderr.append(self._decode(payload))

    @staticmethod
    def _raw_socket(sock: Any) -> Any:
        return getattr(sock, "_sock", sock)

    @staticmethod
    def _set_socket_timeout(sock: Any, timeout: float | None) -> None:
        raw_sock = ManagedProcess._raw_socket(sock)
        if hasattr(raw_sock, "settimeout"):
            raw_sock.settimeout(timeout)

    @staticmethod
    def _close_socket_response(sock: Any) -> None:
        response = getattr(sock, "_response", None)
        if response is not None:
            with suppress(Exception):
                response.close()

    def _resolve_exit_code(self) -> int:
        assert self._exec_id is not None
        deadline = time.monotonic() + EXEC_KILL_JOIN_TIMEOUT_SECONDS
        while True:
            try:
                details = self._api.exec_inspect(self._exec_id)
            except DockerException as exc:
                if self._read_error is None:
                    self._read_error = str(exc)
                return 137 if self._timed_out else 1
            exit_code = details.get("ExitCode")
            if exit_code is not None:
                return int(exit_code)
            if time.monotonic() >= deadline:
                return 137 if self._timed_out else 1
            time.sleep(0.1)

    def _kill_pid(self, pid: int) -> None:
        last_error: str | None = None
        for command in (
            ["kill", "-KILL", str(pid)],
            ["/bin/sh", "-lc", f"kill -KILL {pid}"],
            ["sh", "-lc", f"kill -KILL {pid}"],
        ):
            try:
                result = self._container.exec_run(command, stdout=False, stderr=False)
            except APIError as exc:
                last_error = str(exc)
                continue
            exit_code = result.exit_code if hasattr(result, "exit_code") else None
            if exit_code in (None, 0, 1):
                return
        if last_error is not None:
            LOG.warning("failed to kill container exec pid=%s container=%s error=%s", pid, self._container.name, last_error)

    @staticmethod
    def _split_chunk(chunk: Any) -> tuple[str, str]:
        if isinstance(chunk, tuple):
            stdout, stderr = chunk
        else:
            stdout, stderr = chunk, None
        return ManagedProcess._decode(stdout), ManagedProcess._decode(stderr)

    @staticmethod
    def _decode(chunk: bytes | str | None) -> str:
        if chunk is None:
            return ""
        if isinstance(chunk, bytes):
            return chunk.decode("utf-8", errors="replace")
        return chunk
