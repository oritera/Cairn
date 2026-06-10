from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={
            "title": "test",
            "origin": "starting point",
            "goal": "finish",
            "hints": [{"content": "initial clue", "creator": "human"}],
        },
    )
    assert response.status_code == 201
    assert response.json()["project"]["bootstrap_enabled"] is True
    return response.json()["project"]["id"]


def test_project_workflow_create_conclude_complete_and_reopen(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    assert response.status_code == 201
    assert response.json()["id"] == "i001"

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "explorer"

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "explorer", "description": "new fact"},
    )
    assert response.status_code == 200
    assert response.json()["fact"] == {"id": "f001", "description": "new fact"}

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "description": "solved", "worker": "reasoner"},
    )
    assert response.status_code == 200
    assert response.json()["to"] == "goal"

    response = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "human correction", "creator": "human"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "active"
    assert payload["fact"] == {"id": "f002", "description": "human correction"}
    assert payload["intent"]["from"] == ["f001"]
    assert payload["intent"]["to"] == "f002"


def test_stopping_project_releases_claims_and_reason_but_keeps_hints_writable(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    response = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert response.status_code == 200
    assert response.json()["reason"] is None

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] is None
    assert client.post(
        f"/projects/{project_id}/hints",
        json={"content": "manual note", "creator": "human"},
    ).status_code == 201
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "blocked", "creator": "reasoner", "worker": None},
    ).status_code == 403


def test_intent_creation_rejects_goal_source_and_mismatched_initial_worker(client: TestClient) -> None:
    project_id = _create_project(client)

    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["goal"], "description": "invalid", "creator": "reasoner", "worker": None},
    ).status_code == 400
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "invalid", "creator": "reasoner", "worker": "explorer"},
    ).status_code == 400


def test_settings_and_export_are_backed_by_the_same_database(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.put("/settings", json={"intent_timeout": 30, "reason_timeout": 45})
    assert response.status_code == 200
    assert client.get("/settings").json() == {"intent_timeout": 30, "reason_timeout": 45}

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    assert "origin: starting point" in exported.text
    assert "goal: finish" in exported.text
    assert client.get(f"/projects/{project_id}/export?format=invalid").status_code == 400


def test_expired_intent_and_reason_leases_can_be_reclaimed(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE project_id = ?",
            (project_id,),
        )
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (project_id,),
        )

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "worker-b"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "worker-b"

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )
    assert response.status_code == 200
    assert response.json()["reason"]["worker"] == "worker-b"


def test_live_reason_lease_rejects_competing_worker(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    ).status_code == 200

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    assert response.status_code == 409
    assert "worker-a" in response.json()["detail"]


def test_project_creation_persists_disabled_bootstrap_and_exports_it(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "no bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": False,
        },
    )

    assert response.status_code == 201
    project_id = response.json()["project"]["id"]
    assert client.get(f"/projects/{project_id}").json()["project"]["bootstrap_enabled"] is False
    assert "bootstrap_enabled: false" in client.get(f"/projects/{project_id}/export?format=yaml").text


def test_project_creation_rejects_invalid_bootstrap_enabled(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "invalid bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": "sometimes",
        },
    )

    assert response.status_code == 422


def test_ops_summary_and_project_diagnostics_report_runtime_state(client: TestClient) -> None:
    running_project = _create_project(client)
    waiting_project = _create_project(client)
    completed_project = _create_project(client)
    stopped_project = _create_project(client)

    assert client.post(
        f"/projects/{running_project}/intents",
        json={"from": ["origin"], "description": "running work", "creator": "worker-a", "worker": "worker-a"},
    ).status_code == 201
    assert client.post(
        f"/projects/{waiting_project}/intents",
        json={"from": ["origin"], "description": "waiting work", "creator": "reasoner", "worker": None},
    ).status_code == 201

    assert client.post(
        f"/projects/{completed_project}/intents",
        json={"from": ["origin"], "description": "finish work", "creator": "worker-b", "worker": "worker-b"},
    ).status_code == 201
    assert client.post(
        f"/projects/{completed_project}/intents/i001/conclude",
        json={"worker": "worker-b", "description": "proof"},
    ).status_code == 200
    assert client.post(
        f"/projects/{completed_project}/complete",
        json={"from": ["f001"], "description": "done", "worker": "reasoner"},
    ).status_code == 200

    assert client.put(f"/projects/{stopped_project}/status", json={"status": "stopped"}).status_code == 200

    summary = client.get("/ops/summary")
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["project_count"] == 4
    assert payload["active_count"] == 2
    assert payload["stopped_count"] == 1
    assert payload["completed_count"] == 1
    assert payload["running_count"] == 1
    assert payload["attention_count"] == 1
    assert payload["working_intent_count"] == 1
    assert payload["unclaimed_intent_count"] == 1

    projects = {item["project_id"]: item for item in payload["projects"]}
    assert projects[running_project]["severity"] == "running"
    assert projects[waiting_project]["severity"] == "attention"
    assert projects[completed_project]["severity"] == "completed"
    assert projects[stopped_project]["severity"] == "stopped"

    diagnostics = client.get(f"/projects/{waiting_project}/diagnostics")
    assert diagnostics.status_code == 200
    detail = diagnostics.json()
    assert detail["severity"] == "attention"
    assert detail["open_intent_count"] == 1
    assert detail["unclaimed_intent_count"] == 1
    assert "worker" in detail["next_action"].lower()


def test_project_diagnostics_reports_reason_lease(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "reasoner", "trigger": "initial"},
    ).status_code == 200

    response = client.get(f"/projects/{project_id}/diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["severity"] == "running"
    assert payload["reason"]["worker"] == "reasoner"
    assert "reason" in payload["message"].lower()


def test_ops_events_can_be_created_and_filtered(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        "/ops/events",
        json={
            "project_id": project_id,
            "event_type": "task_dispatched",
            "task_type": "reason",
            "worker": "worker-a",
            "severity": "info",
            "message": "dispatched reason",
            "details": {"trigger": "initial"},
        },
    )

    assert response.status_code == 201
    event = response.json()
    assert event["id"] == 1
    assert event["project_id"] == project_id
    assert event["details"] == {"trigger": "initial"}

    assert client.post(
        "/ops/events",
        json={
            "event_type": "worker_unavailable",
            "severity": "warning",
            "message": "no worker available",
        },
    ).status_code == 201

    project_events = client.get(f"/ops/events?project_id={project_id}").json()
    assert [item["event_type"] for item in project_events] == ["task_dispatched"]

    warning_events = client.get("/ops/events?severity=warning").json()
    assert [item["event_type"] for item in warning_events] == ["worker_unavailable"]

    typed_events = client.get("/ops/events?event_type=task_dispatched").json()
    assert len(typed_events) == 1
    assert typed_events[0]["task_type"] == "reason"


def test_hint_metadata_is_persisted_and_exported(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/hints",
        json={
            "content": "focus on uploaded evidence",
            "creator": "human",
            "hint_type": "strategy",
            "priority": "high",
            "target_type": "fact",
            "target_id": "origin",
            "pinned": True,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["hint_type"] == "strategy"
    assert payload["priority"] == "high"
    assert payload["target_type"] == "fact"
    assert payload["target_id"] == "origin"
    assert payload["pinned"] is True

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["hints"][0]["pinned"] is True
    assert detail["hints"][0]["id"] == payload["id"]

    exported = client.get(f"/projects/{project_id}/export?format=yaml").text
    assert "hint_type: strategy" in exported
    assert "priority: high" in exported
    assert "target_type: fact" in exported
    assert "pinned: true" in exported
