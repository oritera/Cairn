from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi import HTTPException
import yaml

from cairn.dispatcher.config import DispatchConfig, validate_prompt_resources
from cairn.server.models import DispatcherWorkerConfig, UpdateDispatcherWorkerConfigRequest

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

    updates_by_name = {worker.name: worker for worker in workers}
    for worker in worker_list:
        if not isinstance(worker, dict):
            continue
        worker_name = str(worker.get("name", "") or "")
        incoming = updates_by_name.get(worker_name)
        if incoming is None:
            continue
        worker_type = str(worker.get("type", "") or "")
        field_map = EDITABLE_WORKER_FIELDS.get(worker_type)
        if not field_map:
            continue
        env = worker.setdefault("env", {})
        if not isinstance(env, dict):
            raise HTTPException(400, f"Worker {worker_name} has an invalid env section")
        _write_env_value(env, field_map.get("model"), incoming.model)
        _write_env_value(env, field_map.get("base_url"), incoming.base_url)
        _write_env_value(env, field_map.get("api_key"), incoming.api_key)
        _write_env_value(env, field_map.get("api_mode"), incoming.api_mode)

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
