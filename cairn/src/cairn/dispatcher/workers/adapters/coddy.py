from __future__ import annotations

import shutil

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, WorkerDriver
from cairn.dispatcher.workers.health import HealthResult


class CoddyDriver(WorkerDriver):
    """Coddy CLI worker: https://github.com/coddy-project/coddy-agent.

    Local mode only: the host ``coddy`` binary runs in its own configuration, so no API keys
    or base URLs are injected. ``coddy -p`` prints the final answer to stdout and nothing
    else, which is exactly what the dispatcher reads.
    """

    type_name = "coddy"

    def local_binary(self) -> str | None:
        return "coddy"

    def supports_conclude(self) -> bool:
        # Coddy resumes by workspace ("the most recent session in this folder"), not by session
        # id, so a conclude fallback could attach to another task's run in the same project
        # directory. Tasks run as ephemeral one-shots instead, and the optional conclude
        # fallback stays off.
        return False

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        binary = shutil.which("coddy")
        if binary is None:
            return HealthResult(ok=False, status=None, detail="coddy not found on PATH")
        return HealthResult(ok=True, status=None, detail=f"local CLI {binary}")

    def describe_health(self, worker: WorkerConfig) -> str:
        return "local `coddy -p`"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        # -no-stdin: a one-shot run must never block on inherited stdin (nohup, systemd, CI).
        # -permission-mode bypass: local mode has no TTY that could approve tool calls.
        # -ephemeral: one throwaway session per task, nothing to prune from ~/.coddy later.
        argv = ["coddy", "-no-stdin", "-ephemeral", "-permission-mode", "bypass"]
        model = worker.env.get("CODDY_MODEL")
        if model:
            argv += ["-model", model]
        return DriverResult(argv=[*argv, "-p", prompt])

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        raise NotImplementedError("coddy worker does not support the conclude phase")
