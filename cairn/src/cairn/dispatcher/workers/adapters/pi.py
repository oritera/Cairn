from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, WorkerDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class PiDriver(WorkerDriver):
    type_name = "pi"

    def __init__(self, local: bool = False):
        self.local = local

    def local_binary(self) -> str | None:
        return "pi"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        base = env["PI_BASE_URL"].rstrip("/")
        model = env["PI_MODEL"]
        api = env["PI_PROVIDER_API"]
        proxies = proxies_from_env(env)
        headers = {"Authorization": f"Bearer {env['PI_API_KEY']}", "content-type": "application/json"}
        if "anthropic" in api:
            return http_ping(
                f"{base}/v1/messages",
                headers={**headers, "anthropic-version": "2023-06-01"},
                json_body={"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "ping"}]},
                timeout=timeout,
                proxies=proxies,
            )
        if "responses" in api:
            return http_ping(
                f"{base}/responses",
                headers=headers,
                json_body={"model": model, "input": [{"role": "user", "content": "ping"}], "stream": False},
                timeout=timeout,
                proxies=proxies,
            )
        # openai-completions and anything else: OpenAI-compatible chat/completions
        return http_ping(
            f"{base}/chat/completions",
            headers=headers,
            json_body={"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "ping"}]},
            timeout=timeout,
            proxies=proxies,
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        env = worker.env
        return f"POST {env['PI_BASE_URL']} (api={env['PI_PROVIDER_API']}, model={env['PI_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        if self.local:
            return DriverResult(argv=self._local_argv(worker, prompt, session), session=session)
        env = worker.env
        argv = [
            "--provider",
            "cairn",
            "--model",
            env["PI_MODEL"],
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
        ]
        if session:
            argv.extend(["--session", session])
        argv.extend(self._thinking_args(worker))
        argv.extend(["-p", prompt])
        return DriverResult(argv=self._wrap_with_models(worker, argv), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        if self.local:
            return self._local_argv(worker, prompt, session)
        env = worker.env
        argv = [
            "--provider",
            "cairn",
            "--model",
            env["PI_MODEL"],
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
            "--session",
            session,
        ]
        argv.extend(self._thinking_args(worker))
        argv.extend(["-p", prompt])
        return self._wrap_with_models(worker, argv)

    def _local_argv(self, worker: WorkerConfig, prompt: str, session: str | None) -> list[str]:
        # Native pi: no models.json injection and no --provider/--model overrides, so pi uses
        # its own host configuration. A tiny sh wrapper just ensures the session dir exists.
        session_dir = self._session_dir(worker)
        pi_argv = [
            "--mode",
            "json",
            "--session-dir",
            session_dir,
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--tools",
            "read,write,edit,bash,grep,find,ls",
        ]
        if session:
            pi_argv.extend(["--session", session])
        pi_argv.extend(["-p", prompt])
        script = 'sdir="$1"\nshift\nmkdir -p "$sdir"\nexec pi "$@"\n'
        return ["/bin/sh", "-lc", script, "--", session_dir, *pi_argv]

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for event in self._iter_events(stdout):
            if event.get("type") != "session":
                continue
            session_id = event.get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
        return None

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        assistant_message: dict[str, Any] | None = None
        for event in self._iter_events(stdout):
            event_type = event.get("type")
            if event_type == "turn_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    assistant_message = message
            elif event_type == "agent_end":
                messages = event.get("messages")
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get("role") == "assistant":
                            assistant_message = message
                            break
        if assistant_message is None:
            return stdout
        content = assistant_message.get("content")
        if not isinstance(content, list):
            return stdout
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip() or stdout

    def model_id(self, worker: WorkerConfig) -> str | None:
        return worker.env.get("PI_MODEL")

    def extract_usage(self, stdout: str, stderr: str) -> dict | None:
        """Sum pi's normalized usage across every `turn_end` (one billable model call
        each). pi reports the same `{input,output,cacheRead,cacheWrite,cost}` shape for
        every backend (OpenAI/Anthropic/GLM), so this is provider-agnostic."""
        total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "provider_cost": 0.0}
        seen = False
        for event in self._iter_events(stdout):
            if event.get("type") != "turn_end":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                message = event.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue
            seen = True
            total["input"] += _as_int(usage.get("input"))
            total["output"] += _as_int(usage.get("output"))
            total["cache_read"] += _as_int(usage.get("cacheRead"))
            total["cache_write"] += _as_int(usage.get("cacheWrite"))
            cost = usage.get("cost")
            if isinstance(cost, dict):
                total["provider_cost"] += _as_float(cost.get("total"))
        return total if seen else None

    def _wrap_with_models(self, worker: WorkerConfig, pi_argv: list[str], *, enable_tools: bool = True) -> list[str]:
        script = (
            'agent_dir="$1"\n'
            'models_json="$2"\n'
            "shift 2\n"
            'mkdir -p "$agent_dir"\n'
            'mkdir -p "$agent_dir/sessions"\n'
            'printf "%s" "$models_json" > "$agent_dir/models.json"\n'
            'exec env PI_CODING_AGENT_DIR="$agent_dir" pi "$@"\n'
        )
        argv = [
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        if enable_tools:
            argv.extend(["--tools", "read,write,edit,bash,grep,find,ls"])
        return [
            "/bin/sh",
            "-lc",
            script,
            "--",
            self._agent_dir(worker),
            self._models_json(worker),
            *argv,
            *pi_argv,
        ]

    _THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh"}
    _THINKING_FORMATS = {"reasoning_effort", "zai", "deepseek", "qwen", "qwen-chat-template"}

    @staticmethod
    def _thinking_level(worker: WorkerConfig) -> str | None:
        """Resolve the pi thinking level from env, or None to leave pi untouched.

        PI_THINKING: off|minimal|low|medium|high|xhigh, or none/default/"" to disable the
        knob entirely (pre-existing behaviour). When unset, default to "low" for
        OpenAI-compatible completion backends (GLM/Qwen/etc.) — where the thinking burn
        bites — and stay out of the way for anthropic/responses APIs, which pi handles
        natively.
        """
        env = worker.env
        raw = env.get("PI_THINKING")
        if raw is None:
            api = (env.get("PI_PROVIDER_API") or "").lower()
            if "anthropic" in api or "responses" in api:
                return None
            raw = "low"
        raw = raw.strip().lower()
        if raw in ("", "none", "default", "unset"):
            return None
        return raw if raw in PiDriver._THINKING_LEVELS else "low"

    @staticmethod
    def _thinking_format(worker: WorkerConfig) -> str:
        raw = (worker.env.get("PI_THINKING_FORMAT") or "reasoning_effort").strip()
        return raw if raw in PiDriver._THINKING_FORMATS else "reasoning_effort"

    @staticmethod
    def _thinking_args(worker: WorkerConfig) -> list[str]:
        level = PiDriver._thinking_level(worker)
        return ["--thinking", level] if level is not None else []

    @staticmethod
    def _agent_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath("/tmp/cairn-pi") / worker.name)

    @staticmethod
    def _session_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath(PiDriver._agent_dir(worker)) / "sessions")

    @staticmethod
    def _iter_events(stdout: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    @staticmethod
    def _models_json(worker: WorkerConfig) -> str:
        env = worker.env
        model: dict[str, Any] = {
            "id": env["PI_MODEL"],
            "name": env["PI_MODEL"],
        }
        context_window = env.get("PI_MODEL_CONTEXT_WINDOW")
        if context_window:
            model["contextWindow"] = int(context_window)

        # Thinking control. GLM-flash on routerai defaults to "thinking", which on large
        # explore prompts burns the output budget on reasoning — slow turns, and at a small
        # max_tokens even empty answers (finish=length). pi only forwards a thinking knob
        # when the model is marked as a reasoning model with an explicit thinkingFormat;
        # then `--thinking <level>` (see _thinking_args) maps to the provider param. Validated
        # against routerai: reasoning_effort:"low" keeps answers, ~3x faster, far cheaper.
        level = PiDriver._thinking_level(worker)
        if level is not None:
            model["reasoning"] = True
            model["compat"] = {"thinkingFormat": PiDriver._thinking_format(worker)}

        provider: dict[str, Any] = {
            "baseUrl": env["PI_BASE_URL"],
            "api": env["PI_PROVIDER_API"],
            "apiKey": env["PI_API_KEY"],
            "models": [model],
        }
        payload = {"providers": {"cairn": provider}}
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
