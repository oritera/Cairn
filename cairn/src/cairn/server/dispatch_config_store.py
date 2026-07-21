from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from cairn.dispatcher.config import DispatchConfig, validate_prompt_resources

MASK = "********"
SENSITIVE_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH")
_config_path = Path.cwd() / "dispatch.yaml"


def configure_dispatch_config(path: Path) -> None:
    global _config_path
    _config_path = path.resolve()


def config_path() -> Path:
    return _config_path


def load_redacted_document() -> tuple[str, bool, str | None]:
    path = config_path()
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    redacted, changed = _redact(data)
    rendered = yaml.safe_dump(redacted, sort_keys=False, allow_unicode=True)
    updated = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return rendered, changed, updated


def validate_and_save(text: str) -> tuple[DispatchConfig, bool]:
    path = config_path()
    incoming = yaml.safe_load(text) or {}
    if not isinstance(incoming, dict):
        raise ValueError("dispatch config must be a YAML object")
    current = yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}
    merged = _restore_masks(incoming, current)
    config = DispatchConfig.model_validate(merged)
    validate_prompt_resources(config.runtime.prompt_group)
    restart_required = _immutable_projection(current) != _immutable_projection(merged)
    rendered = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)
    return config, restart_required


def _is_sensitive(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in SENSITIVE_PARTS)


def _redact(value: Any, key: str = "") -> tuple[Any, bool]:
    if key and _is_sensitive(key) and value not in (None, ""):
        return MASK, True
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        changed = False
        for child_key, child_value in value.items():
            result_value, result_changed = _redact(child_value, str(child_key))
            result[child_key] = result_value
            changed = changed or result_changed
        return result, changed
    if isinstance(value, list):
        result = []
        changed = False
        for item in value:
            result_value, result_changed = _redact(item)
            result.append(result_value)
            changed = changed or result_changed
        return result, changed
    return value, False


def _restore_masks(incoming: Any, current: Any, key: str = "") -> Any:
    if incoming == MASK and key and _is_sensitive(key):
        return current
    if isinstance(incoming, dict):
        current_dict = current if isinstance(current, dict) else {}
        return {
            child_key: _restore_masks(child_value, current_dict.get(child_key), str(child_key))
            for child_key, child_value in incoming.items()
        }
    if isinstance(incoming, list):
        current_list = current if isinstance(current, list) else []
        return [
            _restore_masks(item, current_list[index] if index < len(current_list) else None)
            for index, item in enumerate(incoming)
        ]
    return incoming


def _immutable_projection(data: Any) -> tuple[Any, ...]:
    if not isinstance(data, dict):
        return (None,)
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    return (
        data.get("server"),
        runtime.get("execution"),
        data.get("container"),
        data.get("local"),
    )
