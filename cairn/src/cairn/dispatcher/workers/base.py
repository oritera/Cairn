from __future__ import annotations

import abc
import re
import uuid
from dataclasses import dataclass

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

    def model_id(self, worker: WorkerConfig) -> str | None:
        """Identifier used to price this worker's usage (see cost.py). None if unknown."""
        return None

    def extract_usage(self, stdout: str, stderr: str) -> dict | None:
        """Summed token usage for the task, or None if the driver can't report it.

        Shape: {input, output, cache_read, cache_write, provider_cost}. Token counts
        are summed across every billable model turn (each turn re-bills its context).
        provider_cost is the worker's own cost figure if it reports one, else 0.
        """
        return None


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
