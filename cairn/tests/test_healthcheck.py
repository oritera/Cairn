from __future__ import annotations

import requests

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.runtime.startup_healthcheck import run_startup_healthchecks
from cairn.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver
from cairn.dispatcher.workers.adapters.codex import CodexDriver
from cairn.dispatcher.workers.adapters.grok import GrokDriver
from cairn.dispatcher.workers.adapters.mock import MockDriver
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.workers.health import http_ping, proxies_from_env


class _Resp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _capture_post(monkeypatch, response: _Resp) -> dict:
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None, proxies=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout, proxies=proxies)
        return response

    monkeypatch.setattr(requests, "post", fake_post)
    return captured


# --------------------------------------------------------------------------- http_ping


def test_http_ping_healthy_on_2xx(monkeypatch) -> None:
    _capture_post(monkeypatch, _Resp(200, "ok"))
    result = http_ping("http://api/v1/messages", headers={}, json_body={}, timeout=5)
    assert result.ok
    assert result.status == 200


def test_http_ping_unhealthy_on_4xx(monkeypatch) -> None:
    _capture_post(monkeypatch, _Resp(401, "unauthorized token"))
    result = http_ping("http://api/v1/messages", headers={}, json_body={}, timeout=5)
    assert not result.ok
    assert result.status == 401
    assert "unauthorized" in result.detail


def test_http_ping_unhealthy_on_connection_error(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(requests, "post", boom)
    result = http_ping("http://api/v1/messages", headers={}, json_body={}, timeout=5)
    assert not result.ok
    assert result.status is None
    assert "no route" in result.detail


def test_proxies_from_env() -> None:
    assert proxies_from_env({"https_proxy": "http://p:7897"}) == {"https": "http://p:7897"}
    assert proxies_from_env({"all_proxy": "http://p:1"}) == {"http": "http://p:1", "https": "http://p:1"}
    assert proxies_from_env({}) is None


# --------------------------------------------------------------------------- driver requests


def _worker(worker_type: str, env: dict[str, str]) -> WorkerConfig:
    return WorkerConfig.model_validate(
        {"name": worker_type, "type": worker_type, "task_types": ["reason"], "max_running": 1, "priority": 0, "env": env}
    )


def test_claudecode_check_health_hits_messages_with_bearer(monkeypatch) -> None:
    captured = _capture_post(monkeypatch, _Resp(200))
    worker = _worker("claudecode", {"ANTHROPIC_BASE_URL": "http://api", "ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_MODEL": "m"})

    result = ClaudeCodeDriver().check_health(worker, timeout=7)

    assert result.ok
    assert captured["url"] == "http://api/v1/messages"
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["json"]["model"] == "m"
    assert captured["timeout"] == 7


def test_codex_check_health_hits_responses(monkeypatch) -> None:
    captured = _capture_post(monkeypatch, _Resp(200))
    worker = _worker("codex", {"CODEX_BASE_URL": "http://api/v1", "OPENAI_API_KEY": "k", "CODEX_MODEL": "m"})

    CodexDriver().check_health(worker, timeout=5)

    assert captured["url"] == "http://api/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer k"


def test_grok_check_health_hits_chat_completions(monkeypatch) -> None:
    captured = _capture_post(monkeypatch, _Resp(200))
    worker = _worker(
        "grok",
        {"GROK_BASE_URL": "http://api/v1", "XAI_API_KEY": "xai-k", "GROK_MODEL": "grok-4"},
    )

    result = GrokDriver().check_health(worker, timeout=5)

    assert result.ok
    assert captured["url"] == "http://api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer xai-k"
    assert captured["json"]["model"] == "grok-4"


def test_pi_check_health_openai_completions_uses_chat_completions(monkeypatch) -> None:
    captured = _capture_post(monkeypatch, _Resp(200))
    worker = _worker(
        "pi",
        {"PI_BASE_URL": "http://api/v1", "PI_API_KEY": "k", "PI_MODEL": "m", "PI_PROVIDER_API": "openai-completions"},
    )

    PiDriver().check_health(worker, timeout=5)

    assert captured["url"] == "http://api/v1/chat/completions"


def test_pi_check_health_anthropic_uses_messages(monkeypatch) -> None:
    captured = _capture_post(monkeypatch, _Resp(200))
    worker = _worker(
        "pi",
        {"PI_BASE_URL": "http://api", "PI_API_KEY": "k", "PI_MODEL": "m", "PI_PROVIDER_API": "anthropic-messages"},
    )

    PiDriver().check_health(worker, timeout=5)

    assert captured["url"] == "http://api/v1/messages"
    assert captured["headers"]["anthropic-version"]


def test_check_health_uses_worker_proxy(monkeypatch) -> None:
    captured = _capture_post(monkeypatch, _Resp(200))
    worker = _worker(
        "claudecode",
        {"ANTHROPIC_BASE_URL": "http://api", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_MODEL": "m", "https_proxy": "http://127.0.0.1:7897"},
    )

    ClaudeCodeDriver().check_health(worker, timeout=5)

    assert captured["proxies"] == {"https": "http://127.0.0.1:7897"}


def test_mock_check_health_reflects_configured_outcome() -> None:
    ok = _worker("mock", {"MOCK_HEALTHCHECK": '{"delay":[0,0],"outcomes":{"ok":1.0,"fail":0.0}}'})
    fail = _worker("mock", {"MOCK_HEALTHCHECK": '{"delay":[0,0],"outcomes":{"ok":0.0,"fail":1.0}}'})

    assert MockDriver().check_health(ok, timeout=1).ok
    assert not MockDriver().check_health(fail, timeout=1).ok


# --------------------------------------------------------------------------- startup aggregation


def test_run_startup_healthchecks_reports_each_worker() -> None:
    config = DispatchConfig.model_validate(
        {
            "server": "http://127.0.0.1:8000",
            "runtime": {
                "interval": 3,
                "max_workers": 2,
                "max_running_projects": 1,
                "max_project_workers": 2,
                "healthcheck_timeout": 5,
                "prompt_group": "default",
            },
            "tasks": {
                "bootstrap": {"timeout": 10, "conclude_timeout": 5},
                "reason": {"timeout": 10, "max_intents": 3},
                "explore": {"timeout": 10, "conclude_timeout": 5},
            },
            "container": {"image": "img", "network_mode": "host", "completed_action": "stop"},
            "workers": [
                {"name": "ok-worker", "type": "mock", "task_types": ["reason"], "max_running": 1, "priority": 0,
                 "env": {"MOCK_HEALTHCHECK": '{"delay":[0,0],"outcomes":{"ok":1.0,"fail":0.0}}'}},
                {"name": "fail-worker", "type": "mock", "task_types": ["reason"], "max_running": 1, "priority": 0,
                 "env": {"MOCK_HEALTHCHECK": '{"delay":[0,0],"outcomes":{"ok":0.0,"fail":1.0}}'}},
            ],
        }
    )

    results = {r.worker_name: r for r in run_startup_healthchecks(config)}

    assert results["ok-worker"].ok
    assert not results["fail-worker"].ok