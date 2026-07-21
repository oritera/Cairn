from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from cairn.dispatcher.config import DispatchConfig, LocalConfig, WorkerConfig
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.local_process import LocalProcess
from cairn.dispatcher.scheduler import loop as loop_module
from cairn.dispatcher.tasks import common, explore
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.workers.adapters.codex import CodexDriver
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.workers.registry import get_driver

from conftest import FakeClient, make_config, make_intent, make_project


REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- LocalProcess


def test_local_process_captures_stdout_and_exit_code() -> None:
    process = LocalProcess(
        ["python3", "-c", "import sys; print('hello'); sys.exit(3)"],
        cwd=os.getcwd(),
        env=dict(os.environ),
        timeout_seconds=10,
    )
    process.start()
    result = process.communicate(timeout=20)

    assert result.stdout.strip() == "hello"
    assert result.returncode == 3
    assert not result.timed_out


def test_local_process_inherits_cwd(tmp_path: Path) -> None:
    process = LocalProcess(
        ["python3", "-c", "import os; print(os.getcwd())"],
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_seconds=10,
    )
    process.start()
    result = process.communicate(timeout=20)

    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_local_process_times_out_and_kills_within_grace() -> None:
    process = LocalProcess(
        ["sh", "-c", "sleep 30"],
        cwd=os.getcwd(),
        env=dict(os.environ),
        timeout_seconds=1,
        term_grace_seconds=2,
    )
    process.start()
    started = time.monotonic()
    result = process.communicate(timeout=30)
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert elapsed < 10  # killed on its own timeout, not the 30s outer backstop


def test_local_process_kill_terminates_child_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = f"sleep 30 & echo $! > {pid_file}; wait"
    process = LocalProcess(
        ["sh", "-c", script],
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_seconds=1,
        term_grace_seconds=2,
    )
    process.start()
    result = process.communicate(timeout=30)

    assert result.timed_out
    child_pid = int(pid_file.read_text().strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"child process {child_pid} survived the group kill")


def test_local_process_cancel_records_reason() -> None:
    process = LocalProcess(
        ["sh", "-c", "sleep 30"],
        cwd=os.getcwd(),
        env=dict(os.environ),
        timeout_seconds=30,
        term_grace_seconds=2,
    )
    process.start()
    process.cancel("project stopped")
    result = process.communicate(timeout=30)

    assert result.cancelled
    assert result.cancel_reason == "project stopped"


# --------------------------------------------------------------------------- LocalBackend


def test_local_backend_creates_isolated_project_dir(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    handle = backend.ensure_running("proj_001")

    assert Path(handle) == tmp_path / "proj_001"
    assert Path(handle).is_dir()
    assert backend.container_name("proj_001") == str(tmp_path / "proj_001")


def test_local_backend_merges_host_env_with_worker_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_HOST_VAR", "host")
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    handle = backend.ensure_running("proj_001")

    process = backend.build_exec_process(
        handle,
        {"CAIRN_WORKER_VAR": "worker"},
        ["sh", "-c", 'printf "%s-%s" "$CAIRN_HOST_VAR" "$CAIRN_WORKER_VAR"'],
        timeout_seconds=10,
    )
    process.start()
    result = process.communicate(timeout=20)

    assert result.stdout == "host-worker"


def test_local_common_env_reaches_worker_subprocess(tmp_path: Path, monkeypatch) -> None:
    # common_env (e.g. an outbound proxy) merges into every worker's env and must survive
    # all the way to the host subprocess in local mode.
    payload = _local_payload()
    payload["local"] = {"workspace_root": str(tmp_path)}
    payload["common_env"] = {
        "https_proxy": "http://127.0.0.1:7897",
        "http_proxy": "http://127.0.0.1:7897",
        "all_proxy": "http://127.0.0.1:7897",
    }
    config = DispatchConfig.model_validate(payload)
    worker = next(w for w in config.workers if w.type == "claudecode")
    assert worker.env["https_proxy"] == "http://127.0.0.1:7897"

    assert config.local is not None
    backend = LocalBackend(config.local)
    handle = backend.ensure_running("proj_001")
    process = backend.build_exec_process(
        handle,
        dict(worker.env),
        ["sh", "-c", 'printf "%s|%s|%s" "$https_proxy" "$http_proxy" "$all_proxy"'],
        timeout_seconds=10,
    )
    process.start()
    result = process.communicate(timeout=20)

    proxy = "http://127.0.0.1:7897"
    assert result.stdout == f"{proxy}|{proxy}|{proxy}"


def test_local_backend_write_text_file_writes_to_host(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    target = tmp_path / "snapshots" / "graph.yaml"

    backend.write_text_file("ignored", str(target), "facts: []\n")

    assert target.read_text() == "facts: []\n"


def test_local_backend_keep_leaves_dir_and_reports_no_cleanup(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path), completed_action="keep"))
    handle = backend.ensure_running("proj_001")

    assert backend.needs_completed_cleanup("proj_001") is False
    assert backend.cleanup_completed("proj_001") is True
    assert Path(handle).is_dir()


def test_local_backend_remove_deletes_dir_on_completion(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path), completed_action="remove"))
    handle = backend.ensure_running("proj_001")

    assert backend.needs_completed_cleanup("proj_001") is True
    assert backend.cleanup_completed("proj_001") is True
    assert not Path(handle).exists()


def test_local_backend_stopped_cleanup_is_noop(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    backend.ensure_running("proj_001")

    assert backend.needs_stopped_cleanup("proj_001") is False
    assert backend.cleanup_stopped("proj_001") is True


# --------------------------------------------------------------------------- config


def _local_payload() -> dict:
    return {
        "server": "http://127.0.0.1:8000",
        "runtime": {
            "execution": "local",
            "worker_healthcheck": "disabled",
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
        "workers": [
            {"name": "local-claude", "type": "claudecode", "task_types": ["explore"], "max_running": 1, "priority": 0},
            {"name": "local-codex", "type": "codex", "task_types": ["explore"], "max_running": 1, "priority": 1},
            {"name": "local-pi", "type": "pi", "task_types": ["reason"], "max_running": 1, "priority": 2},
        ],
    }


def test_local_execution_needs_no_container_or_worker_env() -> None:
    config = DispatchConfig.model_validate(_local_payload())

    assert config.container is None
    assert config.local is not None
    assert config.local.completed_action == "keep"
    assert all(worker.env == {} for worker in config.workers)


def test_local_workspace_root_is_optional_and_defaults_null() -> None:
    payload = _local_payload()
    payload["local"] = {"completed_action": "remove"}
    config = DispatchConfig.model_validate(payload)

    assert config.local is not None
    assert config.local.workspace_root is None
    assert config.local.completed_action == "remove"


def test_container_execution_requires_container_block() -> None:
    payload = make_config().model_dump()
    payload["container"] = None

    with pytest.raises(ValidationError, match="container config is required"):
        DispatchConfig.model_validate(payload)


def test_container_execution_still_requires_worker_env() -> None:
    payload = _local_payload()
    payload["runtime"]["execution"] = "container"
    payload["runtime"]["worker_healthcheck"] = "startup_only"
    payload["container"] = {"image": "img", "network_mode": "host", "completed_action": "stop"}

    with pytest.raises(ValidationError, match="missing env keys"):
        DispatchConfig.model_validate(payload)


def test_shipped_local_example_config_is_valid() -> None:
    config = DispatchConfig.load(REPO_ROOT / "dispatch.local.example.yaml")

    assert config.runtime.execution == "local"
    assert config.container is None
    assert config.local is not None
    assert config.local.completed_action == "keep"


# --------------------------------------------------------------------------- startup CLI check


def _bare_loop(config: DispatchConfig) -> loop_module.DispatcherLoop:
    loop = loop_module.DispatcherLoop.__new__(loop_module.DispatcherLoop)
    loop.config = config
    return loop


def test_local_cli_check_passes_when_cli_present() -> None:
    payload = _local_payload()
    payload["workers"] = [{"name": "m", "type": "mock", "task_types": ["reason"], "max_running": 1, "priority": 0}]
    config = DispatchConfig.model_validate(payload)

    _bare_loop(config)._run_local_binary_check()  # mock -> python3 --help runs; must not raise


def test_local_cli_check_exits_when_no_cli_installed(monkeypatch) -> None:
    monkeypatch.setattr(loop_module.shutil, "which", lambda _binary: None)
    config = DispatchConfig.model_validate(_local_payload())

    with pytest.raises(RuntimeError, match="none of the configured worker CLIs"):
        _bare_loop(config)._run_local_binary_check()


# --------------------------------------------------------------------------- drivers


def _bare_worker(worker_type: str) -> WorkerConfig:
    return WorkerConfig.model_validate(
        {"name": worker_type, "type": worker_type, "task_types": ["explore"], "max_running": 1, "priority": 0}
    )


def test_codex_local_driver_omits_provider_injection() -> None:
    worker = _bare_worker("codex")
    argv = CodexDriver(local=True).build_execute(worker, "PROMPT", None).argv

    assert argv == [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--",
        "PROMPT",
    ]
    assert not any("model_providers" in part for part in argv)
    assert "--model" not in argv

    conclude = CodexDriver(local=True).build_conclude(worker, "PROMPT", "sess-1")
    assert conclude[:4] == ["codex", "exec", "resume", "sess-1"]
    assert "--skip-git-repo-check" in conclude
    assert conclude[-2:] == ["--", "PROMPT"]
    assert not any("model_providers" in part for part in conclude)


def test_pi_local_driver_omits_models_json_and_provider() -> None:
    worker = _bare_worker("pi")
    argv = PiDriver(local=True).build_execute(worker, "PROMPT", None).argv

    assert argv[0] == "/bin/sh"
    assert "exec pi" in argv[2]
    assert "--provider" not in argv
    assert "--model" not in argv
    assert argv[-2:] == ["-p", "PROMPT"]


def test_get_driver_selects_local_or_container_variant() -> None:
    assert get_driver("codex", "local").local is True
    assert get_driver("codex").local is False
    assert get_driver("pi", "local").local is True
    # claudecode and mock are shared instances across both modes
    assert get_driver("claudecode", "local") is get_driver("claudecode")
    assert get_driver("mock", "local") is get_driver("mock")


# --------------------------------------------------------------------------- end to end


def _local_config_for_worker(name: str, worker_type: str) -> DispatchConfig:
    return DispatchConfig.model_validate(
        {
            "server": "in-process",
            "runtime": {
                "execution": "local",
                "worker_healthcheck": "disabled",
                "interval": 60,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 5,
                "prompt_group": "default",
            },
            "tasks": {
                "bootstrap": {"timeout": 30, "conclude_timeout": 10},
                "reason": {"timeout": 30, "max_intents": 3},
                "explore": {"timeout": 30, "conclude_timeout": 10},
            },
            "workers": [
                {"name": name, "type": worker_type, "task_types": ["explore"], "max_running": 1, "priority": 0}
            ],
        }
    )


def _install_fake_cli(tmp_path: Path, monkeypatch, name: str, body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_explore_runs_real_local_cli_end_to_end(tmp_path: Path, monkeypatch) -> None:
    # A fake `claude` on PATH stands in for the real CLI: the whole local path is exercised
    # for real — driver argv -> LocalBackend -> LocalProcess subprocess -> stdout parsing.
    _install_fake_cli(
        tmp_path,
        monkeypatch,
        "claude",
        "echo '{\"accepted\":true,\"data\":{\"description\":\"local fake fact\"}}'",
    )
    monkeypatch.setattr(common, "GRAPH_SNAPSHOT_ROOT", str(tmp_path / "prompts"))

    config = _local_config_for_worker("test-worker", "claudecode")
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)

    outcome = explore.run_explore_task(
        config,
        client,
        backend,
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "local fake fact")]
    # graph snapshot was materialised on the host under the patched root
    snapshot_root = tmp_path / "prompts"
    assert any(p.name == "graph.yaml" for p in snapshot_root.rglob("*"))


def test_explore_local_cli_rejection_releases_intent(tmp_path: Path, monkeypatch) -> None:
    _install_fake_cli(
        tmp_path,
        monkeypatch,
        "claude",
        "echo '{\"accepted\":false,\"reason\":\"policy_refusal\"}'",
    )
    monkeypatch.setattr(common, "GRAPH_SNAPSHOT_ROOT", str(tmp_path / "prompts"))

    config = _local_config_for_worker("test-worker", "claudecode")
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)

    outcome = explore.run_explore_task(
        config,
        client,
        backend,
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "rejected"
    assert client.concluded == []
    assert client.released == [("proj_001", "i001", "test-worker")]
