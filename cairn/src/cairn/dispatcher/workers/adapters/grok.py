from __future__ import annotations

import json
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, SeedSessionDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


class GrokDriver(SeedSessionDriver):
    """Driver for xAI Grok Build CLI (`grok`).

    Headless usage mirrors Claude Code: seed a session id, run with `-p`, then
    resume the same session for conclude. JSON output wraps the agent reply in a
    top-level `text` field that must be unwrapped before Cairn contract parsing.
    """

    type_name = "grok"

    def __init__(self, local: bool = False):
        self.local = local

    def local_binary(self) -> str | None:
        return "grok"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        return http_ping(
            f"{env['GROK_BASE_URL'].rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {env['XAI_API_KEY']}",
                "content-type": "application/json",
            },
            json_body={
                "model": env["GROK_MODEL"],
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return f"POST {worker.env['GROK_BASE_URL']}/chat/completions (model={worker.env['GROK_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        return DriverResult(argv=self._argv(worker, prompt, session=session, resume=False), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return self._argv(worker, prompt, session=session, resume=True)

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        payload = self._parse_json_payload(stdout)
        session_id = payload.get("sessionId") if payload else None
        if isinstance(session_id, str) and session_id:
            return session_id
        return None

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        payload = self._parse_json_payload(stdout)
        if payload is None:
            return stdout
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text
        return stdout

    def _argv(self, worker: WorkerConfig, prompt: str, *, session: str, resume: bool) -> list[str]:
        argv = [
            "grok",
            "--no-auto-update",
            "--always-approve",
            "--output-format",
            "json",
        ]
        if not self.local:
            argv.extend(["-m", worker.env["GROK_MODEL"]])
        if resume:
            argv.extend(["-r", session])
        else:
            argv.extend(["--session-id", session])
        argv.extend(["-p", prompt])
        return argv

    @staticmethod
    def _parse_json_payload(stdout: str) -> dict[str, Any] | None:
        text = stdout.strip()
        if not text:
            return None
        # Prefer the last JSON object on stdout in case the CLI emits noise first.
        for candidate in reversed([line.strip() for line in text.splitlines() if line.strip()] or [text]):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
