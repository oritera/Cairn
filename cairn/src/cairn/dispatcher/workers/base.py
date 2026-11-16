from __future__ import annotations

import abc
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.health import HealthResult


@dataclass(slots=True)
class DriverResult:
    argv: list[str]
    session: str | None = None


class WorkerDriver(abc.ABC):
    type_name: str

    def supports_conclude(self) -> bool:
        return True

    def local_binary(self) -> str | None:
        """Executable this driver invokes in local mode, checked on PATH at startup.

        None means the driver has no host binary to verify (or is not used locally).
        """
        return None

    def prepare_session(self) -> str | None:
        return None

    @abc.abstractmethod
    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        """Verify this worker's LLM config is usable, in-process (no container, no curl)."""
        raise NotImplementedError

    def describe_health(self, worker: WorkerConfig) -> str:
        return "in-process API ping"

    @abc.abstractmethod
    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        raise NotImplementedError

    @abc.abstractmethod
    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        raise NotImplementedError

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        return session

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        return stdout


class SeedSessionDriver(WorkerDriver):
    def prepare_session(self) -> str | None:
        return str(uuid.uuid4())


class RegexSessionDriver(WorkerDriver):
    session_pattern = re.compile(r"session id:\s*([0-9a-fA-F-]+)")

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        match = self.session_pattern.search(stderr)
        if match:
            return match.group(1)
        return None

    def extract_session_from_files(self, stdout_path: str | None, stderr_path: str | None) -> str | None:
        return search_first_group_in_file(stderr_path, self.session_pattern)


def search_first_group_in_file(path: str | None, pattern: re.Pattern[str]) -> str | None:
    """Leftmost regex match over a file, keeping only a chunk plus a small overlap."""
    if not path:
        return None
    try:
        handle = Path(path).open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    overlap = 256
    pending = ""
    with handle:
        while True:
            chunk = handle.read(65536)
            if chunk == "":
                match = pattern.search(pending)
                if match is None:
                    return None
                return match.group(1) if match.lastindex else match.group(0)
            data = pending + chunk
            match = pattern.search(data)
            if match is not None and match.end() < len(data):
                return match.group(1) if match.lastindex else match.group(0)
            if match is not None:
                pending = data[match.start():]
                continue
            pending = data[-overlap:] if len(data) > overlap else data
