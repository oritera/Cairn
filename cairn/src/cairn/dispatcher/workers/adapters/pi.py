from __future__ import annotations

from importlib import resources
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, RuntimeAsset, WorkerDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


# Pi's tool allow-list. The default is the recon/exploration set Cairn has always used. A
# worker can narrow it through PI_TOOLS, which is how a plan-only worker is made genuinely
# read-only: PI_TOOLS="read,grep,find,ls" gives it no execution capability at all.
DEFAULT_PI_TOOLS = "read,write,edit,bash,grep,find,ls"


def resolve_pi_tools(worker: WorkerConfig) -> str:
    """Comma-separated Pi tool allow-list for this worker.

    A blank or whitespace-only PI_TOOLS falls back to DEFAULT_PI_TOOLS. Entries are trimmed
    and empties dropped, so a trailing or doubled comma cannot introduce an empty tool name.
    """
    raw = worker.env.get("PI_TOOLS", "")
    tools = [item.strip() for item in raw.split(",") if item.strip()]
    return ",".join(tools) if tools else DEFAULT_PI_TOOLS


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
        assets = self._extension_assets(worker, local=self.local)
        if self.local:
            return DriverResult(
                argv=self._local_argv(worker, prompt, session),
                session=session,
                assets=assets,
            )
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
        argv.extend(["-p", prompt])
        return DriverResult(
            argv=self._wrap_with_models(worker, argv),
            session=session,
            assets=assets,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> DriverResult:
        assets = self._extension_assets(worker, local=self.local)
        if self.local:
            return DriverResult(
                argv=self._local_argv(worker, prompt, session),
                session=session,
                assets=assets,
            )
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
            "-p",
            prompt,
        ]
        return DriverResult(
            argv=self._wrap_with_models(worker, argv),
            session=session,
            assets=assets,
        )

    def _local_argv(self, worker: WorkerConfig, prompt: str, session: str | None) -> list[str]:
        # Native pi: no models.json injection and no --provider/--model overrides, so pi uses
        # its own host configuration. A tiny sh wrapper just ensures the session dir exists.
        session_dir = self._session_dir(worker)
        pi_argv = [
            "--mode",
            "json",
            "--session-dir",
            session_dir,
            *self._resource_argv(worker, extension_dir=self._extension_dir(worker, local=True)),
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

    def _wrap_with_models(self, worker: WorkerConfig, pi_argv: list[str], *, enable_tools: bool = True) -> list[str]:
        # The dispatcher writes the safety extension into $agent_dir through the Docker API, which
        # creates that directory as root; this wrapper then runs as the container user (kali), so a
        # plain `mkdir -p "$agent_dir/sessions"` fails with EACCES and every task dies at startup.
        # sudo is available (NOPASSWD in the worker image). The chown is deliberately NOT recursive:
        # $agent_dir/cairn-safety holds the safety extension and must stay root-owned, so that the
        # agent cannot rewrite its own gate.
        script = (
            'agent_dir="$1"\n'
            'models_json="$2"\n'
            "shift 2\n"
            'sudo mkdir -p "$agent_dir" "$agent_dir/sessions"\n'
            'sudo chown "$(id -un):$(id -gn)" "$agent_dir" "$agent_dir/sessions"\n'
            'printf "%s" "$models_json" > "$agent_dir/models.json"\n'
            'exec env PI_CODING_AGENT_DIR="$agent_dir" pi "$@"\n'
        )
        argv = self._resource_argv(worker, enable_tools=enable_tools)
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

    @staticmethod
    def _agent_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath("/tmp/cairn-pi") / worker.name)

    @staticmethod
    def _session_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath(PiDriver._agent_dir(worker)) / "sessions")

    @staticmethod
    def _extension_dir(worker: WorkerConfig, *, local: bool = False) -> str:
        if local and os.name == "nt":
            return str(Path(tempfile.gettempdir()) / "cairn-pi" / worker.name / "cairn-safety")
        return str(PurePosixPath(PiDriver._agent_dir(worker)) / "cairn-safety")

    @staticmethod
    def _extension_assets(worker: WorkerConfig, *, local: bool = False) -> tuple[RuntimeAsset, ...]:
        source = resources.files("cairn.safety.pi_extension")
        if local and os.name == "nt":
            target = Path(PiDriver._extension_dir(worker, local=True))
        else:
            target = PurePosixPath(PiDriver._extension_dir(worker))
        return tuple(
            RuntimeAsset(
                path=str(target / name),
                content=source.joinpath(name).read_text(encoding="utf-8"),
            )
            for name in ("index.ts", "transport.mjs")
        )

    @staticmethod
    def _resource_argv(
        worker: WorkerConfig,
        *,
        enable_tools: bool = True,
        extension_dir: str | None = None,
    ) -> list[str]:
        extension_path = str(Path(extension_dir) / "index.ts") if extension_dir and os.name == "nt" else str(
            PurePosixPath(extension_dir or PiDriver._extension_dir(worker)) / "index.ts"
        )
        argv = [
            "--no-extensions",
            "-e",
            extension_path,
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        if enable_tools:
            argv.extend(["--tools", resolve_pi_tools(worker)])
        return argv

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

        provider: dict[str, Any] = {
            "baseUrl": env["PI_BASE_URL"],
            "api": env["PI_PROVIDER_API"],
            "apiKey": env["PI_API_KEY"],
            "models": [model],
        }
        payload = {"providers": {"cairn": provider}}
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
