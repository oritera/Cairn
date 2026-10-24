"""Shared-secret authentication for the Cairn server.

Every request needs credentials unless its path is explicitly public (the console shell,
its static assets, ``/health`` and the ``/auth/*`` endpoints). Two credential forms are
accepted:

* ``Authorization: Bearer <token>`` -- used by the dispatcher and any other API client.
* The ``cairn_session`` cookie set by ``POST /auth/login`` -- used by the browser console.
  Its value is an HMAC derived from the token, so a leaked cookie never reveals the token
  itself and rotating the token invalidates every session at once.

The token is resolved once at startup (:func:`configure`): an explicit value, else the
``CAIRN_AUTH_TOKEN`` environment variable, else the token file next to the database, which
is generated on first start. Authentication is only ever off when asked for explicitly
with ``--no-auth`` / ``CAIRN_NO_AUTH=1``; it never fails open.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from cairn.server import db

LOG = logging.getLogger(__name__)

TOKEN_ENV = "CAIRN_AUTH_TOKEN"
TOKEN_FILE_ENV = "CAIRN_AUTH_TOKEN_FILE"
NO_AUTH_ENV = "CAIRN_NO_AUTH"
TOKEN_FILE_NAME = "auth_token"
GENERATED_TOKEN_BYTES = 32

SESSION_COOKIE = "cairn_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_SESSION_PURPOSE = b"cairn-session-v1"

# Reachable without credentials. The console shell carries no data of its own: every API
# call it makes is checked separately.
PUBLIC_PATHS = frozenset({"/", "/favicon.ico", "/health", "/auth/login", "/auth/logout", "/auth/status"})
PUBLIC_PREFIXES = ("/static/",)


@dataclass(frozen=True, slots=True)
class AuthState:
    token: str | None
    source: str  # "explicit" | "env" | "file" | "generated" | "disabled"

    @property
    def enabled(self) -> bool:
        return self.token is not None


_state: AuthState | None = None


def configure(
    *,
    token: str | None = None,
    token_file: Path | None = None,
    disabled: bool | None = None,
) -> AuthState:
    """Resolve the auth token once. Later calls return the existing state (like ``db.configure``)."""
    global _state
    if _state is not None:
        return _state

    if disabled is None:
        disabled = _env_flag(NO_AUTH_ENV)
    if disabled:
        _state = AuthState(token=None, source="disabled")
        LOG.warning("authentication is DISABLED: anyone who can reach this server controls every project")
        return _state

    source = "explicit"
    if token is None:
        token = os.environ.get(TOKEN_ENV)
        source = "env"
    token = (token or "").strip() or None
    if token is None:
        token, source = _load_or_generate_token(token_file or default_token_file())

    _state = AuthState(token=token, source=source)
    LOG.info("authentication enabled (token source: %s)", source)
    return _state


def reset() -> None:
    """Forget the resolved state so the next ``configure`` runs again. Only tests need this."""
    global _state
    _state = None


def state() -> AuthState:
    return _state if _state is not None else configure()


def default_token_file() -> Path:
    override = os.environ.get(TOKEN_FILE_ENV, "").strip()
    if override:
        return Path(override)
    return (db._db_path or db.DEFAULT_DB).parent / TOKEN_FILE_NAME


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _load_or_generate_token(path: Path) -> tuple[str, str]:
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            LOG.info("using auth token from %s", path)
            return existing, "file"
        LOG.warning("auth token file %s is empty; generating a new token", path)

    token = secrets.token_urlsafe(GENERATED_TOKEN_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_private(path, token + "\n")
    LOG.warning(
        "no auth token configured; generated one and saved it to %s\n"
        "    token: %s\n"
        "    Use it to sign in to the console, and set it as server_token in dispatch.yaml "
        "(or CAIRN_SERVER_TOKEN) for the dispatcher.",
        path,
        token,
    )
    return token, "generated"


def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


# --- credential checks -----------------------------------------------------------------


def session_value(token: str) -> str:
    """Opaque cookie value derived from the token; it cannot be turned back into the token."""
    return hmac.new(token.encode("utf-8"), _SESSION_PURPOSE, hashlib.sha256).hexdigest()


def _matches(expected: str, provided: str | None) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def is_authenticated(request: Request) -> bool:
    current = state()
    if current.token is None:
        return True
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and _matches(current.token, credentials.strip()):
        return True
    return _matches(session_value(current.token), request.cookies.get(SESSION_COOKIE))


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


class AuthMiddleware:
    """Default-deny: every HTTP request needs credentials unless its path is public."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if is_public(request.url.path) or is_authenticated(request):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {"detail": "Authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


# --- browser session endpoints ---------------------------------------------------------


class LoginRequest(BaseModel):
    token: str


class AuthStatus(BaseModel):
    auth_required: bool
    authenticated: bool


router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/status", response_model=AuthStatus)
def auth_status(request: Request) -> AuthStatus:
    current = state()
    return AuthStatus(auth_required=current.enabled, authenticated=is_authenticated(request))


@router.post("/login", status_code=204, response_class=Response)
def login(body: LoginRequest, request: Request) -> Response:
    current = state()
    response = Response(status_code=204)
    if current.token is None:
        return response
    if not _matches(current.token, body.token.strip()):
        raise HTTPException(401, "Invalid token")
    response.set_cookie(
        SESSION_COOKIE,
        session_value(current.token),
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
        httponly=True,
        samesite="strict",
        secure=_is_https(request),
    )
    return response


@router.post("/logout", status_code=204, response_class=Response)
def logout(request: Request) -> Response:
    response = Response(status_code=204)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="strict",
        secure=_is_https(request),
    )
    return response
