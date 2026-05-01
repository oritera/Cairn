from __future__ import annotations

import abc
import re
import shlex
import uuid
from dataclasses import dataclass

from cairn.dispatcher.config import WorkerConfig


@dataclass(slots=True)
class DriverResult:
    argv: list[str]
    session: str | None = None


class WorkerDriver(abc.ABC):
    type_name: str

    def supports_conclude(self) -> bool:
        return True

    def prepare_session(self) -> str | None:
        return None

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return self.build_healthcheck(worker)

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        return shlex.join(self.build_startup_healthcheck(worker))

    @abc.abstractmethod
    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        raise NotImplementedError

    @abc.abstractmethod
    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        raise NotImplementedError

    @abc.abstractmethod
    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        raise NotImplementedError

    def extract_session(self, session: str | None, stderr: str) -> str | None:
        return session


class SeedSessionDriver(WorkerDriver):
    def prepare_session(self) -> str | None:
        return str(uuid.uuid4())


class RegexSessionDriver(WorkerDriver):
    session_pattern = re.compile(r"session id:\s*([0-9a-fA-F-]+)")

    def extract_session(self, session: str | None, stderr: str) -> str | None:
        if session:
            return session
        match = self.session_pattern.search(stderr)
        if match:
            return match.group(1)
        return None
