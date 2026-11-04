"""Desktop app logic that runs without a GUI: settings, API-key storage, dispatcher supervision."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from cairn.desktop.processes import DispatcherProcess, pick_port, port_is_free
from cairn.desktop.settings import (
    MemorySecretStore,
    SettingsStore,
    default_settings,
    is_secret_key,
    worker_secret_id,
)
from cairn.dispatcher.config import DispatchConfig

SERVER = "http://127.0.0.1:8000"


@pytest.fixture()
def store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "settings.json", MemorySecretStore(), tmp_path / "projects")


def ui_payload(store: SettingsStore, settings: dict, **worker_overrides) -> dict:
    """What the Settings window sends back: the public view, with env as rows + actions."""
    view = store.public_view(settings)
    dispatch = view["dispatch"]
    for worker in dispatch["workers"]:
        worker["original_name"] = worker["name"]
        for row in worker["env"]:
            row["action"] = "keep"
    for row in dispatch["common_env"]:
        row["action"] = "keep"
    if worker_overrides:
        dispatch["workers"][0].update(worker_overrides)
    return {"desktop": view["desktop"], "dispatch": dispatch}


def set_key(payload: dict, key: str, value: str, worker: int = 0) -> None:
    rows = payload["dispatch"]["workers"][worker]["env"]
    rows[:] = [r for r in rows if r["key"] != key]
    rows.append({"key": key, "value": value, "action": "set"})


# ----------------------------------------------------------------------- basics


def test_secret_names_are_detected() -> None:
    for name in ("ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "PI_API_KEY", "MY_SECRET", "DB_PASSWORD", "apikey"):
        assert is_secret_key(name), name
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "HTTPS_PROXY", "PI_MODEL_CONTEXT_WINDOW"):
        assert not is_secret_key(name), name


def test_defaults_are_a_valid_local_config(store: SettingsStore) -> None:
    settings = store.load()
    assert settings["dispatch"]["runtime"]["execution"] == "local"
    assert store.validate(settings) == []
    config = DispatchConfig.from_yaml_text(store.dispatch_yaml(settings, SERVER))
    assert config.local is not None and config.local.workspace_root == str(store.default_workspace)
    assert [w.name for w in config.workers] == ["local-claude"]


def test_missing_or_corrupt_file_falls_back_to_defaults(store: SettingsStore) -> None:
    assert not store.exists
    store.path.write_text("{ not json", encoding="utf-8")
    assert store.load()["dispatch"]["workers"][0]["name"] == "local-claude"


# ----------------------------------------------------------------------- secrets


def test_api_key_goes_to_secret_store_not_to_disk(store: SettingsStore) -> None:
    current = store.load()
    payload = ui_payload(store, current)
    set_key(payload, "ANTHROPIC_AUTH_TOKEN", "sk-live-123")
    set_key(payload, "ANTHROPIC_MODEL", "claude-opus-5-5")

    result = store.apply_update(current, payload)

    assert result.ok, result.errors
    on_disk = store.path.read_text(encoding="utf-8")
    assert "sk-live-123" not in on_disk
    assert "claude-opus-5-5" in on_disk  # non-secret values are kept in the file
    assert store.secrets.get(worker_secret_id("local-claude", "ANTHROPIC_AUTH_TOKEN")) == "sk-live-123"
    # The UI only learns that a key is saved, never its value.
    view = store.public_view(store.load())
    rows = {r["key"]: r for r in view["dispatch"]["workers"][0]["env"]}
    assert rows["ANTHROPIC_AUTH_TOKEN"] == {"key": "ANTHROPIC_AUTH_TOKEN", "value": "", "secret": True, "saved": True}
    assert "sk-live-123" not in json.dumps(view)
    # ...but the dispatcher config built in memory has it.
    built = store.dispatch_dict(store.load(), SERVER)
    assert built["workers"][0]["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-live-123"


def test_saved_key_is_kept_when_field_left_empty(store: SettingsStore) -> None:
    current = store.load()
    payload = ui_payload(store, current)
    set_key(payload, "OPENAI_API_KEY", "sk-1")
    current = store.apply_update(current, payload).settings

    result = store.apply_update(current, ui_payload(store, current))  # untouched resave

    assert result.ok
    assert store.secrets.get(worker_secret_id("local-claude", "OPENAI_API_KEY")) == "sk-1"


def test_clear_removes_saved_key(store: SettingsStore) -> None:
    current = store.load()
    payload = ui_payload(store, current)
    set_key(payload, "OPENAI_API_KEY", "sk-1")
    current = store.apply_update(current, payload).settings

    payload = ui_payload(store, current)
    for row in payload["dispatch"]["workers"][0]["env"]:
        if row["key"] == "OPENAI_API_KEY":
            row["action"] = "clear"
    result = store.apply_update(current, payload)

    assert result.ok
    assert store.secrets.get(worker_secret_id("local-claude", "OPENAI_API_KEY")) is None
    assert result.settings["secret_refs"] == []


def test_renaming_a_worker_moves_its_key(store: SettingsStore) -> None:
    current = store.load()
    payload = ui_payload(store, current)
    set_key(payload, "ANTHROPIC_AUTH_TOKEN", "sk-move")
    current = store.apply_update(current, payload).settings

    payload = ui_payload(store, current, name="renamed")  # original_name stays local-claude
    result = store.apply_update(current, payload)

    assert result.ok, result.errors
    assert store.secrets.get(worker_secret_id("renamed", "ANTHROPIC_AUTH_TOKEN")) == "sk-move"
    assert store.secrets.get(worker_secret_id("local-claude", "ANTHROPIC_AUTH_TOKEN")) is None


def test_removing_a_worker_deletes_its_keys(store: SettingsStore) -> None:
    current = store.load()
    payload = ui_payload(store, current)
    payload["dispatch"]["workers"].append({
        "name": "codex-1", "original_name": "codex-1", "type": "codex", "task_types": ["explore"],
        "max_running": 1, "priority": 1, "env": [{"key": "OPENAI_API_KEY", "value": "sk-x", "action": "set"}]})
    current = store.apply_update(current, payload).settings
    assert store.secrets.get(worker_secret_id("codex-1", "OPENAI_API_KEY")) == "sk-x"

    payload = ui_payload(store, current)
    payload["dispatch"]["workers"] = [w for w in payload["dispatch"]["workers"] if w["name"] != "codex-1"]
    assert store.apply_update(current, payload).ok

    assert store.secrets.get(worker_secret_id("codex-1", "OPENAI_API_KEY")) is None


def test_common_env_secret_and_plain_values(store: SettingsStore) -> None:
    current = store.load()
    payload = ui_payload(store, current)
    payload["dispatch"]["common_env"] = [
        {"key": "HTTPS_PROXY", "value": "http://127.0.0.1:7897", "action": "keep"},
        {"key": "PROXY_PASSWORD", "value": "hunter2", "action": "set"},
    ]
    result = store.apply_update(current, payload)

    assert result.ok
    assert "hunter2" not in store.path.read_text(encoding="utf-8")
    built = store.dispatch_dict(result.settings, SERVER)
    assert built["common_env"] == {"HTTPS_PROXY": "http://127.0.0.1:7897", "PROXY_PASSWORD": "hunter2"}
    config = DispatchConfig.from_dict(built)  # DispatchConfig merges common_env into every worker
    assert config.workers[0].env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert config.workers[0].env["PROXY_PASSWORD"] == "hunter2"


# ----------------------------------------------------------------------- validation


def test_invalid_save_writes_nothing(store: SettingsStore) -> None:
    current = store.load()
    store.save(current)
    before = store.path.read_text(encoding="utf-8")
    payload = ui_payload(store, current)
    payload["dispatch"]["runtime"]["execution"] = "container"  # needs API settings per worker
    set_key(payload, "ANTHROPIC_AUTH_TOKEN", "sk-should-not-persist")

    result = store.apply_update(current, payload)

    assert not result.ok
    assert any("missing env keys" in e and "ANTHROPIC_MODEL" in e for e in result.errors), result.errors
    assert store.path.read_text(encoding="utf-8") == before
    assert store.secrets.values == {}


def test_validation_messages_are_readable(store: SettingsStore) -> None:
    current = store.load()
    payload = ui_payload(store, current, task_types=[])
    payload["desktop"]["port"] = 80

    errors = store.apply_update(current, payload).errors

    assert "Server port must be between 1024 and 65535." in errors
    assert errors[-1] == "Worker 'local-claude' → Task types: task_types must not be empty"


def test_cross_field_limits_are_checked(store: SettingsStore) -> None:
    current = store.load()
    payload = ui_payload(store, current)
    payload["dispatch"]["runtime"]["max_project_workers"] = 9  # > max_workers (3)

    errors = store.apply_update(current, payload).errors

    assert any("max_project_workers cannot exceed max_workers" in e for e in errors), errors


def test_non_numeric_values_are_rejected_cleanly(store: SettingsStore) -> None:
    current = store.load()
    payload = ui_payload(store, current)
    payload["dispatch"]["runtime"]["max_workers"] = "lots"

    result = store.apply_update(current, payload)

    assert not result.ok
    assert result.errors == ["Invalid settings: max_workers must be a whole number, got 'lots'"]


def test_duplicate_worker_names_are_rejected(store: SettingsStore) -> None:
    current = store.load()
    payload = ui_payload(store, current)
    payload["dispatch"]["workers"].append(dict(payload["dispatch"]["workers"][0], original_name="x"))

    assert any("worker names must be unique" in e for e in store.apply_update(current, payload).errors)


# ----------------------------------------------------------------------- import


def test_import_dispatch_yaml_moves_keys_into_secret_store(store: SettingsStore, tmp_path: Path) -> None:
    text = (Path(__file__).resolve().parents[2] / "dispatch.example.yaml").read_text(encoding="utf-8")
    text = text.replace('server: "http://cairn-server:8000"', 'server: "http://x:1"')
    current = store.load()

    result = store.import_dispatch_yaml(current, text)

    assert result.ok, result.errors
    settings = result.settings
    assert settings["dispatch"]["runtime"]["execution"] == "container"
    assert settings["dispatch"]["workers"][0]["name"] == "claudecode_deepseek-v4-pro"
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    # Values stay out of the file; only the *names* of stored secrets are recorded.
    assert "sk-xxx" not in json.dumps(on_disk)
    assert "TSEC_AGENT_TOKEN" not in on_disk["dispatch"]["common_env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in on_disk["dispatch"]["workers"][0]["env"]
    assert on_disk["dispatch"]["workers"][0]["env"]["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
    assert store.secrets.get(worker_secret_id("claudecode_deepseek-v4-pro", "ANTHROPIC_AUTH_TOKEN")) == "sk-xxx"
    assert store.secrets.get("common/TSEC_AGENT_TOKEN") == "xxx"
    built = DispatchConfig.from_dict(store.dispatch_dict(settings, SERVER))
    assert built.workers[0].env["ANTHROPIC_AUTH_TOKEN"] == "sk-xxx"
    assert built.server == SERVER  # the app always points the dispatcher at its own server


def test_import_keeps_local_mode_when_the_file_says_so(store: SettingsStore) -> None:
    text = (Path(__file__).resolve().parents[2] / "dispatch.windows.example.yaml").read_text(encoding="utf-8")

    result = store.import_dispatch_yaml(store.load(), text)

    assert result.ok, result.errors
    assert result.settings["dispatch"]["runtime"]["execution"] == "local"
    assert result.settings["dispatch"]["local"]["workspace_root"] == "C:/cairn-runs"


def test_import_rejects_garbage(store: SettingsStore) -> None:
    result = store.import_dispatch_yaml(store.load(), "- just\n- a list\n")
    assert not result.ok and "YAML mapping" in result.errors[0]


# ----------------------------------------------------------------------- stdin config / processes


def _mock_yaml(store: SettingsStore, server: str) -> str:
    settings = default_settings(str(store.default_workspace))
    settings["dispatch"]["runtime"]["prompt_group"] = "mock"
    settings["dispatch"]["workers"] = [{"name": "m", "type": "mock", "task_types": ["bootstrap", "reason", "explore"],
                                        "max_running": 1, "priority": 0, "env": {}}]
    return store.dispatch_yaml(settings, server)


def test_cli_reads_config_from_stdin(store: SettingsStore) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cairn", "dispatch", "--config", "-", "--startup-healthcheck-only"],
        input=_mock_yaml(store, SERVER).encode("utf-8"), capture_output=True, timeout=60)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def test_cli_stdin_config_errors_are_reported(store: SettingsStore) -> None:
    result = subprocess.run([sys.executable, "-m", "cairn", "dispatch", "--config", "-"],
                            input=b"server: x\n", capture_output=True, timeout=60)
    assert result.returncode != 0
    assert b"validation error" in result.stderr


def _unused_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(predicate, timeout=20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_dispatcher_process_start_and_stop(store: SettingsStore, tmp_path: Path) -> None:
    # No server listens on this port: the dispatcher keeps retrying, i.e. stays "running".
    changes: list[str] = []
    proc = DispatcherProcess(tmp_path / "logs" / "dispatcher.log", on_change=lambda p: changes.append(p.state))
    proc.start(_mock_yaml(store, f"http://127.0.0.1:{_unused_port()}"), tmp_path / "work")

    assert proc.is_running()
    assert _wait(lambda: "server request failed" in proc.tail(50)), proc.tail(50)
    proc.stop(grace_seconds=1.0)

    assert not proc.is_running()
    assert proc.state == "stopped"
    assert changes[0] == "running" and changes[-1] == "stopped"


def test_dispatcher_process_reports_failed_start(store: SettingsStore, tmp_path: Path) -> None:
    proc = DispatcherProcess(tmp_path / "logs" / "dispatcher.log")
    proc.start("server: only\n", tmp_path / "work")  # invalid config: exits immediately

    assert _wait(lambda: proc.state == "failed")
    assert proc.early_exit and proc.returncode not in (0, None)
    assert "validation error" in proc.tail(40)


def test_dispatcher_logs_rotate(store: SettingsStore, tmp_path: Path) -> None:
    log = tmp_path / "logs" / "dispatcher.log"
    proc = DispatcherProcess(log)
    for _ in range(3):
        proc.start("server: only\n", tmp_path / "work")
        assert _wait(lambda: proc.state == "failed")
    assert log.exists() and log.with_name("dispatcher.log.1").exists() and log.with_name("dispatcher.log.2").exists()


def test_pick_port_falls_back_when_taken() -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert not port_is_free(taken)
        chosen = pick_port(taken)
        assert chosen != taken and port_is_free(chosen)


def test_settings_file_is_written_atomically_and_is_json(store: SettingsStore) -> None:
    store.save(store.load())
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert not list(store.path.parent.glob(".settings-*"))
    yaml.safe_dump(data)  # plain data, no custom objects
