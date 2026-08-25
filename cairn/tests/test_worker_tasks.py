from __future__ import annotations

from collections.abc import Iterator
import json

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.workers.health import HealthResult
from cairn.dispatcher.tasks import bootstrap, common, explore, reason
from cairn.server.models import AuditEvent

from conftest import (
    FakeClient,
    FakeContainerManager,
    FakeDriver,
    FakeLease,
    make_config,
    make_intent,
    make_project,
)


def _lease_factory(lease: FakeLease):
    return lambda *_args, **_kwargs: lease


def _audit_event(**overrides) -> AuditEvent:
    values = {
        "event_id": "event-1",
        "action_id": "action-1",
        "run_id": "run-1",
        "project_id": "proj_001",
        "intent_id": "i001",
        "worker": "test-worker",
        "phase": "explore_execute",
        "event_type": "ACTION_DECISION",
        "tool_name": "bash",
        "decision": "block",
        "rule_id": "destructive_delete",
        "reason": "blocked",
        "payload": {"proposal": {"input": {"command": "rm -rf /srv/data"}}},
        "payload_sha256": "a" * 64,
        "truncated": False,
        "created_at": "2026-08-25T01:02:03Z",
    }
    values.update(overrides)
    return AuditEvent.model_validate(values)


def test_parse_safety_fallbacks_ignores_noise_and_preserves_correlation_ids() -> None:
    first = {
        "schema_version": 1,
        "failed_event": {
            "schema_version": 1,
            "event_id": "decision-1",
            "action_id": "action-1",
            "run_id": "run-1",
            "project_id": "proj_001",
            "intent_id": "i001",
            "worker": "test-worker",
            "phase": "explore_execute",
            "event_type": "ACTION_DECISION",
            "tool_name": "bash",
            "decision": "block",
            "rule_id": "safety_unavailable",
            "reason": "safety preflight was unavailable",
            "payload": {"proposal": {"input": {"command": "rm -rf /srv/data"}}},
        },
        "transport_error": "connect ECONNREFUSED",
    }
    second = {
        "schema_version": 1,
        "failed_event": {
            "schema_version": 1,
            "event_id": "result-1",
            "action_id": "action-2",
            "run_id": "run-1",
            "project_id": "proj_001",
            "intent_id": "i001",
            "worker": "test-worker",
            "phase": "explore_execute",
            "event_type": "ACTION_RESULT",
            "tool_name": "bash",
            "payload": {"is_error": False},
        },
        "transport_error": "safety_http_503",
    }
    stderr = "\n".join(
        [
            "ordinary pi stderr",
            "CAIRN_SAFETY_FALLBACK not-json",
            f"CAIRN_SAFETY_FALLBACK {json.dumps(first)}",
            "CAIRN_SAFETY_FALLBACK []",
            f"CAIRN_SAFETY_FALLBACK {json.dumps(second)}",
        ]
    )

    parsed = common.parse_safety_fallbacks(stderr)

    assert [event["event_id"] for event in parsed] == ["backfill:decision-1", "backfill:result-1"]
    assert [event["action_id"] for event in parsed] == ["action-1", "action-2"]
    assert parsed[0]["event_type"] == "AUDIT_BACKFILL"
    assert parsed[0]["decision"] == "block"
    assert parsed[0]["payload"]["original_event_id"] == "decision-1"
    assert parsed[0]["payload"]["transport_error"] == "connect ECONNREFUSED"
    assert parsed[1]["decision"] is None


def test_latest_blocked_action_prefers_the_newest_committed_or_backfilled_decision() -> None:
    older = _audit_event(created_at="2026-08-25T01:02:03Z")
    newer = _audit_event(
        event_id="backfill:decision-2",
        action_id="action-2",
        event_type="AUDIT_BACKFILL",
        rule_id="safety_unavailable",
        payload={"original_event_id": "decision-2"},
        created_at="2026-08-25T01:02:04Z",
    )

    class Client:
        def list_audit_events(self, _project_id, **kwargs):
            if kwargs["event_type"] == "ACTION_DECISION":
                return [older]
            if kwargs["event_type"] == "AUDIT_BACKFILL":
                return [newer]
            raise AssertionError(kwargs)

    assert common.latest_blocked_action(Client(), "proj_001", "run-1") == newer


def test_explore_backfills_safety_fallback_before_interpreting_output(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()
    posted = []
    fallback = {
        "schema_version": 1,
        "failed_event": {
            "schema_version": 1,
            "event_id": "decision-1",
            "action_id": "action-1",
            "run_id": "run-1",
            "project_id": "proj_001",
            "intent_id": "i001",
            "worker": "test-worker",
            "phase": "explore_execute",
            "event_type": "ACTION_DECISION",
            "tool_name": "bash",
            "decision": "block",
            "rule_id": "safety_unavailable",
            "reason": "safety preflight was unavailable",
            "payload": {"proposal": {"input": {"command": "rm -rf /srv/data"}}},
        },
        "transport_error": "offline",
    }

    def backfill(payload, token):
        posted.append((payload, token))
        return ApiResult(201, {"event_id": payload["event_id"]})

    client.backfill_audit_event = backfill  # type: ignore[attr-defined]
    monkeypatch.setenv("CAIRN_SAFETY_TOKEN", "shared-secret")
    monkeypatch.setattr(explore, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(
        explore,
        "_run_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"description":"safe fact"}}',
            f"CAIRN_SAFETY_FALLBACK {json.dumps(fallback)}",
        ),
    )

    outcome = explore.run_explore_task(
        config,
        client,
        containers,
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert len(posted) == 1
    assert posted[0][0]["event_id"] == "backfill:decision-1"
    assert posted[0][1] == "shared-secret"
    assert client.concluded[-1][-1] == "safe fact"


def test_explore_synthesizes_v1_when_both_pi_phases_fail_after_a_block(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    driver = FakeDriver()
    lease = FakeLease()
    results: Iterator[ProcessResult] = iter(
        [ProcessResult(0, "not-json", ""), ProcessResult(1, "", "conclude failed")]
    )

    monkeypatch.setattr(explore, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(explore, "_run_process", lambda *_a, **_k: next(results))
    monkeypatch.setattr(explore, "latest_blocked_action", lambda *_a, **_k: _audit_event())

    outcome = explore.run_explore_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded[0][3].startswith("[V1][BRANCH_CLOSED]\n")
    assert client.completed == []
    assert client.released == []


def test_bootstrap_block_writes_only_v1_fact_even_if_pi_returns_complete(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    lease = FakeLease()
    v1_fact = "[V1][BRANCH_CLOSED]\nTarget: /srv/data\nAutonomous retry: prohibited for this Intent"

    monkeypatch.setattr(bootstrap, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(bootstrap.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(bootstrap, "latest_blocked_action", lambda *_a, **_k: _audit_event())
    monkeypatch.setattr(
        bootstrap,
        "run_worker_process",
        lambda *_a, **_k: ProcessResult(
            0,
            json.dumps(
                {
                    "accepted": True,
                    "data": {
                        "fact": {"description": v1_fact},
                        "complete": {"description": "goal met"},
                    },
                }
            ),
            "",
        ),
    )

    outcome = bootstrap.run_bootstrap_task(
        config,
        client,
        FakeContainerManager(),
        project,
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", v1_fact)]
    assert client.completed == []


def test_explore_synthesizes_r1_when_resource_paused_output_is_invalid(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    driver = FakeDriver()
    lease = FakeLease()
    resource_event = _audit_event(
        decision="resource_pause",
        rule_id="auth_batch_limit",
        reason="64 attempts exceed batch limit 30",
        payload={
            "proposal": {"input": {"command": "hydra -L users -P passwords ssh://target"}},
            "decision": {"target": "ssh://target"},
            "resource": {"auth_attempt_count": 64},
        },
    )
    results: Iterator[ProcessResult] = iter(
        [ProcessResult(0, "not-json", ""), ProcessResult(1, "", "conclude failed")]
    )

    monkeypatch.setattr(explore, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(explore, "_run_process", lambda *_a, **_k: next(results))
    monkeypatch.setattr(explore, "latest_blocked_action", lambda *_a, **_k: resource_event)

    outcome = explore.run_explore_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded[0][3].startswith("[R1][RESOURCE_PAUSED]\n")
    assert "不是漏洞确认" in client.concluded[0][3]


def test_reason_releases_its_lease_without_creating_a_retry_after_a_block(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    lease = FakeLease()
    event = _audit_event(intent_id=None, phase="reason_execute")

    monkeypatch.setattr(reason, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(reason, "latest_blocked_action", lambda *_a, **_k: event)
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_a, **_k: ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"retry deletion"}]}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "facts:\n- id: f001\n",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_intents == []
    assert client.completed == []
    assert client.released_reasons == [("proj_001", "test-worker")]


def test_reason_writes_graph_snapshot_and_creates_intent(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()
    graph_yaml = "project:\n  title: huge\n" + ("x" * 100_000)

    monkeypatch.setattr(reason, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    safety_contexts = []

    def run_reason_process(*_args, **kwargs):
        safety_contexts.append(kwargs["safety_context"])
        return ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"next step"}]}}',
            "",
        )

    monkeypatch.setattr(reason, "run_worker_process", run_reason_process)

    outcome = reason.run_reason_task(
        config,
        client,
        containers,
        project,
        graph_yaml,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_intents == [("proj_001", ["f001"], "next step", "test-worker")]
    assert client.released_reasons == [("proj_001", "test-worker")]
    assert lease.started and lease.stopped
    assert len(containers.writes) == 1
    container_name, path, content = containers.writes[0]
    assert container_name == "container-proj_001"
    assert path.startswith("/tmp/cairn-prompts/reason_execute-")
    assert path.endswith("/graph.yaml")
    assert content == graph_yaml
    assert graph_yaml not in driver.execute_prompts[0]
    assert path in driver.execute_prompts[0]
    assert len(safety_contexts) == 1
    assert safety_contexts[0].project_id == "proj_001"
    assert safety_contexts[0].intent_id is None
    assert safety_contexts[0].phase == "reason_execute"


def test_explore_early_plain_text_exit_uses_conclude_fallback(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(0, "Need inspect files and keep working.", ""),
            ProcessResult(0, '{"accepted":true,"data":{"description":"confirmed fact"}}', ""),
        ]
    )

    monkeypatch.setattr(explore, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    safety_contexts = []

    def run_process(*_args, **kwargs):
        safety_contexts.append(kwargs["safety_context"])
        return next(results)

    monkeypatch.setattr(explore, "_run_process", run_process)

    outcome = explore.run_explore_task(
        config,
        client,
        containers,
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "confirmed fact")]
    assert len(containers.writes) == 2
    assert "/explore_execute-" in containers.writes[0][1]
    assert "/explore_conclude-" in containers.writes[1][1]
    assert len(driver.execute_prompts) == 1
    assert len(driver.conclude_prompts) == 1
    assert len(safety_contexts) == 2
    assert safety_contexts[0].run_id == safety_contexts[1].run_id
    assert [context.phase for context in safety_contexts] == ["explore_execute", "explore_conclude"]
    assert lease.started and lease.stopped


def test_explore_healthcheck_failure_releases_claim(monkeypatch) -> None:
    config = make_config()
    config.runtime.worker_healthcheck = "startup_and_task"
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    driver = FakeDriver()
    driver.health = HealthResult(ok=False, status=401, detail="unauthorized")
    monkeypatch.setattr(explore, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))

    outcome = explore.run_explore_task(
        config,
        client,
        containers,
        project,
        "graph",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "unhealthy"
    assert client.released == [("proj_001", "i001", "test-worker")]
    assert containers.writes == []


def test_bootstrap_success_concludes_fact_then_completes_project(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(bootstrap, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(bootstrap.HeartbeatLease, "for_intent", _lease_factory(lease))
    safety_contexts = []

    def run_bootstrap_process(*_args, **kwargs):
        safety_contexts.append(kwargs["safety_context"])
        return ProcessResult(
            0,
            '{"accepted":true,"data":{"fact":{"description":"solved"},'
            '"complete":{"description":"goal met"}}}',
            "",
        )

    monkeypatch.setattr(bootstrap, "run_worker_process", run_bootstrap_process)

    outcome = bootstrap.run_bootstrap_task(
        config,
        client,
        containers,
        project,
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "solved")]
    assert client.completed == [("proj_001", ["f002"], "goal met", "test-worker")]
    assert len(safety_contexts) == 1
    assert safety_contexts[0].project_id == "proj_001"
    assert safety_contexts[0].intent_id == "i001"
    assert safety_contexts[0].phase == "bootstrap"
    assert lease.started and lease.stopped


def test_reason_complete_treats_inactive_project_as_success(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    def complete(*_args, **_kwargs) -> ApiResult:
        return ApiResult(403, text="inactive")

    client.complete = complete  # type: ignore[method-assign]
    monkeypatch.setattr(reason, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"complete":{"from":["f001"],"description":"done"}}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        containers,
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.released_reasons == [("proj_001", "test-worker")]


def test_reason_startup_only_mode_skips_task_healthcheck(monkeypatch) -> None:
    config = make_config()
    config.runtime.worker_healthcheck = "startup_only"
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    lease = FakeLease()

    driver = FakeDriver()

    def _boom(*_a, **_k):
        raise AssertionError("task healthcheck should be skipped")

    driver.check_health = _boom  # type: ignore[method-assign]
    monkeypatch.setattr(reason, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"next"}]}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        containers,
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_intents == [("proj_001", ["f001"], "next", "test-worker")]
