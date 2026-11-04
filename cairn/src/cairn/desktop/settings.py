"""Desktop settings: persisted config, API-key storage, and conversion to a dispatcher config.

Non-secret settings live in ``settings.json``. Any environment value whose *name* looks like
a credential (``*API_KEY``, ``*TOKEN``, ``*SECRET``, ``*PASSWORD`` ...) is stored in the
Windows Credential Manager instead and only merged in, in memory, when the dispatcher is
started (it receives the config over stdin). The UI never receives stored secret values:
it only learns *whether* one is saved.

This module has no GUI dependencies so it can be unit-tested headlessly.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import ValidationError

from cairn.dispatcher.config import DispatchConfig

SETTINGS_VERSION = 1
# Credential Manager "service" name; CAIRN_KEYRING_SERVICE overrides it (isolated test runs).
KEYRING_SERVICE = os.environ.get("CAIRN_KEYRING_SERVICE") or "Cairn"

# An env var is treated as a secret when its name matches this (case-insensitive).
_SECRET_NAME = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)

TASK_TYPES = ("bootstrap", "reason", "explore")

# Per worker type: the env fields the Settings window shows by default. `local` says how the
# value is used when workers run as local CLIs (container mode always needs all of them).
WORKER_TYPES: dict[str, dict[str, Any]] = {
    "claudecode": {
        "label": "Claude Code",
        "binary": "claude",
        "local_note": "Optional in local mode: leave empty to use the claude CLI's own login. "
                      "If set, these are passed to the CLI and override its login.",
        "fields": [
            {"key": "ANTHROPIC_BASE_URL", "label": "API base URL", "placeholder": "https://api.anthropic.com"},
            {"key": "ANTHROPIC_AUTH_TOKEN", "label": "API key / auth token", "placeholder": "sk-..."},
            {"key": "ANTHROPIC_MODEL", "label": "Model", "placeholder": "e.g. claude-opus-5-5"},
        ],
    },
    "codex": {
        "label": "Codex",
        "binary": "codex",
        "local_note": "In local mode the codex CLI uses its own configuration; base URL and model "
                      "are only used in container mode. OPENAI_API_KEY is passed to the CLI if set.",
        "fields": [
            {"key": "CODEX_BASE_URL", "label": "API base URL", "placeholder": "https://api.openai.com/v1"},
            {"key": "OPENAI_API_KEY", "label": "API key", "placeholder": "sk-..."},
            {"key": "CODEX_MODEL", "label": "Model", "placeholder": "e.g. gpt-5"},
        ],
    },
    "pi": {
        "label": "Pi",
        "binary": "pi",
        "local_note": "In local mode the pi CLI uses its own configuration; these fields are only "
                      "used in container mode.",
        "fields": [
            {"key": "PI_BASE_URL", "label": "API base URL", "placeholder": "https://.../v1"},
            {"key": "PI_API_KEY", "label": "API key", "placeholder": "sk-..."},
            {"key": "PI_MODEL", "label": "Model", "placeholder": "model id"},
            {"key": "PI_PROVIDER_API", "label": "Provider API", "placeholder": "openai-completions",
             "choices": ["openai-completions", "openai-responses", "anthropic-messages"]},
            {"key": "PI_MODEL_CONTEXT_WINDOW", "label": "Context window (tokens)", "placeholder": "optional, e.g. 262144"},
        ],
    },
    "mock": {
        "label": "Mock (demo, no LLM)",
        "binary": None,
        "local_note": "Simulated worker for trying Cairn out. Uses no model, account or API key.",
        "fields": [],
    },
}


def is_secret_key(name: str) -> bool:
    return bool(_SECRET_NAME.search(name or ""))


def worker_secret_id(worker_name: str, key: str) -> str:
    return f"worker/{worker_name}/{key}"


def common_secret_id(key: str) -> str:
    return f"common/{key}"


def default_settings(workspace_root: str) -> dict[str, Any]:
    return {
        "version": SETTINGS_VERSION,
        "desktop": {"port": 8000, "autostart_dispatcher": True, "log_level": "INFO"},
        "dispatch": {
            "runtime": {
                "execution": "local",
                "worker_healthcheck": "startup_only",
                "max_workers": 3,
                "max_running_projects": 2,
                "max_project_workers": 2,
                "interval": 3,
                "healthcheck_timeout": 15,
                "prompt_group": "default",
            },
            "tasks": {
                "bootstrap": {"timeout": 120, "conclude_timeout": 30},
                "reason": {"timeout": 45, "max_intents": 3},
                "explore": {"timeout": 600, "conclude_timeout": 120},
            },
            "local": {"workspace_root": workspace_root, "completed_action": "keep"},
            "container": {
                "image": "ghcr.io/oritera/cairn-worker-container:latest",
                "network_mode": "host",
                "completed_action": "stop",
                "cap_add": [],
            },
            "common_env": {},
            "workers": [
                {
                    "name": "local-claude",
                    "type": "claudecode",
                    "task_types": list(TASK_TYPES),
                    "max_running": 1,
                    "priority": 0,
                    "env": {},
                }
            ],
        },
        "secret_refs": [],
    }


# --------------------------------------------------------------------------- secret stores


class SecretStore(Protocol):
    def get(self, secret_id: str) -> str | None: ...

    def set(self, secret_id: str, value: str) -> None: ...

    def delete(self, secret_id: str) -> None: ...


class KeyringSecretStore:
    """Windows Credential Manager (via ``keyring``), one generic credential per secret."""

    def __init__(self, service: str = KEYRING_SERVICE):
        import keyring  # imported lazily: only the desktop app needs it

        self._keyring = keyring
        self._service = service

    def get(self, secret_id: str) -> str | None:
        return self._keyring.get_password(self._service, secret_id)

    def set(self, secret_id: str, value: str) -> None:
        self._keyring.set_password(self._service, secret_id, value)

    def delete(self, secret_id: str) -> None:
        try:
            self._keyring.delete_password(self._service, secret_id)
        except self._keyring.errors.PasswordDeleteError:
            pass


class MemorySecretStore:
    """In-memory store for tests."""

    def __init__(self, initial: dict[str, str] | None = None):
        self.values: dict[str, str] = dict(initial or {})

    def get(self, secret_id: str) -> str | None:
        return self.values.get(secret_id)

    def set(self, secret_id: str, value: str) -> None:
        self.values[secret_id] = value

    def delete(self, secret_id: str) -> None:
        self.values.pop(secret_id, None)


# --------------------------------------------------------------------------- results


@dataclass
class SaveResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    settings: dict[str, Any] | None = None


# --------------------------------------------------------------------------- the store


class SettingsStore:
    def __init__(self, path: Path, secrets: SecretStore, default_workspace: Path):
        self.path = path
        self.secrets = secrets
        self.default_workspace = default_workspace

    # -- persistence ---------------------------------------------------------

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> dict[str, Any]:
        defaults = default_settings(str(self.default_workspace))
        if not self.path.is_file():
            return defaults
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return defaults
        merged = _deep_merge(defaults, data)
        # Lists are replaced, not merged: keep the user's workers exactly.
        if isinstance(data.get("dispatch", {}).get("workers"), list):
            merged["dispatch"]["workers"] = data["dispatch"]["workers"]
        return merged

    def save(self, settings: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(settings, indent=2, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- views ---------------------------------------------------------------

    def public_view(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Settings for the UI: env as editable rows, secrets reduced to 'is one saved?'."""
        view = copy.deepcopy(settings)
        refs = set(settings.get("secret_refs", []))
        dispatch = view["dispatch"]
        for worker in dispatch["workers"]:
            rows = [{"key": k, "value": v, "secret": False, "saved": False} for k, v in worker.get("env", {}).items()]
            for ref in sorted(refs):
                prefix = f"worker/{worker['name']}/"
                if ref.startswith(prefix):
                    rows.append({"key": ref[len(prefix):], "value": "", "secret": True, "saved": True})
            worker["env"] = rows
        common_rows = [{"key": k, "value": v, "secret": False, "saved": False} for k, v in dispatch.get("common_env", {}).items()]
        for ref in sorted(refs):
            if ref.startswith("common/"):
                common_rows.append({"key": ref[len("common/"):], "value": "", "secret": True, "saved": True})
        dispatch["common_env"] = common_rows
        view.pop("secret_refs", None)
        return view

    # -- conversion ----------------------------------------------------------

    def dispatch_dict(self, settings: dict[str, Any], server_url: str,
                      overrides: dict[str, str | None] | None = None) -> dict[str, Any]:
        """Full dispatcher config with secrets merged in. ``overrides`` are pending secret
        values (None = deleted) that take precedence over the store, used to validate a save
        before anything is written."""
        overrides = overrides or {}

        def secret(secret_id: str) -> str | None:
            if secret_id in overrides:
                return overrides[secret_id]
            return self.secrets.get(secret_id)

        src = settings["dispatch"]
        refs = settings.get("secret_refs", [])
        runtime = copy.deepcopy(src["runtime"])
        out: dict[str, Any] = {
            "server": server_url,
            "runtime": runtime,
            "tasks": copy.deepcopy(src["tasks"]),
            "common_env": dict(src.get("common_env", {})),
            "workers": [],
        }
        if runtime.get("execution") == "container":
            container = copy.deepcopy(src["container"])
            container["cap_add"] = [c for c in container.get("cap_add", []) if c]
            out["container"] = container
        else:
            local = copy.deepcopy(src.get("local") or {})
            if not local.get("workspace_root"):
                local["workspace_root"] = str(self.default_workspace)
            out["local"] = local
        for ref in refs:
            if ref.startswith("common/"):
                value = secret(ref)
                if value:
                    out["common_env"][ref[len("common/"):]] = value
        for worker in src["workers"]:
            env = dict(worker.get("env", {}))
            prefix = f"worker/{worker['name']}/"
            for ref in refs:
                if ref.startswith(prefix):
                    value = secret(ref)
                    if value:
                        env[ref[len(prefix):]] = value
            out["workers"].append({
                "name": worker["name"],
                "type": worker["type"],
                "task_types": list(worker["task_types"]),
                "max_running": worker["max_running"],
                "priority": worker["priority"],
                "env": env,
            })
        return out

    def dispatch_yaml(self, settings: dict[str, Any], server_url: str) -> str:
        return yaml.safe_dump(self.dispatch_dict(settings, server_url), sort_keys=False, allow_unicode=True)

    def validate(self, settings: dict[str, Any], overrides: dict[str, str | None] | None = None) -> list[str]:
        errors = _validate_desktop(settings.get("desktop", {}))
        try:
            DispatchConfig.from_dict(self.dispatch_dict(settings, "http://127.0.0.1:8000", overrides))
        except ValidationError as exc:
            errors.extend(_format_validation_errors(exc, settings))
        except ValueError as exc:
            errors.append(str(exc))
        return errors

    # -- updates from the UI ---------------------------------------------------

    def apply_update(self, current: dict[str, Any], payload: dict[str, Any]) -> SaveResult:
        """Apply the Settings window's payload. Nothing is written unless it validates."""
        try:
            new, ops = self._settings_from_payload(current, payload)
        except (KeyError, TypeError, ValueError) as exc:
            return SaveResult(ok=False, errors=[f"Invalid settings: {exc}"])
        errors = self.validate(new, overrides=ops)
        if errors:
            return SaveResult(ok=False, errors=errors)
        self._commit(current, new, ops)
        return SaveResult(ok=True, settings=new)

    def preview_worker_env(self, current: dict[str, Any], worker_payload: dict[str, Any]) -> dict[str, str]:
        """Resolved env for one worker as edited in the UI (for the Test button)."""
        name = str(worker_payload.get("name") or "").strip()
        original = str(worker_payload.get("original_name") or name)
        env: dict[str, str] = {}
        for row in worker_payload.get("env", []):
            key = str(row.get("key") or "").strip()
            if not key:
                continue
            value = _resolve_row(row, self.secrets, worker_secret_id(original, key), current)
            if value:
                env[key] = value
        return env

    def _settings_from_payload(self, current: dict[str, Any], payload: dict[str, Any]):
        new = copy.deepcopy(current)
        new["version"] = SETTINGS_VERSION
        new["desktop"] = _coerce_desktop(payload["desktop"])
        src = payload["dispatch"]
        dispatch = new["dispatch"]
        dispatch["runtime"] = _coerce_ints(src["runtime"], ("max_workers", "max_running_projects",
                                                            "max_project_workers", "interval", "healthcheck_timeout"))
        dispatch["tasks"] = {
            "bootstrap": _coerce_ints(src["tasks"]["bootstrap"], ("timeout", "conclude_timeout")),
            "reason": _coerce_ints(src["tasks"]["reason"], ("timeout", "max_intents")),
            "explore": _coerce_ints(src["tasks"]["explore"], ("timeout", "conclude_timeout")),
        }
        dispatch["local"] = {
            "workspace_root": str(src["local"].get("workspace_root") or "").strip() or str(self.default_workspace),
            "completed_action": src["local"].get("completed_action", "keep"),
        }
        container = dict(src["container"])
        cap_add = container.get("cap_add", [])
        if isinstance(cap_add, str):
            cap_add = [c.strip() for c in cap_add.replace(",", " ").split()]
        container["cap_add"] = [c for c in cap_add if c]
        dispatch["container"] = container

        ops: dict[str, str | None] = {}
        refs: list[str] = []

        common_env: dict[str, str] = {}
        for row in src.get("common_env", []):
            key = str(row.get("key") or "").strip()
            if not key:
                continue
            if is_secret_key(key):
                sid = common_secret_id(key)
                value = _resolve_row(row, self.secrets, sid, current)
                if value:
                    ops[sid] = value
                    refs.append(sid)
            elif str(row.get("value") or "") != "":
                common_env[key] = str(row["value"])
        dispatch["common_env"] = common_env

        workers = []
        for raw in src.get("workers", []):
            name = str(raw.get("name") or "").strip()
            original = str(raw.get("original_name") or name)
            env: dict[str, str] = {}
            for row in raw.get("env", []):
                key = str(row.get("key") or "").strip()
                if not key:
                    continue
                if is_secret_key(key):
                    value = _resolve_row(row, self.secrets, worker_secret_id(original, key), current)
                    if value:
                        sid = worker_secret_id(name, key)
                        ops[sid] = value
                        refs.append(sid)
                elif str(row.get("value") or "") != "":
                    env[key] = str(row["value"])
            workers.append({
                "name": name,
                "type": raw.get("type"),
                "task_types": [t for t in TASK_TYPES if t in (raw.get("task_types") or [])],
                "max_running": _to_int(raw.get("max_running"), "max_running"),
                "priority": _to_int(raw.get("priority"), "priority"),
                "env": env,
            })
        dispatch["workers"] = workers
        new["secret_refs"] = sorted(set(refs))
        # Secrets that existed before but are no longer referenced get deleted.
        for old in current.get("secret_refs", []):
            if old not in new["secret_refs"]:
                ops.setdefault(old, None)
        return new, ops

    def _commit(self, current: dict[str, Any], new: dict[str, Any], ops: dict[str, str | None]) -> None:
        for sid, value in ops.items():
            if value is None:
                self.secrets.delete(sid)
            elif self.secrets.get(sid) != value:
                self.secrets.set(sid, value)
        self.save(new)

    # -- import --------------------------------------------------------------------

    def import_dispatch_yaml(self, current: dict[str, Any], text: str) -> SaveResult:
        """Replace the dispatcher settings with an existing dispatch.yaml (CLI format).
        Credentials found in it are moved into the Credential Manager."""
        try:
            data = yaml.safe_load(text) or {}
            if not isinstance(data, dict):
                raise ValueError("the file does not contain a YAML mapping")
        except (yaml.YAMLError, ValueError) as exc:
            return SaveResult(ok=False, errors=[f"Could not read YAML: {exc}"])
        defaults = default_settings(str(self.default_workspace))["dispatch"]
        # Missing keys take the *CLI's* defaults (RuntimeConfig), not the app's: a dispatch.yaml
        # without runtime.execution means container mode, exactly as `cairn dispatch` reads it.
        cli_runtime_defaults = {"execution": "container", "worker_healthcheck": "startup_only"}
        payload_dispatch = {
            "runtime": {**defaults["runtime"], **cli_runtime_defaults, **(data.get("runtime") or {})},
            "tasks": _deep_merge(defaults["tasks"], data.get("tasks") or {}),
            "local": {**defaults["local"], **{k: v for k, v in (data.get("local") or {}).items() if v is not None}},
            "container": {**defaults["container"], **(data.get("container") or {})},
            "common_env": [{"key": k, "value": str(v), "action": "set"} for k, v in (data.get("common_env") or {}).items()],
            "workers": [
                {
                    "name": w.get("name"),
                    "original_name": w.get("name"),
                    "type": w.get("type"),
                    "task_types": w.get("task_types") or [],
                    "max_running": w.get("max_running", 1),
                    "priority": w.get("priority", 0),
                    "env": [{"key": k, "value": str(v), "action": "set"} for k, v in (w.get("env") or {}).items()],
                }
                for w in (data.get("workers") or [])
                if isinstance(w, dict)
            ],
        }
        payload = {"desktop": current["desktop"], "dispatch": payload_dispatch}
        return self.apply_update(current, payload)


# --------------------------------------------------------------------------- helpers


def _resolve_row(row: dict[str, Any], secrets: SecretStore, existing_id: str, current: dict[str, Any]) -> str | None:
    """Value for a secret row: 'set' uses the typed value, 'clear' deletes, anything else
    keeps whatever is already stored under the row's previous id."""
    action = row.get("action") or "keep"
    typed = str(row.get("value") or "")
    if action == "clear":
        return None
    if action == "set" or typed:
        return typed or None
    if existing_id in set(current.get("secret_refs", [])):
        return secrets.get(existing_id)
    return None


def _to_int(value: Any, name: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number, got {value!r}") from None


def _coerce_ints(src: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out = dict(src)
    for key in keys:
        if key in out:
            out[key] = _to_int(out[key], key)
    return out


def _coerce_desktop(src: dict[str, Any]) -> dict[str, Any]:
    return {
        "port": _to_int(src.get("port", 8000), "port"),
        "autostart_dispatcher": bool(src.get("autostart_dispatcher", True)),
        "log_level": str(src.get("log_level") or "INFO").upper(),
    }


def _validate_desktop(desktop: dict[str, Any]) -> list[str]:
    errors = []
    port = desktop.get("port")
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        errors.append("Server port must be between 1024 and 65535.")
    if desktop.get("log_level") not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        errors.append("Log level must be DEBUG, INFO, WARNING or ERROR.")
    return errors


_FRIENDLY_FIELDS = {
    "max_workers": "Max workers (total)",
    "max_running_projects": "Max running projects",
    "max_project_workers": "Max workers per project",
    "healthcheck_timeout": "Health-check timeout",
    "conclude_timeout": "Conclude timeout",
    "max_intents": "Max new intents per reason step",
    "task_types": "Task types",
    "max_running": "Max running",
}


def _format_validation_errors(exc: ValidationError, settings: dict[str, Any]) -> list[str]:
    workers = settings.get("dispatch", {}).get("workers", [])
    messages = []
    for err in exc.errors():
        loc = list(err.get("loc", ()))
        msg = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
        where = ""
        if loc[:1] == ["workers"] and len(loc) > 1 and isinstance(loc[1], int):
            idx = loc[1]
            name = workers[idx]["name"] if idx < len(workers) else f"#{idx + 1}"
            rest = [str(p) for p in loc[2:]]
            label = _FRIENDLY_FIELDS.get(rest[-1], rest[-1]) if rest else ""
            where = f"Worker '{name or '(unnamed)'}'" + (f" → {label}" if label else "")
        elif loc:
            parts = [str(p) for p in loc]
            where = " → ".join(_FRIENDLY_FIELDS.get(p, p.replace("_", " ")) for p in parts)
        messages.append(f"{where}: {msg}" if where else msg)
    return messages


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out
