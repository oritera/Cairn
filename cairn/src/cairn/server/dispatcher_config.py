from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi import HTTPException
import requests
import yaml

from cairn.dispatcher.config import DispatchConfig, WorkerConfig, validate_prompt_resources
from cairn.server.models import (
    DispatcherConnectionTestRequest,
    DispatcherConnectionTestResponse,
    DispatcherWorkerConfig,
    UpdateDispatcherWorkerConfigRequest,
)

EDITABLE_WORKER_FIELDS: dict[str, dict[str, str]] = {
    "codex": {
        "model": "CODEX_MODEL",
        "base_url": "CODEX_BASE_URL",
        "api_key": "OPENAI_API_KEY",
        "api_mode": "CODEX_WIRE_API",
    },
    "claudecode": {
        "model": "ANTHROPIC_MODEL",
        "base_url": "ANTHROPIC_BASE_URL",
        "api_key": "ANTHROPIC_AUTH_TOKEN",
    },
    "pi": {
        "model": "PI_MODEL",
        "base_url": "PI_BASE_URL",
        "api_key": "PI_API_KEY",
        "api_mode": "PI_PROVIDER_API",
    },
}

API_MODE_OPTIONS: dict[str, list[str]] = {
    "codex": [
        "responses",
        "chat_completions",
        "qizhi_openai",
        "iq_openai",
        "openai_compatible",
        "dashscope_compatible",
    ],
    "pi": [
        "openai-completions",
        "openai-chat-completions",
        "qizhi_openai",
        "iq_openai",
        "dashscope_compatible",
    ],
}

DEFAULT_EDITABLE_TASK_TYPES = ["bootstrap", "reason", "explore"]
DEFAULT_EDITABLE_MAX_RUNNING = 2
DEFAULT_EDITABLE_PRIORITY = 0
CONNECTION_TEST_PREVIEW_LIMIT = 200


def resolve_dispatch_config_path() -> Path:
    configured = os.getenv("CAIRN_DISPATCH_CONFIG")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    cwd = Path.cwd()
    candidates.extend([cwd / "dispatch.yaml", cwd / "dispatch_local.yaml", cwd / "dispatch.example.yaml"])
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise HTTPException(404, f"Dispatcher config not found. Checked: {searched}")


def load_dispatch_config_document() -> tuple[Path, dict[str, Any]]:
    path = resolve_dispatch_config_path()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise HTTPException(500, f"Failed to read dispatcher config: {exc}") from exc
    if not isinstance(raw, dict):
        raise HTTPException(500, "Dispatcher config root must be an object")
    return path, raw


def build_dispatcher_config_response() -> tuple[Path, list[DispatcherWorkerConfig]]:
    path, raw = load_dispatch_config_document()
    workers = raw.get("workers")
    if not isinstance(workers, list):
        raise HTTPException(500, "Dispatcher config is missing a valid workers list")

    response_workers: list[DispatcherWorkerConfig] = []
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        worker_type = str(worker.get("type", "") or "")
        env = worker.get("env") if isinstance(worker.get("env"), dict) else {}
        field_map = EDITABLE_WORKER_FIELDS.get(worker_type, {})
        response_workers.append(
            DispatcherWorkerConfig(
                name=str(worker.get("name", "") or ""),
                type=worker_type,
                enabled=bool(worker.get("enabled", True)),
                model=_read_env_value(env, field_map.get("model")),
                base_url=_read_env_value(env, field_map.get("base_url")),
                api_key=_read_env_value(env, field_map.get("api_key")),
                api_mode=_read_env_value(env, field_map.get("api_mode")),
                api_mode_options=list(API_MODE_OPTIONS.get(worker_type, [])),
            )
        )
    return path, response_workers


def update_dispatcher_config(workers: list[UpdateDispatcherWorkerConfigRequest]) -> tuple[Path, list[DispatcherWorkerConfig]]:
    path, raw = load_dispatch_config_document()
    updated = deepcopy(raw)
    worker_list = updated.get("workers")
    if not isinstance(worker_list, list):
        raise HTTPException(500, "Dispatcher config is missing a valid workers list")

    existing_by_name = {
        str(worker.get("name", "") or ""): worker
        for worker in worker_list
        if isinstance(worker, dict) and str(worker.get("name", "") or "")
    }
    next_workers: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for incoming in workers:
        if incoming.name in seen_names:
            raise HTTPException(400, f"Duplicate worker profile name: {incoming.name}")
        seen_names.add(incoming.name)

        existing = existing_by_name.get(incoming.name)
        worker_type = incoming.type or (str(existing.get("type", "") or "") if isinstance(existing, dict) else "codex")
        if worker_type not in EDITABLE_WORKER_FIELDS:
            raise HTTPException(400, f"Unsupported editable worker type: {worker_type}")

        worker = deepcopy(existing) if isinstance(existing, dict) else _new_editable_worker(incoming.name, worker_type)
        if str(worker.get("type", "") or "") != worker_type:
            worker = _new_editable_worker(incoming.name, worker_type)
        worker["name"] = incoming.name
        worker["type"] = worker_type
        worker["enabled"] = incoming.enabled
        worker.setdefault("task_types", list(DEFAULT_EDITABLE_TASK_TYPES))
        worker.setdefault("max_running", DEFAULT_EDITABLE_MAX_RUNNING)
        worker.setdefault("priority", DEFAULT_EDITABLE_PRIORITY)

        env = worker.setdefault("env", {})
        if not isinstance(env, dict):
            raise HTTPException(400, f"Worker {incoming.name} has an invalid env section")
        field_map = EDITABLE_WORKER_FIELDS[worker_type]
        _write_env_value(env, field_map.get("model"), incoming.model)
        _write_env_value(env, field_map.get("base_url"), incoming.base_url)
        _write_env_value(env, field_map.get("api_key"), incoming.api_key)
        _write_env_value(env, field_map.get("api_mode"), incoming.api_mode)
        next_workers.append(worker)

    for worker in worker_list:
        if not isinstance(worker, dict):
            continue
        worker_type = str(worker.get("type", "") or "")
        worker_name = str(worker.get("name", "") or "")
        if worker_type in EDITABLE_WORKER_FIELDS:
            continue
        if worker_name in seen_names:
            continue
        next_workers.append(deepcopy(worker))

    updated["workers"] = next_workers

    try:
        validated = DispatchConfig.model_validate(updated)
        validate_prompt_resources(validated.runtime.prompt_group)
    except Exception as exc:
        raise HTTPException(400, f"Updated dispatcher config is invalid: {exc}") from exc

    try:
        path.write_text(
            yaml.safe_dump(updated, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except OSError as exc:
        raise HTTPException(500, f"Failed to write dispatcher config: {exc}") from exc

    reloaded_path, response_workers = build_dispatcher_config_response()
    return reloaded_path, response_workers


def test_dispatcher_api_connection(body: DispatcherConnectionTestRequest) -> DispatcherConnectionTestResponse:
    worker_type = body.type or "codex"
    if worker_type not in EDITABLE_WORKER_FIELDS:
        raise HTTPException(400, f"Unsupported worker type: {worker_type}")

    worker = _build_test_worker(body, worker_type)
    if worker_type == "mock":
        return DispatcherConnectionTestResponse(ok=True, detail="Mock worker does not require an API connection.")

    url, headers, payload = _connection_test_request(worker)
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=body.timeout)
    except requests.RequestException as exc:
        return DispatcherConnectionTestResponse(ok=False, detail=str(exc), response_preview="")

    ok = 200 <= response.status_code < 300
    return DispatcherConnectionTestResponse(
        ok=ok,
        http_status=response.status_code,
        detail="Connection succeeded." if ok else "Connection failed.",
        response_preview=_preview_response(response.text),
    )


def _new_editable_worker(name: str, worker_type: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": worker_type,
        "enabled": True,
        "task_types": list(DEFAULT_EDITABLE_TASK_TYPES),
        "max_running": DEFAULT_EDITABLE_MAX_RUNNING,
        "priority": DEFAULT_EDITABLE_PRIORITY,
        "env": {},
    }


def _build_test_worker(body: DispatcherConnectionTestRequest, worker_type: str) -> WorkerConfig:
    worker = _new_editable_worker(body.name, worker_type)
    worker["env"] = {}
    field_map = EDITABLE_WORKER_FIELDS[worker_type]
    _write_env_value(worker["env"], field_map.get("model"), body.model)
    _write_env_value(worker["env"], field_map.get("base_url"), body.base_url)
    _write_env_value(worker["env"], field_map.get("api_key"), body.api_key)
    _write_env_value(worker["env"], field_map.get("api_mode"), body.api_mode)
    try:
        return WorkerConfig.model_validate(worker)
    except Exception as exc:
        raise HTTPException(400, f"API profile is invalid: {exc}") from exc


def _connection_test_request(worker: WorkerConfig) -> tuple[str, dict[str, str], dict[str, Any]]:
    if worker.type == "claudecode":
        env = worker.env
        return (
            f"{env['ANTHROPIC_BASE_URL'].rstrip('/')}/v1/messages",
            {
                "Authorization": f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            {
                "model": env["ANTHROPIC_MODEL"],
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
    if worker.type == "codex":
        env = worker.env
        base = env["CODEX_BASE_URL"].rstrip("/")
        if _codex_wire_api(env.get("CODEX_WIRE_API")) == "chat_completions":
            return (
                f"{base}/chat/completions",
                _bearer_headers(env["OPENAI_API_KEY"]),
                {"model": env["CODEX_MODEL"], "messages": [{"role": "user", "content": "ping"}], "stream": False},
            )
        return (
            f"{base}/responses",
            _bearer_headers(env["OPENAI_API_KEY"]),
            {"model": env["CODEX_MODEL"], "input": [{"role": "user", "content": "ping"}], "stream": False},
        )
    if worker.type == "pi":
        env = worker.env
        base = env["PI_BASE_URL"].rstrip("/")
        mode = str(env.get("PI_PROVIDER_API", "openai-completions")).strip()
        if mode in {"openai-completions"}:
            return (
                f"{base}/completions",
                _bearer_headers(env["PI_API_KEY"]),
                {"model": env["PI_MODEL"], "prompt": "ping", "max_tokens": 10, "stream": False},
            )
        return (
            f"{base}/chat/completions",
            _bearer_headers(env["PI_API_KEY"]),
            {"model": env["PI_MODEL"], "messages": [{"role": "user", "content": "ping"}], "stream": False},
        )
    raise HTTPException(400, f"Unsupported worker type: {worker.type}")


def _bearer_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}


def _codex_wire_api(value: str | None) -> str:
    wire_api = str(value or "responses").strip().lower()
    if wire_api in {
        "chat_completions",
        "chat-completions",
        "chat.completions",
        "qizhi_openai",
        "qizhi-openai",
        "qizhi.openai",
        "iq_openai",
        "iq-openai",
        "iq.openai",
        "openai_compatible",
        "openai-compatible",
        "openai.compatible",
        "dashscope_compatible",
        "dashscope-compatible",
        "dashscope.compatible",
    }:
        return "chat_completions"
    return "responses"


def _preview_response(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= CONNECTION_TEST_PREVIEW_LIMIT:
        return compact
    return compact[:CONNECTION_TEST_PREVIEW_LIMIT] + "..."


def _read_env_value(env: dict[str, Any], key: str | None) -> str | None:
    if not key:
        return None
    value = env.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _write_env_value(env: dict[str, Any], key: str | None, value: str | None) -> None:
    if not key or value is None:
        return
    env[key] = value.strip()
