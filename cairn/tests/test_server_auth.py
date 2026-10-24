from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from fastapi.testclient import TestClient
import pytest

from cairn import cli
from cairn.server import auth, db
from cairn.server.app import app

TOKEN = "correct-horse-battery-staple"


def _reset(tmp_path: Path, monkeypatch) -> None:
    for name in (auth.TOKEN_ENV, auth.TOKEN_FILE_ENV, auth.NO_AUTH_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    monkeypatch.setattr(auth, "_state", None)


@pytest.fixture
def protected(tmp_path, monkeypatch) -> TestClient:
    _reset(tmp_path, monkeypatch)
    auth.configure(token=TOKEN)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def unprotected(tmp_path, monkeypatch) -> TestClient:
    _reset(tmp_path, monkeypatch)
    auth.configure(disabled=True)
    with TestClient(app) as client:
        yield client


def test_api_rejects_anonymous_requests_but_serves_console_shell(protected: TestClient) -> None:
    for path in (
        "/projects",
        "/projects/proj_001",
        "/projects/proj_001/export?format=yaml",
        "/settings",
        "/docs",
        "/openapi.json",
    ):
        response = protected.get(path)
        assert response.status_code == 401, path
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json() == {"detail": "Authentication required"}
    assert protected.post("/projects", json={"title": "t", "origin": "o", "goal": "g"}).status_code == 401
    assert protected.put("/settings", json={"intent_timeout": 30, "reason_timeout": 30}).status_code == 401
    assert protected.delete("/projects/proj_001").status_code == 401

    # The console shell, its assets and the probes stay reachable: they carry no data.
    assert protected.get("/").status_code == 200
    assert protected.get("/static/favicon.svg").status_code == 200
    assert protected.get("/health").json() == {"status": "ok"}
    assert protected.get("/auth/status").json() == {"auth_required": True, "authenticated": False}


def test_bearer_token_grants_access_and_wrong_token_does_not(protected: TestClient) -> None:
    assert protected.get("/projects", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert protected.get("/projects", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert protected.get("/projects", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    assert protected.get("/projects", headers={"Authorization": TOKEN}).status_code == 401
    status = protected.get("/auth/status", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert status == {"auth_required": True, "authenticated": True}


def test_console_login_sets_session_cookie_and_logout_clears_it(protected: TestClient) -> None:
    assert protected.post("/auth/login", json={"token": "nope"}).status_code == 401
    assert protected.post("/auth/login", json={"token": ""}).status_code == 401
    assert protected.get("/projects").status_code == 401

    response = protected.post("/auth/login", json={"token": TOKEN})
    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{auth.SESSION_COOKIE}=")
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()
    assert "secure" not in cookie.lower()  # plain http in tests
    assert TOKEN not in cookie

    assert protected.get("/projects").status_code == 200
    assert protected.get("/auth/status").json() == {"auth_required": True, "authenticated": True}

    assert protected.post("/auth/logout").status_code == 204
    assert protected.get("/projects").status_code == 401
    assert protected.get("/auth/status").json() == {"auth_required": True, "authenticated": False}


def test_session_cookie_must_be_derived_from_the_token(protected: TestClient) -> None:
    for forged in (TOKEN, "deadbeef", auth.session_value("other-token")):
        protected.cookies.set(auth.SESSION_COOKIE, forged)
        assert protected.get("/projects").status_code == 401, forged
    protected.cookies.set(auth.SESSION_COOKIE, auth.session_value(TOKEN))
    assert protected.get("/projects").status_code == 200


def test_login_marks_cookie_secure_behind_an_https_proxy(protected: TestClient) -> None:
    response = protected.post(
        "/auth/login",
        json={"token": TOKEN},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 204
    assert "secure" in response.headers["set-cookie"].lower()


def test_no_auth_mode_opens_everything_and_says_so(unprotected: TestClient) -> None:
    assert unprotected.get("/projects").status_code == 200
    assert unprotected.get("/auth/status").json() == {"auth_required": False, "authenticated": True}
    assert unprotected.post("/auth/login", json={"token": "anything"}).status_code == 204


def test_token_is_generated_once_and_reused_from_file(tmp_path, monkeypatch, caplog) -> None:
    _reset(tmp_path, monkeypatch)
    token_file = tmp_path / "secrets" / "auth_token"

    with caplog.at_level("WARNING", logger="cairn.server.auth"):
        first = auth.configure(token_file=token_file)
    assert first.source == "generated"
    assert first.token is not None and len(first.token) >= 32
    assert token_file.read_text(encoding="utf-8").strip() == first.token
    assert first.token in caplog.text  # operators learn the token from the startup log

    monkeypatch.setattr(auth, "_state", None)
    second = auth.configure(token_file=token_file)
    assert second == auth.AuthState(token=first.token, source="file")

    with TestClient(app) as client:
        assert client.get("/projects").status_code == 401
        assert client.get("/projects", headers={"Authorization": f"Bearer {first.token}"}).status_code == 200


def test_default_token_file_lives_next_to_the_database(tmp_path, monkeypatch) -> None:
    _reset(tmp_path, monkeypatch)
    state = auth.configure()
    assert state.source == "generated"
    assert (tmp_path / auth.TOKEN_FILE_NAME).read_text(encoding="utf-8").strip() == state.token


def test_environment_variables_configure_token_file_and_opt_out(tmp_path, monkeypatch) -> None:
    _reset(tmp_path, monkeypatch)
    monkeypatch.setenv(auth.TOKEN_ENV, "  from-env  ")
    assert auth.configure() == auth.AuthState(token="from-env", source="env")

    monkeypatch.setattr(auth, "_state", None)
    monkeypatch.setenv(auth.NO_AUTH_ENV, "1")
    assert auth.configure() == auth.AuthState(token=None, source="disabled")

    monkeypatch.setattr(auth, "_state", None)
    monkeypatch.delenv(auth.NO_AUTH_ENV)
    monkeypatch.delenv(auth.TOKEN_ENV)
    custom = tmp_path / "custom_token"
    custom.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv(auth.TOKEN_FILE_ENV, str(custom))
    assert auth.configure() == auth.AuthState(token="from-file", source="file")


def test_explicit_token_beats_environment(tmp_path, monkeypatch) -> None:
    _reset(tmp_path, monkeypatch)
    monkeypatch.setenv(auth.TOKEN_ENV, "from-env")
    assert auth.configure(token="explicit") == auth.AuthState(token="explicit", source="explicit")


def test_serve_cli_wires_auth_options(tmp_path, monkeypatch) -> None:
    _reset(tmp_path, monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))

    result = CliRunner().invoke(
        cli.main,
        ["serve", "--db-path", str(tmp_path / "cairn.db"), "--auth-token", "cli-token", "--port", "9"],
    )
    assert result.exit_code == 0, result.output
    assert captured["port"] == 9
    assert auth.state() == auth.AuthState(token="cli-token", source="explicit")

    result = CliRunner().invoke(cli.main, ["serve", "--no-auth", "--auth-token", "x"])
    assert result.exit_code != 0
    assert "--no-auth cannot be combined with --auth-token" in result.output
