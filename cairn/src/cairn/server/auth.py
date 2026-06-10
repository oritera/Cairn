from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

AUTH_COOKIE = "cairn_session"
SESSION_TTL_SECONDS = 60 * 60 * 12

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


def auth_enabled() -> bool:
    if os.getenv("CAIRN_AUTH_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        return False
    return bool(expected_password())


def expected_username() -> str:
    return os.getenv("CAIRN_AUTH_USERNAME", "gumingyao_sx").strip() or "gumingyao_sx"


def expected_password() -> str:
    return os.getenv("CAIRN_AUTH_PASSWORD", "").strip()


def _session_secret() -> str:
    return os.getenv("CAIRN_AUTH_SECRET", "").strip() or expected_password()


def _encode_json(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_json(data: str) -> dict[str, Any] | None:
    padding = "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode((data + padding).encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _signature(payload: str) -> str:
    secret = _session_secret()
    return hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()


def create_session_token(username: str) -> str:
    payload = _encode_json(
        {
            "u": username,
            "exp": int(time.time()) + SESSION_TTL_SECONDS,
            "n": secrets.token_urlsafe(12),
        }
    )
    return f"{payload}.{_signature(payload)}"


def verify_session_token(token: str | None) -> str | None:
    if not auth_enabled() or not token or "." not in token:
        return None
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _signature(payload)):
        return None
    data = _decode_json(payload)
    if not data:
        return None
    username = data.get("u")
    expires_at = data.get("exp")
    if username != expected_username() or not isinstance(expires_at, int):
        return None
    if expires_at < int(time.time()):
        return None
    return username


def public_path(path: str) -> bool:
    return (
        path == "/healthz"
        or path == "/favicon.ico"
        or path.startswith("/static/")
        or path.startswith("/auth/")
    )


async def auth_middleware(request: Request, call_next):
    if not auth_enabled() or public_path(request.url.path):
        return await call_next(request)

    username = verify_session_token(request.cookies.get(AUTH_COOKIE))
    if username:
        request.state.auth_user = username
        return await call_next(request)

    if request.url.path == "/" and request.method == "GET":
        from cairn.server.app import STATIC_DIR

        return FileResponse(STATIC_DIR / "login.html")
    return JSONResponse({"detail": "Authentication required"}, status_code=401)


@router.get("/auth/me")
def auth_me(request: Request):
    username = verify_session_token(request.cookies.get(AUTH_COOKIE))
    return {
        "auth_enabled": auth_enabled(),
        "authenticated": bool(username) or not auth_enabled(),
        "username": username,
    }


@router.post("/auth/login")
def login(body: LoginRequest, response: Response):
    if not auth_enabled():
        return {"ok": True, "auth_enabled": False}
    if body.username != expected_username() or not hmac.compare_digest(body.password, expected_password()):
        raise HTTPException(401, "Invalid username or password")
    response.set_cookie(
        AUTH_COOKIE,
        create_session_token(body.username),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True, "auth_enabled": True, "username": body.username}


@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(AUTH_COOKIE)
    return {"ok": True}
