from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
import yaml

from cairn.server import db
from cairn.server import dispatcher_config as dispatcher_config_service
from cairn.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CAIRN_AUTH_DISABLED", "1")
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


def test_dispatcher_config_update_can_add_and_enable_single_api_profile(
    client: TestClient,
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "dispatch.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "server": "http://127.0.0.1:8000",
                "runtime": {
                    "interval": 60,
                    "max_workers": 2,
                    "max_running_projects": 1,
                    "max_project_workers": 2,
                    "healthcheck_timeout": 5,
                    "prompt_group": "zh",
                },
                "tasks": {
                    "bootstrap": {"timeout": 10, "conclude_timeout": 5},
                    "reason": {"timeout": 10, "max_intents": 2},
                    "explore": {"timeout": 10, "conclude_timeout": 5},
                },
                "container": {
                    "image": "test-image",
                    "network_mode": "host",
                    "completed_action": "stop",
                },
                "workers": [
                    {
                        "name": "codex_old",
                        "type": "codex",
                        "task_types": ["bootstrap", "reason", "explore"],
                        "max_running": 1,
                        "priority": 0,
                        "env": {
                            "CODEX_MODEL": "old",
                            "CODEX_BASE_URL": "http://old.example/v1",
                            "OPENAI_API_KEY": "old-key",
                        },
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CAIRN_DISPATCH_CONFIG", str(config_path))

    response = client.put(
        "/dispatcher-config",
        json={
            "workers": [
                {"name": "codex_old", "type": "codex", "enabled": False},
                {
                    "name": "codex_new",
                    "type": "codex",
                    "enabled": True,
                    "model": "gpt-test",
                    "base_url": "http://api.example/v1",
                    "api_key": "sk-test",
                    "api_mode": "responses",
                },
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert [worker["name"] for worker in payload["workers"]] == ["codex_old", "codex_new"]
    assert payload["workers"][0]["enabled"] is False
    assert payload["workers"][1]["enabled"] is True

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["runtime"]["prompt_group"] == "zh"
    assert saved["workers"][0]["enabled"] is False
    assert saved["workers"][1]["env"]["CODEX_MODEL"] == "gpt-test"
    assert "iq_openai" not in payload["workers"][1]["api_mode_options"]


def test_qizhi_connection_test_uses_documented_bearer_token_and_defaults(client: TestClient, monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status_code = 200
        text = '{"ok":true}'

    def fake_post(url, *, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(dispatcher_config_service.requests, "post", fake_post)

    response = client.post(
        "/dispatcher-config/test",
        json={
            "name": "qizhi",
            "type": "codex",
            "enabled": True,
            "api_key": "test-service|123456|test-token",
            "api_mode": "qizhi_openai",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert captured["url"] == "http://ibrain.qiyi.domain/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-service|123456|test-token"
    assert captured["json"]["model"] == "test-service"


def test_dispatcher_config_test_rejects_incomplete_profile(client: TestClient) -> None:
    response = client.post(
        "/dispatcher-config/test",
        json={"name": "bad", "type": "codex", "enabled": True},
    )

    assert response.status_code == 400
    assert "missing env keys" in response.json()["detail"]


def test_auth_requires_login_when_password_is_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CAIRN_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("CAIRN_AUTH_USERNAME", "tester")
    monkeypatch.setenv("CAIRN_AUTH_PASSWORD", "secret-password")
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "auth.db")

    with TestClient(app) as auth_client:
        assert auth_client.get("/healthz").status_code == 200
        assert auth_client.get("/projects").status_code == 401

        bad_login = auth_client.post("/auth/login", json={"username": "tester", "password": "wrong"})
        assert bad_login.status_code == 401

        good_login = auth_client.post("/auth/login", json={"username": "tester", "password": "secret-password"})
        assert good_login.status_code == 200
        assert auth_client.get("/projects").status_code == 200
