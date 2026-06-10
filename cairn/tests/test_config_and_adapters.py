from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cairn.dispatcher.config import DispatchConfig, WorkerConfig, validate_prompt_resources
from cairn.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver
from cairn.dispatcher.workers.adapters.codex import CodexDriver
from cairn.dispatcher.workers.adapters.pi import PiDriver

from conftest import make_config


def test_dispatch_config_merges_common_env_with_worker_override() -> None:
    payload = make_config().model_dump()
    payload["common_env"] = {"SHARED": "common", "OVERRIDE": "common"}
    payload["workers"][0]["env"] = {"OVERRIDE": "worker"}

    config = DispatchConfig.model_validate(payload)

    assert config.workers[0].env["SHARED"] == "common"
    assert config.workers[0].env["OVERRIDE"] == "worker"


def test_dispatch_config_expands_whole_env_var_references(monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_SECRET_FOR_TEST", "expanded")
    payload = make_config().model_dump()
    payload["common_env"] = {"FROM_COMMON": "${CAIRN_SECRET_FOR_TEST}"}
    payload["workers"][0]["env"] = {
        "FROM_WORKER": "$CAIRN_SECRET_FOR_TEST",
        "LITERAL": "prefix-${CAIRN_SECRET_FOR_TEST}",
    }

    config = DispatchConfig.model_validate(payload)

    assert config.workers[0].env["FROM_COMMON"] == "expanded"
    assert config.workers[0].env["FROM_WORKER"] == "expanded"
    assert config.workers[0].env["LITERAL"] == "prefix-${CAIRN_SECRET_FOR_TEST}"


def test_dispatch_config_defaults_worker_healthcheck_and_rejects_unknown_mode() -> None:
    payload = make_config().model_dump()
    payload["runtime"].pop("worker_healthcheck")

    assert DispatchConfig.model_validate(payload).runtime.worker_healthcheck == "startup_only"

    payload["runtime"]["worker_healthcheck"] = "sometimes"
    with pytest.raises(ValidationError):
        DispatchConfig.model_validate(payload)


def test_dispatch_config_rejects_duplicate_workers_and_excess_project_parallelism() -> None:
    payload = make_config().model_dump()
    payload["workers"].append(dict(payload["workers"][0]))
    with pytest.raises(ValidationError, match="worker names must be unique"):
        DispatchConfig.model_validate(payload)

    payload = make_config().model_dump()
    payload["runtime"]["max_project_workers"] = 3
    with pytest.raises(ValidationError, match="max_project_workers cannot exceed max_workers"):
        DispatchConfig.model_validate(payload)


def test_pi_worker_rejects_invalid_context_window() -> None:
    with pytest.raises(ValidationError, match="PI_MODEL_CONTEXT_WINDOW must be greater than 0"):
        WorkerConfig.model_validate(
            {
                "name": "pi",
                "type": "pi",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {
                    "PI_MODEL": "model",
                    "PI_BASE_URL": "http://api",
                    "PI_API_KEY": "secret",
                    "PI_PROVIDER_API": "openai-completions",
                    "PI_MODEL_CONTEXT_WINDOW": "0",
                },
            }
        )


def test_claudecode_api_key_mode_still_requires_anthropic_endpoint_and_token() -> None:
    with pytest.raises(ValidationError, match="ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN"):
        WorkerConfig.model_validate(
            {
                "name": "claude",
                "type": "claudecode",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {"ANTHROPIC_MODEL": "sonnet"},
            }
        )


def test_claudecode_subscription_mode_uses_cli_auth_and_model() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "claude-native",
            "type": "claudecode",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "ANTHROPIC_MODEL": "sonnet",
                "CLAUDE_AUTH_MODE": "subscription",
            },
        }
    )

    driver = ClaudeCodeDriver()
    assert driver.build_healthcheck(worker) == ["claude", "auth", "status", "--text"]
    argv = driver.build_execute(worker, "prompt", "00000000-0000-0000-0000-000000000001").argv

    assert ["--model", "sonnet"] == argv[4:6]
    assert argv[-3:] == ["-p", "--", "prompt"]


def test_mock_worker_rejects_unknown_phase_configuration() -> None:
    with pytest.raises(ValidationError, match="unsupported mock env keys"):
        WorkerConfig.model_validate(
            {
                "name": "mock",
                "type": "mock",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {"MOCK_UNKNOWN": "{}"},
            }
        )


def test_bundled_prompt_groups_have_required_placeholders() -> None:
    validate_prompt_resources("default")
    validate_prompt_resources("mock")


def test_pi_driver_models_json_and_execute_argv_include_context_window_and_tools() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-worker",
            "type": "pi",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
                "PI_MODEL_CONTEXT_WINDOW": "131072",
            },
        }
    )

    result = PiDriver().build_execute(worker, "prompt", None)
    models = json.loads(result.argv[5])

    assert models["providers"]["cairn"]["models"][0]["contextWindow"] == 131072
    assert "--tools" in result.argv
    assert result.argv[-2:] == ["-p", "prompt"]


def test_codex_driver_execute_argv_passes_model_endpoint_and_prompt() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "codex",
            "type": "codex",
            "task_types": ["reason"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "CODEX_MODEL": "gpt-test",
                "CODEX_BASE_URL": "http://api/v1",
                "OPENAI_API_KEY": "secret",
            },
        }
    )

    argv = CodexDriver().build_execute(worker, "prompt", None).argv

    assert "--model" in argv
    assert "gpt-test" in argv
    assert 'model_providers.cairn.base_url="http://api/v1"' in argv
    assert argv[-2:] == ["--", "prompt"]


def test_codex_chatgpt_mode_uses_native_login_without_provider_override() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "codex-native",
            "type": "codex",
            "task_types": ["reason"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "CODEX_MODEL": "gpt-test",
                "CODEX_AUTH_MODE": "chatgpt",
            },
        }
    )

    driver = CodexDriver()
    assert driver.build_healthcheck(worker) == ["codex", "login", "status"]
    argv = driver.build_execute(worker, "prompt", None).argv

    assert "--model" in argv
    assert "gpt-test" in argv
    assert 'model_provider="cairn"' not in argv
    assert all(not arg.startswith("model_providers.cairn.") for arg in argv)
    assert argv[-2:] == ["--", "prompt"]
