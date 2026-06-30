from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.app import app
from cairn.server.auth import configure_api_keys

_TEST_API_KEY = "test-auth-key"


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    configure_api_keys([_TEST_API_KEY])
    with TestClient(app) as c:
        yield c


@pytest.fixture
def authed_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    configure_api_keys([_TEST_API_KEY])
    with TestClient(app, headers={"X-API-Key": _TEST_API_KEY}) as c:
        yield c


# --- registration ---


def test_register_returns_token(client: TestClient) -> None:
    r = client.post("/auth/register", json={"username": "alice", "password": "secret123"})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "alice"
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 0


def test_register_duplicate_username_returns_409(client: TestClient) -> None:
    client.post("/auth/register", json={"username": "bob", "password": "secret123"})
    r = client.post("/auth/register", json={"username": "bob", "password": "other456"})
    assert r.status_code == 409


def test_register_short_username_returns_422(client: TestClient) -> None:
    r = client.post("/auth/register", json={"username": "ab", "password": "secret123"})
    assert r.status_code == 422


def test_register_short_password_returns_422(client: TestClient) -> None:
    r = client.post("/auth/register", json={"username": "charlie", "password": "short"})
    assert r.status_code == 422


# --- login ---


def test_login_valid_credentials(client: TestClient) -> None:
    client.post("/auth/register", json={"username": "dave", "password": "mypassword"})
    r = client.post("/auth/login", json={"username": "dave", "password": "mypassword"})
    assert r.status_code == 200
    assert r.json()["username"] == "dave"
    assert len(r.json()["access_token"]) > 0


def test_login_wrong_password_returns_401(client: TestClient) -> None:
    client.post("/auth/register", json={"username": "eve", "password": "correct123"})
    r = client.post("/auth/login", json={"username": "eve", "password": "wrong456"})
    assert r.status_code == 401


def test_login_nonexistent_user_returns_401(client: TestClient) -> None:
    r = client.post("/auth/login", json={"username": "nobody", "password": "whatever"})
    assert r.status_code == 401


# --- /auth/me ---


def test_me_with_valid_jwt(client: TestClient) -> None:
    r = client.post("/auth/register", json={"username": "frank", "password": "secret123"})
    token = r.json()["access_token"]
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["username"] == "frank"
    assert "id" in r.json()
    assert "created_at" in r.json()


def test_me_without_token_returns_401(client: TestClient) -> None:
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_me_with_invalid_token_returns_401(client: TestClient) -> None:
    r = client.get("/auth/me", headers={"Authorization": "Bearer invalid.jwt.token"})
    assert r.status_code == 401


# --- API Key auth on protected routes ---


def test_api_key_grants_access_to_projects(authed_client: TestClient) -> None:
    r = authed_client.get("/projects")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_wrong_api_key_returns_401(client: TestClient) -> None:
    r = client.get("/projects", headers={"X-API-Key": "wrong-key"})
    assert r.status_code == 401


def test_no_auth_returns_401_on_projects(client: TestClient) -> None:
    r = client.get("/projects")
    assert r.status_code == 401


def test_no_auth_returns_401_on_settings(client: TestClient) -> None:
    r = client.get("/settings")
    assert r.status_code == 401


def test_no_auth_returns_401_on_hints(client: TestClient) -> None:
    r = client.post("/projects/fake/hints", json={"content": "x", "creator": "y"})
    assert r.status_code == 401


# --- JWT auth on protected routes ---


def test_jwt_grants_access_to_projects(client: TestClient) -> None:
    r = client.post("/auth/register", json={"username": "grace", "password": "secret123"})
    token = r.json()["access_token"]
    r = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_jwt_grants_access_to_settings(client: TestClient) -> None:
    r = client.post("/auth/register", json={"username": "heidi", "password": "secret123"})
    token = r.json()["access_token"]
    r = client.get("/settings", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "intent_timeout" in r.json()


# --- auth-free endpoints ---


def test_register_does_not_require_auth(client: TestClient) -> None:
    r = client.post("/auth/register", json={"username": "ivan", "password": "secret123"})
    assert r.status_code == 200


def test_login_does_not_require_auth(client: TestClient) -> None:
    client.post("/auth/register", json={"username": "judy", "password": "secret123"})
    r = client.post("/auth/login", json={"username": "judy", "password": "secret123"})
    assert r.status_code == 200


def test_openapi_does_not_require_auth(client: TestClient) -> None:
    r = client.get("/openapi.json")
    assert r.status_code == 200


# --- full flow: register -> login -> use API ---


def test_full_user_flow(client: TestClient) -> None:
    r = client.post("/auth/register", json={"username": "kate", "password": "secret123"})
    assert r.status_code == 200

    r = client.post("/auth/login", json={"username": "kate", "password": "secret123"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = client.get("/auth/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["username"] == "kate"

    r = client.get("/projects", headers=headers)
    assert r.status_code == 200

    r = client.post(
        "/projects",
        json={"title": "test", "origin": "start", "goal": "finish"},
        headers=headers,
    )
    assert r.status_code == 201
    project_id = r.json()["project"]["id"]

    r = client.get(f"/projects/{project_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["project"]["title"] == "test"

    r = client.get(f"/projects/{project_id}/export?format=yaml", headers=headers)
    assert r.status_code == 200
    assert "origin: start" in r.text
