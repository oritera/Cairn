from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cairn.dispatcher.config import DispatchConfig, WorkerConfig, validate_prompt_resources
from cairn.dispatcher.workers.adapters.coddy import CoddyDriver
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


def test_coddy_driver_builds_headless_one_shot_argv() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "coddy",
            "type": "coddy",
            "task_types": ["bootstrap", "reason", "explore"],
            "max_running": 1,
            "priority": 0,
        }
    )

    argv = CoddyDriver().build_execute(worker, "prompt", None).argv

    # -no-stdin keeps an inherited stdin from blocking the one-shot run, -permission-mode
    # bypass lets the agent use tools without a TTY, -ephemeral leaves no session behind.
    assert argv == [
        "coddy",
        "-no-stdin",
        "-ephemeral",
        "-permission-mode",
        "bypass",
        "-p",
        "prompt",
    ]


def test_coddy_driver_passes_model_override_and_reports_local_binary() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "coddy",
            "type": "coddy",
            "task_types": ["reason"],
            "max_running": 1,
            "priority": 0,
            "env": {"CODDY_MODEL": "llama64080/local64080"},
        }
    )
    driver = CoddyDriver()

    argv = driver.build_execute(worker, "prompt", None).argv

    assert argv[argv.index("-model") + 1] == "llama64080/local64080"
    assert argv[-2:] == ["-p", "prompt"]
    assert driver.local_binary() == "coddy"


def test_coddy_driver_has_no_conclude_phase() -> None:
    driver = CoddyDriver()

    assert driver.supports_conclude() is False
    with pytest.raises(NotImplementedError):
        driver.build_conclude(
            WorkerConfig.model_validate(
                {
                    "name": "coddy",
                    "type": "coddy",
                    "task_types": ["explore"],
                    "max_running": 1,
                    "priority": 0,
                }
            ),
            "prompt",
            "session",
        )


def test_coddy_worker_is_accepted_locally_and_rejected_in_container_mode() -> None:
    payload = make_config().model_dump()
    payload["workers"] = [
        {
            "name": "coddy",
            "type": "coddy",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {},
        }
    ]

    payload["runtime"]["execution"] = "local"
    assert DispatchConfig.model_validate(payload).runtime.execution == "local"

    payload["runtime"]["execution"] = "container"
    with pytest.raises(ValidationError, match="local-only"):
        DispatchConfig.model_validate(payload)
