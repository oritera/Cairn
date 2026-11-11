"""Config wizard + supervisor control.

The server never restarts the dispatcher itself — it only writes files. The wizard
generates a dispatcher config from simple form input, validates it with the real
loader, and writes it to disk; "Apply" points the supervisor at it and asks for a
restart. API keys travel browser -> server (the user's own input) and are written
into the gitignored config file, but are NEVER echoed back to the browser: every
read masks them.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.supervisor import DEFAULT_SUPERVISOR_DIR

router = APIRouter(tags=["wizard"])

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_CONFIG_PREFIX = "dispatch.yaml."
_KEY_ENV = ("PI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")


def _config_dir() -> Path:
    return Path(os.environ.get("CAIRN_CONFIG_DIR") or Path.cwd())


def _supervisor_dir() -> Path:
    env = os.environ.get("CAIRN_SUPERVISOR_DIR")
    return Path(env) if env else DEFAULT_SUPERVISOR_DIR


def _config_path(name: str) -> Path:
    if not _NAME_RE.match(name):
        raise HTTPException(400, "bad config name (allowed: letters, digits, _ and -, up to 40)")
    return _config_dir() / f"{_CONFIG_PREFIX}{name}"


def _mask_key(value: str) -> str:
    if not value:
        return ""
    return (value[:3] + "…" + value[-3:]) if len(value) > 8 else "••••"


# --------------------------------------------------------------------------- #
# Presets — convenience defaults the UI shows; everything stays editable and is
# validated server-side regardless. All go through the `pi` worker, which speaks
# OpenAI-compatible and Anthropic backends, so one worker type covers every model.
# --------------------------------------------------------------------------- #
PRESETS = [
    {
        "id": "glm_routerai",
        "label": "GLM · routerai.ru",
        "hint": "Дёшево и быстро. OpenAI-совместимый эндпоинт routerai.",
        "provider_api": "openai-completions",
        "base_url": "https://routerai.ru/api/v1",
        "model": "~z-ai/glm-flash-latest",
        "context_window": 1048576,
        "thinking": "low",
        "thinking_format": "reasoning_effort",
    },
    {
        "id": "openai",
        "label": "OpenAI · GPT",
        "hint": "Официальный OpenAI. Нужен ключ sk-… с platform.openai.com.",
        "provider_api": "openai-completions",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "context_window": 128000,
        "thinking": "low",
        "thinking_format": "reasoning_effort",
    },
    {
        "id": "anthropic",
        "label": "Anthropic · Claude",
        "hint": "Claude напрямую. Ключ с console.anthropic.com.",
        "provider_api": "anthropic-messages",
        "base_url": "https://api.anthropic.com",
        "model": "claude-haiku-4-5",
        "context_window": 200000,
        "thinking": "",
        "thinking_format": "",
    },
    {
        "id": "custom",
        "label": "Свой OpenAI-совместимый",
        "hint": "Любой сервер с /chat/completions: локальный, прокси, другой провайдер.",
        "provider_api": "openai-completions",
        "base_url": "",
        "model": "",
        "context_window": 128000,
        "thinking": "low",
        "thinking_format": "reasoning_effort",
    },
]

FIELD_HELP = {
    "api_key": "Секретный ключ провайдера. Хранится на сервере, в браузер не возвращается.",
    "model": "Идентификатор модели у провайдера (как в их API).",
    "base_url": "Базовый URL API провайдера.",
    "thinking": "Сколько модели «думать». low — бюджетно и быстро; off — без размышлений; выше — дороже/медленнее.",
    "max_workers": "Сколько задач движок выполняет одновременно (на все проекты).",
    "max_running_projects": "Сколько проектов движок ведёт одновременно.",
    "max_project_workers": "Сколько задач одновременно на один проект.",
    "explore_timeout": "Сколько секунд даётся одной задаче explore, прежде чем её срежут.",
    "reason_max_intents": "Сколько новых направлений reason предлагает за раз.",
    "price": "Цена за 1 000 000 токенов (для подсчёта денег на дашборде). Необязательно.",
}


class WizardWorker(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    model: str = ""
    base_url: str = ""
    api_key: str | None = None  # empty on edit => keep existing key
    provider_api: str = "openai-completions"
    context_window: int | None = None
    thinking: str | None = None
    thinking_format: str | None = "reasoning_effort"
    price_input: float | None = None
    price_output: float | None = None
    price_currency: str | None = None
    task_types: list[str] = Field(default_factory=lambda: ["bootstrap", "reason", "explore"])
    max_running: int = 1
    priority: int = 0


class WizardRequest(BaseModel):
    name: str
    server: str = "http://127.0.0.1:8000"
    prompt_group: str = "default"
    execution: str = "container"
    container_image: str = "ghcr.io/oritera/cairn-worker-container:latest"
    network_mode: str = "host"
    interval: int = 3
    healthcheck_timeout: int = 20
    max_workers: int = 2
    max_running_projects: int = 1
    max_project_workers: int = 2
    explore_timeout: int = 240
    explore_conclude_timeout: int = 90
    reason_timeout: int = 240
    reason_max_intents: int = 3
    bootstrap_timeout: int = 120
    bootstrap_conclude_timeout: int = 60
    workers: list[WizardWorker] = Field(min_length=1)


class ApplyRequest(BaseModel):
    name: str


def _worker_env(w: WizardWorker, existing_key: str | None) -> dict:
    key = w.api_key if (w.api_key is not None and w.api_key != "") else (existing_key or "")
    env = {
        "PI_MODEL": w.model,
        "PI_BASE_URL": w.base_url,
        "PI_API_KEY": key,
        "PI_PROVIDER_API": w.provider_api,
    }
    if w.context_window:
        env["PI_MODEL_CONTEXT_WINDOW"] = str(w.context_window)
    if w.thinking:
        env["PI_THINKING"] = w.thinking
    if w.thinking_format:
        env["PI_THINKING_FORMAT"] = w.thinking_format
    if w.price_input is not None:
        env["PI_PRICE_INPUT"] = str(w.price_input)
    if w.price_output is not None:
        env["PI_PRICE_OUTPUT"] = str(w.price_output)
    if w.price_currency:
        env["PI_PRICE_CURRENCY"] = w.price_currency
    return env


def _existing_keys(name: str) -> dict[str, str]:
    """Map worker-name -> API key from an existing config of this name (for edit mode)."""
    path = _config_path(name)
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for w in data.get("workers", []):
            env = w.get("env", {}) or {}
            for k in _KEY_ENV:
                if env.get(k):
                    out[w.get("name", "")] = env[k]
                    break
    except Exception:
        pass
    return out


def _build_config(req: WizardRequest) -> dict:
    prior_keys = _existing_keys(req.name)
    cfg: dict = {
        "server": req.server,
        "runtime": {
            "interval": req.interval,
            "max_workers": req.max_workers,
            "max_running_projects": req.max_running_projects,
            "max_project_workers": req.max_project_workers,
            "healthcheck_timeout": req.healthcheck_timeout,
            "worker_healthcheck": "startup_only",
            "prompt_group": req.prompt_group,
        },
        "tasks": {
            "bootstrap": {"timeout": req.bootstrap_timeout, "conclude_timeout": req.bootstrap_conclude_timeout},
            "reason": {"timeout": req.reason_timeout, "max_intents": req.reason_max_intents},
            "explore": {"timeout": req.explore_timeout, "conclude_timeout": req.explore_conclude_timeout},
        },
        "workers": [
            {
                "name": w.name,
                "type": "pi",
                "task_types": w.task_types,
                "max_running": w.max_running,
                "priority": w.priority,
                "env": _worker_env(w, prior_keys.get(w.name)),
            }
            for w in req.workers
        ],
    }
    if req.execution == "local":
        cfg["runtime"]["execution"] = "local"
    else:
        cfg["container"] = {
            "image": req.container_image,
            "network_mode": req.network_mode,
            "completed_action": "stop",
            "cap_add": ["NET_RAW", "NET_ADMIN"],
        }
    return cfg


def _validate(cfg: dict) -> None:
    """Validate with the real loader by round-tripping through a temp file."""
    tmp = Path(tempfile.mkdtemp()) / "candidate.yaml"
    try:
        tmp.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
        DispatchConfig.load(tmp)
    except Exception as exc:
        raise HTTPException(400, f"config invalid: {exc}")
    finally:
        try:
            tmp.unlink()
            tmp.parent.rmdir()
        except OSError:
            pass


def _masked_yaml(cfg: dict) -> str:
    masked = yaml.safe_load(yaml.safe_dump(cfg))  # deep copy
    for w in masked.get("workers", []):
        env = w.get("env", {})
        for k in _KEY_ENV:
            if env.get(k):
                env[k] = _mask_key(env[k])
    return yaml.safe_dump(masked, sort_keys=False, allow_unicode=True)


@router.get("/wizard/presets")
def get_presets():
    configs = sorted(
        p.name[len(_CONFIG_PREFIX):]
        for p in _config_dir().glob(f"{_CONFIG_PREFIX}*")
        if p.is_file() and _NAME_RE.match(p.name[len(_CONFIG_PREFIX):] or "")
    )
    return {
        "presets": PRESETS,
        "field_help": FIELD_HELP,
        "thinking_levels": ["", "off", "minimal", "low", "medium", "high", "xhigh"],
        "prompt_groups": ["default"],
        "existing_configs": configs,
        "defaults": {
            "server": "http://127.0.0.1:8000",
            "container_image": "ghcr.io/oritera/cairn-worker-container:latest",
        },
    }


@router.get("/wizard/config/{name}")
def get_config(name: str):
    """Structured, key-MASKED view of an existing config, for edit mode."""
    path = _config_path(name)
    if not path.is_file():
        raise HTTPException(404, "config not found")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rt = data.get("runtime", {})
    tasks = data.get("tasks", {})
    workers = []
    for w in data.get("workers", []):
        env = w.get("env", {}) or {}
        has_key = any(env.get(k) for k in _KEY_ENV)
        workers.append(
            {
                "name": w.get("name"),
                "model": env.get("PI_MODEL", ""),
                "base_url": env.get("PI_BASE_URL", ""),
                "provider_api": env.get("PI_PROVIDER_API", "openai-completions"),
                "context_window": int(env["PI_MODEL_CONTEXT_WINDOW"]) if env.get("PI_MODEL_CONTEXT_WINDOW") else None,
                "thinking": env.get("PI_THINKING", ""),
                "thinking_format": env.get("PI_THINKING_FORMAT", ""),
                "price_input": float(env["PI_PRICE_INPUT"]) if env.get("PI_PRICE_INPUT") else None,
                "price_output": float(env["PI_PRICE_OUTPUT"]) if env.get("PI_PRICE_OUTPUT") else None,
                "price_currency": env.get("PI_PRICE_CURRENCY") or None,
                "task_types": w.get("task_types", ["bootstrap", "reason", "explore"]),
                "max_running": w.get("max_running", 1),
                "priority": w.get("priority", 0),
                "has_key": has_key,
                "api_key_masked": _mask_key(next((env[k] for k in _KEY_ENV if env.get(k)), "")),
            }
        )
    return {
        "name": name,
        "server": data.get("server", "http://127.0.0.1:8000"),
        "execution": rt.get("execution", "container"),
        "prompt_group": rt.get("prompt_group", "default"),
        "container_image": (data.get("container") or {}).get("image", ""),
        "network_mode": (data.get("container") or {}).get("network_mode", "host"),
        "interval": rt.get("interval", 3),
        "healthcheck_timeout": rt.get("healthcheck_timeout", 20),
        "max_workers": rt.get("max_workers", 2),
        "max_running_projects": rt.get("max_running_projects", 1),
        "max_project_workers": rt.get("max_project_workers", 2),
        "explore_timeout": (tasks.get("explore") or {}).get("timeout", 240),
        "explore_conclude_timeout": (tasks.get("explore") or {}).get("conclude_timeout", 90),
        "reason_timeout": (tasks.get("reason") or {}).get("timeout", 240),
        "reason_max_intents": (tasks.get("reason") or {}).get("max_intents", 3),
        "bootstrap_timeout": (tasks.get("bootstrap") or {}).get("timeout", 120),
        "bootstrap_conclude_timeout": (tasks.get("bootstrap") or {}).get("conclude_timeout", 60),
        "workers": workers,
    }


@router.post("/wizard/config")
def save_config(req: WizardRequest):
    path = _config_path(req.name)
    cfg = _build_config(req)
    # if any worker still has no key (new config, nothing typed) that's only an error
    # in container mode — the loader will say so; surface it clearly.
    _validate(cfg)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return {
        "name": req.name,
        "path": str(path),
        "masked_preview": _masked_yaml(cfg),
        "apply_hint": "Нажми Apply, чтобы супервизор перезапустил диспетчер с этим конфигом.",
        "manual_command": f"uv run --project cairn cairn dispatch --config {path}",
    }


@router.post("/supervisor/apply")
def apply_config(req: ApplyRequest):
    path = _config_path(req.name)
    if not path.is_file():
        raise HTTPException(404, "config not found — save it first")
    try:
        DispatchConfig.load(path)
    except Exception as exc:
        raise HTTPException(400, f"config invalid: {exc}")
    sup = _supervisor_dir()
    try:
        sup.mkdir(parents=True, exist_ok=True)
        (sup / "active.config").write_text(str(path.resolve()), encoding="utf-8")
        (sup / "restart.request").write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(500, f"cannot signal supervisor: {exc}")
    return {"applied": True, "active_config": str(path.resolve()), "note": "supervisor will restart the dispatcher shortly"}


@router.get("/supervisor/status")
def supervisor_status():
    status_file = _supervisor_dir() / "status.json"
    import json
    from datetime import datetime, timezone

    try:
        raw = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "absent", "reason": "supervisor not running (dispatcher started directly?)", "present": False}
    # staleness: supervisor writes every ~1.5s; >10s => not updating
    age = None
    try:
        upd = datetime.fromisoformat(raw.get("updated_at", "").replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - upd).total_seconds()
    except (ValueError, AttributeError):
        pass
    raw["present"] = True
    raw["age_s"] = age
    raw["stale"] = age is None or age > 10
    return raw
