from __future__ import annotations

from concurrent.futures import Future
import time

from cairn.dispatcher.models import ReasonCheckpoint, RunningTask
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.scheduler.worker_select import choose_worker
from cairn.server.models import Fact, ProjectSummary

from conftest import make_config, make_intent, make_project


def _loop() -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.reason_checkpoints = {}
    loop.runtime_project_ids = set()
    loop.cleanup_futures = {}
    loop._cleanup_pending = set()
    loop._inactive_cleanup_done = {}
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop._log_state = {}
    loop.project_cursor = 0
    return loop


def _summary(project_id: str, status: str) -> ProjectSummary:
    return ProjectSummary(
        id=project_id,
        title=project_id,
        status=status,
        bootstrap_enabled=True,
        created_at="2026-01-01T00:00:00Z",
        fact_count=2,
        intent_count=0,
        working_intent_count=0,
        unclaimed_intent_count=0,
        hint_count=0,
    )


def test_reason_trigger_detects_new_facts_and_open_intent_completion() -> None:
    loop = _loop()
    project = make_project(intents=[make_intent()])
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(
        fact_count=3,
        hint_count=1,
        open_intent_count=1,
    )
    project.facts.append(Fact(id="f002", description="new"))
    project.intents = []

    assert loop._reason_trigger(project) == "facts:3->4,open_intents:1->0"


def test_reason_trigger_returns_none_when_graph_is_unchanged() -> None:
    loop = _loop()
    project = make_project(intents=[make_intent()])
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(
        fact_count=3,
        hint_count=1,
        open_intent_count=1,
    )

    assert loop._reason_trigger(project) is None


def test_refresh_runtime_projects_discards_active_and_changed_cleanup_markers() -> None:
    loop = _loop()
    loop.runtime_project_ids = {"active", "stopped", "deleted"}
    loop._inactive_cleanup_done = {
        "active": "stopped",
        "stopped": "stopped",
        "changed": "completed",
        "deleted": "completed",
    }

    loop._refresh_runtime_projects(
        [
            _summary("active", "active"),
            _summary("stopped", "stopped"),
            _summary("changed", "stopped"),
        ]
    )

    assert loop.runtime_project_ids == {"active"}
    assert loop._inactive_cleanup_done == {"stopped": "stopped"}


def test_reap_cleanup_future_records_only_successful_inactive_cleanup() -> None:
    loop = _loop()
    succeeded: Future[bool] = Future()
    failed: Future[bool] = Future()
    succeeded.set_result(True)
    failed.set_result(False)
    loop.cleanup_futures = {
        succeeded: ("container-success", "proj-success", "completed"),
        failed: ("container-failed", "proj-failed", "stopped"),
    }
    loop._cleanup_pending = {"container-success", "container-failed"}
    loop._inactive_cleanup_done = {"proj-failed": "stopped"}

    loop._reap_cleanup_futures()

    assert loop.cleanup_futures == {}
    assert loop._cleanup_pending == set()
    assert loop._inactive_cleanup_done == {"proj-success": "completed"}


def test_reap_future_records_task_finished_event() -> None:
    loop = _loop()
    events = []

    class Client:
        def record_event(self, **kwargs):
            events.append(kwargs)
            return ApiResult(201, {})

    future: Future[str] = Future()
    future.set_result("success")
    loop.client = Client()
    loop.futures = {
        future: RunningTask(
            "proj",
            "explore",
            "worker",
            TaskCancellation(),
            intent_id="i001",
            started_at=time.time() - 0.1,
        )
    }

    loop._reap_futures()

    assert loop.futures == {}
    assert events[0]["event_type"] == "task_finished"
    assert events[0]["project_id"] == "proj"
    assert events[0]["task_type"] == "explore"
    assert events[0]["intent_id"] == "i001"
    assert events[0]["details"]["outcome"] == "success"
    assert events[0]["details"]["duration_ms"] >= 0


def test_choose_worker_prefers_priority_then_lower_running_count() -> None:
    workers = make_config().workers
    first = workers[0].model_copy(update={"name": "first", "priority": 0})
    busy = workers[0].model_copy(update={"name": "busy", "priority": 0})
    lower_priority = workers[0].model_copy(update={"name": "lower", "priority": 1})

    ordered = choose_worker(
        [lower_priority, busy, first],
        {"busy": 2, "first": 0, "lower": 0},
    )

    assert [worker.name for worker in ordered] == ["first", "busy", "lower"]


def test_new_fact_dispatches_reason_before_unclaimed_explore_intent() -> None:
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    project = make_project(intents=[make_intent()])
    project.intents[0].worker = None
    project.facts.append(Fact(id="f002", description="new"))
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(
        fact_count=3,
        hint_count=1,
        open_intent_count=1,
    )
    loop.container_manager = type("Containers", (), {"container_name": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "graph",
        },
    )()
    dispatched: list[tuple[str, str]] = []
    loop._dispatch_reason = lambda _project, _graph, trigger: dispatched.append(("reason", trigger)) or True
    loop._dispatch_explore = lambda *_args: dispatched.append(("explore", "")) or True

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == [("reason", "facts:3->4")]


def test_initial_enabled_project_without_bootstrap_worker_dispatches_reason() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={
            "workers": [
                config.workers[0].model_copy(update={"task_types": ["reason", "explore"]})
            ]
        }
    )
    loop.futures = {}
    project = make_project()
    project.facts = project.facts[:2]
    loop.container_manager = type("Containers", (), {"container_name": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "graph",
        },
    )()
    dispatched: list[tuple[str, str]] = []
    loop._dispatch_initial_project = lambda _project: dispatched.append(("bootstrap", "")) or True
    loop._dispatch_reason = lambda _project, _graph, trigger: dispatched.append(("reason", trigger)) or True

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == [("reason", "initial")]


def test_initial_disabled_project_skips_configured_bootstrap_worker() -> None:
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    project = make_project()
    project.project.bootstrap_enabled = False
    project.facts = project.facts[:2]
    loop.container_manager = type("Containers", (), {"container_name": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "graph",
        },
    )()
    dispatched: list[tuple[str, str]] = []
    loop._dispatch_initial_project = lambda _project: dispatched.append(("bootstrap", "")) or True
    loop._dispatch_reason = lambda _project, _graph, trigger: dispatched.append(("reason", trigger)) or True

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == [("reason", "initial")]


def test_initial_enabled_project_without_bootstrap_worker_skips_bootstrap() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={
            "workers": [
                config.workers[0].model_copy(update={"task_types": ["reason", "explore"]})
            ]
        }
    )
    project = make_project()
    project.project.bootstrap_enabled = True
    project.facts = project.facts[:2]

    assert not loop._project_requires_bootstrap(project)


def test_initial_enabled_project_keeps_existing_bootstrap_intent_when_workers_change() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={
            "workers": [
                config.workers[0].model_copy(update={"task_types": ["reason", "explore"]})
            ]
        }
    )
    project = make_project(intents=[make_intent()])
    project.project.bootstrap_enabled = True
    project.facts = project.facts[:2]
    project.intents[0].description = "bootstrap"
    project.intents[0].creator = "dispatcher.bootstrap"
    project.intents[0].from_ = ["origin"]

    assert loop._project_requires_bootstrap(project)


def test_cancel_inactive_tasks_marks_stopped_and_deleted_projects() -> None:
    loop = _loop()
    stopped = TaskCancellation()
    deleted = TaskCancellation()
    loop.futures = {
        Future(): RunningTask("stopped", "explore", "worker", stopped),
        Future(): RunningTask("deleted", "reason", "worker", deleted),
    }

    loop._cancel_inactive_tasks([_summary("stopped", "stopped")])

    assert stopped.reason == "stopped"
    assert deleted.reason == "deleted"


def test_initialize_reason_checkpoint_only_for_active_projects_with_open_intents() -> None:
    loop = _loop()
    active = _summary("active", "active")
    active.unclaimed_intent_count = 1

    loop._initialize_reason_checkpoints(
        [
            active,
            _summary("idle", "active"),
            _summary("stopped", "stopped"),
        ]
    )

    assert loop.reason_checkpoints == {
        "active": ReasonCheckpoint(fact_count=2, hint_count=0, open_intent_count=1)
    }


def test_select_worker_reports_busy_unhealthy_rejected_and_unsupported_workers(monkeypatch) -> None:
    loop = _loop()
    base = make_config()
    busy = base.workers[0].model_copy(update={"name": "busy", "task_types": ["reason"]})
    unhealthy = base.workers[0].model_copy(update={"name": "unhealthy", "task_types": ["reason"]})
    rejected = base.workers[0].model_copy(update={"name": "rejected", "task_types": ["reason"]})
    unsupported = base.workers[0].model_copy(update={"name": "unsupported", "task_types": ["explore"]})
    loop.config = base.model_copy(update={"workers": [busy, unhealthy, rejected, unsupported]})
    loop.futures = {Future(): RunningTask("proj", "reason", "busy", TaskCancellation())}
    loop.worker_unhealthy_until = {"unhealthy": 110.0}
    loop.worker_rejected_until = {("proj", "reason", "rejected"): 120.0}
    monkeypatch.setattr("cairn.dispatcher.scheduler.loop.time.time", lambda: 100.0)

    selection = loop._select_worker("proj", "reason")

    assert selection.worker is None
    assert selection.blocked_busy == ["busy(1/1)"]
    assert selection.blocked_unhealthy == ["unhealthy(10.0s)"]
    assert selection.blocked_rejected == ["rejected(20.0s)"]
    assert selection.blocked_task_type == ["unsupported"]


def test_disabled_worker_healthcheck_skips_automatic_startup_but_force_runs_diagnostic() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={"runtime": config.runtime.model_copy(update={"worker_healthcheck": "disabled"})}
    )
    calls: list[bool] = []
    loop._run_startup_healthchecks = lambda *, show_commands: calls.append(show_commands)
    loop._startup_healthchecks_checked = False

    loop.run_startup_healthchecks()

    assert calls == []
    assert loop._startup_healthchecks_checked

    loop._startup_healthchecks_checked = False
    loop.run_startup_healthchecks(show_commands=True, force=True)

    assert calls == [True]


def test_startup_only_worker_healthcheck_runs_automatic_startup_check() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={"runtime": config.runtime.model_copy(update={"worker_healthcheck": "startup_only"})}
    )
    calls: list[bool] = []
    loop._run_startup_healthchecks = lambda *, show_commands: calls.append(show_commands)
    loop._startup_healthchecks_checked = False

    loop.run_startup_healthchecks()

    assert calls == [False]
