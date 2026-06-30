from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from cairn.server.db import get_conn

_JWT_ALGORITHM = "HS256"
_ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24
_bearer = HTTPBearer(auto_error=False)
_api_keys: set[str] = set()


def _get_or_create_secret(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM kv WHERE key = 'jwt_secret'").fetchone()
    if row is not None:
        return row["value"]
    secret = secrets.token_hex(32)
    conn.execute("INSERT INTO kv (key, value) VALUES ('jwt_secret', ?)", (secret,))
    return secret


def _jwt_secret() -> str:
    with get_conn() as conn:
        return _get_or_create_secret(conn)


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def _create_token(user_id: str, username: str) -> str:
    payload = {
        "sub": user_id,
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=_ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_JWT_ALGORITHM)


class TokenUser(BaseModel):
    id: str
    username: str


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> TokenUser:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing token")
    try:
        payload = jwt.decode(credentials.credentials, _jwt_secret(), algorithms=[_JWT_ALGORITHM])
        return TokenUser(id=payload["sub"], username=payload["username"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired")
    except (jwt.InvalidTokenError, KeyError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")


def configure_api_keys(keys: list[str]) -> None:
    _api_keys.clear()
    _api_keys.update(k for k in keys if k)


def _api_keys_configured() -> bool:
    return len(_api_keys) > 0


def require_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> TokenUser | None:
    if x_api_key and x_api_key in _api_keys:
        return None
    if credentials is not None:
        try:
            payload = jwt.decode(credentials.credentials, _jwt_secret(), algorithms=[_JWT_ALGORITHM])
            return TokenUser(id=payload["sub"], username=payload["username"])
        except (jwt.InvalidTokenError, KeyError):
            pass
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        v = v.strip()
        if not v.isalnum() and not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError("username must be alphanumeric, hyphens and underscores only")
        return v


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str


class UserInfoResponse(BaseModel):
    id: str
    username: str
    created_at: str


def _generate_user_id(username: str) -> str:
    return hashlib.sha256(f"{username}-{secrets.token_hex(8)}".encode()).hexdigest()[:16]


def register_user(req: RegisterRequest) -> TokenResponse:
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (req.username,)).fetchone()
        if existing is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "username already exists")
        user_id = _generate_user_id(req.username)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, req.username, _hash_password(req.password), now),
        )
    return TokenResponse(access_token=_create_token(user_id, req.username), username=req.username)


def login_user(req: LoginRequest) -> TokenResponse:
    with get_conn() as conn:
        row = conn.execute("SELECT id, username, password_hash FROM users WHERE username = ?", (req.username,)).fetchone()
    if row is None or not _verify_password(req.password, row["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
    return TokenResponse(access_token=_create_token(row["id"], row["username"]), username=row["username"])
