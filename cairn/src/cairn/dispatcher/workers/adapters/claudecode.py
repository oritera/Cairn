from __future__ import annotations

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.adapters._curl import build_verbose_curl_healthcheck, expand_env, render_curl_command
from cairn.dispatcher.workers.base import DriverResult, SeedSessionDriver


ANTHROPIC_VERSION = "2023-06-01"


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        if self._auth_mode(worker) == "subscription":
            return ["claude", "auth", "status", "--text"]
        env = worker.env
        return [
            "curl",
            "-sS",
            "--fail",
            "-o",
            "/dev/null",
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            "-H",
            f"Authorization: Bearer {env['ANTHROPIC_AUTH_TOKEN']}",
            "-H",
            f"anthropic-version: {ANTHROPIC_VERSION}",
            "-H",
            "content-type: application/json",
            "-d",
            (
                '{"model":"'
                + env["ANTHROPIC_MODEL"]
                + '","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}'
            ),
        ]

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        if self._auth_mode(worker) == "subscription":
            return self.build_healthcheck(worker)
        env = worker.env
        return build_verbose_curl_healthcheck(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers=[
                "-H",
                f"Authorization: Bearer {env['ANTHROPIC_AUTH_TOKEN']}",
                "-H",
                f"anthropic-version: {ANTHROPIC_VERSION}",
                "-H",
                "content-type: application/json",
            ],
            payload=(
                '{"model":"'
                + env["ANTHROPIC_MODEL"]
                + '","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}'
            ),
        )

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        if self._auth_mode(worker) == "subscription":
            return "claude auth status --text"
        env = worker.env
        return render_curl_command(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers=[
                "-H",
                expand_env("Authorization: Bearer $ANTHROPIC_AUTH_TOKEN"),
                "-H",
                f"anthropic-version: {ANTHROPIC_VERSION}",
                "-H",
                "content-type: application/json",
            ],
            payload=(
                '{"model":"'
                + env["ANTHROPIC_MODEL"]
                + '","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}'
            ),
        )

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        argv = [
            "claude",
            "--session-id",
            session,
            "--dangerously-skip-permissions",
        ]
        if self._auth_mode(worker) == "subscription":
            argv.extend(["--model", worker.env["ANTHROPIC_MODEL"]])
        argv.extend(["-p", "--", prompt])
        return DriverResult(argv=argv, session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        argv = [
            "claude",
            "-r",
            session,
            "--dangerously-skip-permissions",
        ]
        if self._auth_mode(worker) == "subscription":
            argv.extend(["--model", worker.env["ANTHROPIC_MODEL"]])
        argv.extend(["-p", "--", prompt])
        return argv

    @staticmethod
    def _auth_mode(worker: WorkerConfig) -> str:
        return worker.env.get("CLAUDE_AUTH_MODE", "api_key")
