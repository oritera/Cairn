from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cairn.dispatcher import config as config_module
from cairn.dispatcher.config import DispatchConfig, WorkerConfig, validate_prompt_resources
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.workers.registry import DRIVERS, LOCAL_DRIVERS

from conftest import make_config


@pytest.mark.parametrize("worker_type", ["claudecode", "codex", "mock"])
def test_production_config_rejects_non_pi_worker(worker_type: str) -> None:
    payload = make_config().model_dump()
    payload["runtime"]["execution"] = "local"
    payload["local"] = {}
    payload["workers"][0]["type"] = worker_type

    with pytest.raises(ValidationError):
        DispatchConfig.model_validate(payload)


def test_production_registry_contains_only_pi() -> None:
    assert set(DRIVERS) == {"pi"}
    assert set(LOCAL_DRIVERS) == {"pi"}


def test_container_config_requires_enabled_safety() -> None:
    payload = make_config().model_dump()
    payload.pop("safety")

    with pytest.raises(ValidationError, match="safety config is required"):
        DispatchConfig.model_validate(payload)


def test_safety_config_uses_documented_resource_defaults() -> None:
    config = make_config()

    assert config.safety is not None
    assert config.safety.model_dump().get("resource_budget") == {
        "max_bulk_concurrency": 2,
        "max_unattended_bulk_seconds": 600,
        "auth_concurrency": 1,
        "auth_attempts_per_minute": 10,
        "auth_attempts_per_batch": 30,
    }
    assert config.safety.model_dump().get("token_env") == "CAIRN_SAFETY_TOKEN"
    assert config.safety.model_dump().get("request_timeout_ms") == 2000
    assert config.safety.model_dump().get("max_payload_bytes") == 65536


@pytest.mark.parametrize(
    ("location", "key"),
    [
        ("common", "CAIRN_SAFETY_ENDPOINT"),
        ("worker", "CAIRN_SAFETY_TOKEN"),
        ("common", "CAIRN_PROJECT_ID"),
        ("worker", "CAIRN_RUN_ID"),
    ],
)
def test_user_config_rejects_reserved_safety_env(location: str, key: str) -> None:
    payload = make_config().model_dump()
    if location == "common":
        payload["common_env"][key] = "attacker-controlled"
    else:
        payload["workers"][0]["env"][key] = "attacker-controlled"

    with pytest.raises(ValidationError, match="reserved safety env"):
        DispatchConfig.model_validate(payload)


def test_missing_safety_token_fails_explicitly(monkeypatch) -> None:
    monkeypatch.delenv("CAIRN_SAFETY_TOKEN", raising=False)
    resolver = getattr(config_module, "resolve_safety_token", None)
    assert resolver is not None
    config = make_config()
    assert config.safety is not None

    with pytest.raises(ValueError, match="missing safety token environment variable"):
        resolver(config.safety)


def test_safety_token_is_read_from_configured_process_env(monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_SAFETY_TOKEN", "  shared-secret  ")
    config = make_config()
    assert config.safety is not None

    assert config_module.resolve_safety_token(config.safety) == "shared-secret"


def test_dispatch_config_merges_common_env_with_worker_override() -> None:
    payload = make_config().model_dump()
    payload["common_env"] = {"SHARED": "common", "OVERRIDE": "common"}
    payload["workers"][0]["env"]["OVERRIDE"] = "worker"

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
