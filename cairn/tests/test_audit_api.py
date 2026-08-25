from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setattr(db, "DEFAULT_DB", tmp_path / "audit-api.db")
    monkeypatch.setenv("CAIRN_SAFETY_TOKEN", "test-safety-token")
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={"title": "audit", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _event_body(project_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "event_id": "evt-001",
        "action_id": "act-001",
        "run_id": "run-001",
        "project_id": project_id,
        "intent_id": None,
        "worker": "pi-main",
        "phase": "reason",
        "event_type": "ACTION_DECISION",
        "tool_name": "bash",
        "decision": "allow",
        "rule_id": None,
        "reason": "no_high_confidence_red_line",
        "payload": {"command": "pwd"},
    }
    body.update(overrides)
    return body


def _preflight_body(project_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "event_id": "evt-preflight-001",
        "action_id": "act-preflight-001",
        "run_id": "run-preflight-001",
        "project_id": project_id,
        "intent_id": None,
        "worker": "pi-main",
        "phase": "explore",
        "tool_name": "bash",
        "input": {"command": "pwd"},
        "cwd": "/workspace",
    }
    body.update(overrides)
    return body


def test_internal_event_write_requires_token(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post("/internal/safety/events", json=_event_body(project_id))

    assert response.status_code == 401


def test_authenticated_event_round_trips_through_project_query(client: TestClient) -> None:
    project_id = _create_project(client)
    headers = {"X-Cairn-Safety-Token": "test-safety-token"}

    created = client.post(
        "/internal/safety/events",
        json=_event_body(project_id),
        headers=headers,
    )
    listed = client.get(f"/projects/{project_id}/audit")

    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.json() == {"items": [created.json()], "next": None}
    event = created.json()
    assert event["event_id"] == "evt-001"
    assert event["payload"] == {"command": "pwd"}
    assert event["truncated"] is False
    assert len(event["payload_sha256"]) == 64


def test_event_id_replay_is_idempotent_and_collision_is_rejected(client: TestClient) -> None:
    project_id = _create_project(client)
    headers = {"X-Cairn-Safety-Token": "test-safety-token"}
    body = _event_body(project_id, event_id="evt-fixed")

    first = client.post("/internal/safety/events", json=body, headers=headers)
    replay = client.post("/internal/safety/events", json=body, headers=headers)
    collision = client.post(
        "/internal/safety/events",
        json={**body, "payload": {"command": "whoami"}},
        headers=headers,
    )

    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert collision.status_code == 409
    assert len(client.get(f"/projects/{project_id}/audit").json()["items"]) == 1


def test_oversized_payload_keeps_full_hash_and_utf8_safe_preview(
    client: TestClient,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CAIRN_SAFETY_MAX_PAYLOAD_BYTES", "4096")
    project_id = _create_project(client)
    payload = {"text": "证" * 3000}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    response = client.post(
        "/internal/safety/events",
        json=_event_body(project_id, payload=payload),
        headers={"X-Cairn-Safety-Token": "test-safety-token"},
    )

    assert response.status_code == 201
    event = response.json()
    assert event["truncated"] is True
    assert event["payload_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert event["payload"]["original_bytes"] == len(canonical)
    assert event["payload"]["truncated"] is True
    assert isinstance(event["payload"]["preview"], str)


def test_project_audit_supports_filters_and_cursor_pagination(client: TestClient) -> None:
    project_id = _create_project(client)
    headers = {"X-Cairn-Safety-Token": "test-safety-token"}
    events = [
        _event_body(project_id, event_id="evt-001", action_id="act-001", run_id="run-a", decision="allow"),
        _event_body(project_id, event_id="evt-002", action_id="act-002", run_id="run-a", decision="block", rule_id="filesystem_mass_delete"),
        _event_body(project_id, event_id="evt-003", action_id="act-003", run_id="run-b", event_type="ACTION_RESULT", tool_name="read", decision=None),
    ]
    for body in events:
        assert client.post("/internal/safety/events", json=body, headers=headers).status_code == 201

    filtered = client.get(
        f"/projects/{project_id}/audit",
        params={"run_id": "run-a", "event_type": "ACTION_DECISION", "decision": "block", "tool_name": "bash"},
    ).json()
    first_page = client.get(f"/projects/{project_id}/audit", params={"limit": 2}).json()
    second_page = client.get(
        f"/projects/{project_id}/audit",
        params={"limit": 2, "after": first_page["next"]},
    ).json()

    assert [item["event_id"] for item in filtered["items"]] == ["evt-002"]
    assert [item["event_id"] for item in first_page["items"]] == ["evt-001", "evt-002"]
    assert first_page["next"]
    assert [item["event_id"] for item in second_page["items"]] == ["evt-003"]
    assert second_page["next"] is None


def test_preflight_commits_block_before_response_and_replays_idempotently(client: TestClient) -> None:
    project_id = _create_project(client)
    headers = {"X-Cairn-Safety-Token": "test-safety-token"}
    body = _preflight_body(
        project_id,
        input={"command": "rm -rf /var/lib/app"},
    )

    first = client.post("/internal/safety/preflight", json=body, headers=headers)
    with db.get_conn() as conn:
        stored = conn.execute(
            "SELECT decision, rule_id FROM audit_events WHERE event_id = ?",
            (body["event_id"],),
        ).fetchone()
    replay = client.post("/internal/safety/preflight", json=body, headers=headers)

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert first.json()["decision"] == "block"
    assert first.json()["rule_id"] == "filesystem_mass_delete"
    assert stored is not None
    assert dict(stored) == {
        "decision": "block",
        "rule_id": "filesystem_mass_delete",
    }
    assert len(client.get(f"/projects/{project_id}/audit").json()["items"]) == 1


def test_preflight_rejects_event_id_collision(client: TestClient) -> None:
    project_id = _create_project(client)
    headers = {"X-Cairn-Safety-Token": "test-safety-token"}
    body = _preflight_body(project_id)

    first = client.post("/internal/safety/preflight", json=body, headers=headers)
    collision = client.post(
        "/internal/safety/preflight",
        json={**body, "input": {"command": "whoami"}},
        headers=headers,
    )

    assert first.status_code == 201
    assert collision.status_code == 409


def test_preflight_pauses_the_attempt_that_would_exceed_rolling_auth_budget(
    client: TestClient,
) -> None:
    project_id = _create_project(client)
    headers = {"X-Cairn-Safety-Token": "test-safety-token"}

    decisions = []
    for index in range(11):
        response = client.post(
            "/internal/safety/preflight",
            json=_preflight_body(
                project_id,
                event_id=f"evt-auth-{index}",
                action_id=f"act-auth-{index}",
                input={"command": f"hydra -l user{index} -p known ssh://target"},
            ),
            headers=headers,
        )
        assert response.status_code == 201
        decisions.append(response.json())

    assert [item["decision"] for item in decisions[:10]] == ["allow"] * 10
    assert decisions[10]["decision"] == "resource_pause"
    assert decisions[10]["rule_id"] == "auth_rate_limit"
    assert decisions[10]["auth_attempt_count"] == 1


def test_preflight_validates_intent_ownership(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        "/internal/safety/preflight",
        json=_preflight_body(project_id, intent_id="i999"),
        headers={"X-Cairn-Safety-Token": "test-safety-token"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "intent not found in project"


def test_preflight_applies_a_stricter_requested_resource_budget(client: TestClient) -> None:
    project_id = _create_project(client)
    body = _preflight_body(
        project_id,
        input={"command": "cat hosts.txt | xargs -P2 -n1 nmap -sV"},
        resource_budget={
            "max_bulk_concurrency": 1,
            "max_unattended_bulk_seconds": 600,
            "auth_concurrency": 1,
            "auth_attempts_per_minute": 10,
            "auth_attempts_per_batch": 30,
        },
    )

    response = client.post(
        "/internal/safety/preflight",
        json=body,
        headers={"X-Cairn-Safety-Token": "test-safety-token"},
    )

    assert response.status_code == 201
    assert response.json()["decision"] == "resource_pause"
    assert response.json()["rule_id"] == "bulk_concurrency_limit"
