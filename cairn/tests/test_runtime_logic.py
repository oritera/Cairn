from __future__ import annotations

from dataclasses import dataclass, field

from cairn.dispatcher.config import ContainerConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease


@dataclass
class FakeProcess:
    cancelled: list[str] = field(default_factory=list)
    kill_count: int = 0

    def cancel(self, reason: str) -> None:
        self.cancelled.append(reason)

    def kill(self) -> None:
        self.kill_count += 1


class FakeContainer:
    def __init__(self) -> None:
        self.client = type("Client", (), {"api": object()})()
        self.stop_count = 0
        self.archives: list[tuple[str, bytes]] = []
        self.archive_result = True

    def stop(self, timeout: int) -> None:
        assert timeout == 1
        self.stop_count += 1

    def put_archive(self, path: str, archive: bytes) -> bool:
        self.archives.append((path, archive))
        return self.archive_result


def _manager(*, completed_action: str = "stop") -> ContainerManager:
    manager = ContainerManager.__new__(ContainerManager)
    manager._config = ContainerConfig(
        image="image",
        network_mode="host",
        completed_action=completed_action,
    )
    return manager


def test_task_cancellation_keeps_first_reason_and_cancels_late_process() -> None:
    cancellation = TaskCancellation()

    assert cancellation.cancel("project stopped")
    assert not cancellation.cancel("second reason")
    assert cancellation.reason == "project stopped"

    process = FakeProcess()
    cancellation.attach_process(process)
    assert process.cancelled == ["project stopped"]


def test_heartbeat_conflict_failure_kills_attached_process() -> None:
    process = FakeProcess()
    lease = HeartbeatLease(lambda: ApiResult(409, text="lost"), "intent", "worker", interval=60)
    lease.attach_process(process)

    lease._fail(409, "lost")

    assert lease.failure is not None
    assert lease.failure.status_code == 409
    assert process.kill_count == 1


def test_container_manager_build_exec_process_wraps_command_with_timeout() -> None:
    manager = _manager()
    container = FakeContainer()
    manager._require_container = lambda _name: container

    process = manager.build_exec_process("container", {"A": "B"}, ["agent", "-p", "prompt"], timeout_seconds=300)

    assert process.command == ["timeout", "-k", "5s", "300s", "agent", "-p", "prompt"]
    assert process.env == {"A": "B"}


def test_container_manager_parses_configured_volume_bindings() -> None:
    manager = _manager()
    manager._config = manager._config.model_copy(
        update={
            "volumes": [
                "/Users/me/.codex:/home/kali/.codex:ro",
                "/Users/me/.claude:/home/kali/.claude",
            ]
        }
    )

    assert manager._volume_bindings() == {
        "/Users/me/.codex": {"bind": "/home/kali/.codex", "mode": "ro"},
        "/Users/me/.claude": {"bind": "/home/kali/.claude", "mode": "rw"},
    }


def test_completed_container_stop_action_only_stops_running_container() -> None:
    manager = _manager()
    container = FakeContainer()
    states = iter(["running", "exited"])
    manager.inspect_state = lambda _name: next(states)
    manager._require_container = lambda _name: container

    assert manager.cleanup_completed("proj/001")
    assert manager.container_name("proj/001") == "cairn-dispatch-proj-001"
    assert container.stop_count == 1


def test_stopped_container_cleanup_is_noop_after_container_has_already_stopped() -> None:
    manager = _manager()
    manager.inspect_state = lambda _name: "exited"

    assert manager.cleanup_stopped("proj_001")


def test_write_text_file_uses_archive_api_and_rejects_false_result() -> None:
    manager = _manager()
    container = FakeContainer()
    manager._require_container = lambda _name: container

    manager.write_text_file("container", "/tmp/graph.yaml", "facts: []\n")
    assert container.archives[0][0] == "/tmp"

    container.archive_result = False
    try:
        manager.write_text_file("container", "/tmp/graph.yaml", "facts: []\n")
    except RuntimeError as exc:
        assert "failed to write container file" in str(exc)
    else:
        raise AssertionError("expected failed put_archive result to raise")
