from __future__ import annotations

import base64
import hashlib
import json
import sqlite3

from cairn.server.models import AuditEvent, AuditEventCreate
from cairn.server.services import utcnow


class AuditProjectNotFoundError(LookupError):
    pass


class AuditEventCollisionError(ValueError):
    pass


def append_audit_event(
    conn: sqlite3.Connection,
    event: AuditEventCreate,
    *,
    max_payload_bytes: int,
) -> AuditEvent:
    payload_json = json.dumps(event.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload_bytes = payload_json.encode("utf-8")
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    existing_row = conn.execute("SELECT * FROM audit_events WHERE event_id = ?", (event.event_id,)).fetchone()
    if existing_row is not None:
        existing = _event_from_row(existing_row)
        if _matches_request(existing, event, payload_sha256):
            return existing
        raise AuditEventCollisionError(event.event_id)

    project = conn.execute("SELECT 1 FROM projects WHERE id = ?", (event.project_id,)).fetchone()
    if project is None:
        raise AuditProjectNotFoundError(event.project_id)

    stored_payload = payload_json
    truncated = False
    if len(payload_bytes) > max_payload_bytes:
        preview = payload_bytes[:max_payload_bytes].decode("utf-8", errors="ignore")
        stored_payload = json.dumps(
            {"preview": preview, "original_bytes": len(payload_bytes), "truncated": True},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        truncated = True

    created_at = utcnow()
    conn.execute(
        """
        INSERT INTO audit_events (
            event_id, action_id, run_id, project_id, intent_id, worker, phase,
            event_type, tool_name, decision, rule_id, reason, payload_json,
            payload_sha256, truncated, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.action_id,
            event.run_id,
            event.project_id,
            event.intent_id,
            event.worker,
            event.phase,
            event.event_type,
            event.tool_name,
            event.decision,
            event.rule_id,
            event.reason,
            stored_payload,
            payload_sha256,
            int(truncated),
            created_at,
        ),
    )
    row = conn.execute("SELECT * FROM audit_events WHERE event_id = ?", (event.event_id,)).fetchone()
    assert row is not None
    return _event_from_row(row)


def list_audit_events(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    intent_id: str | None = None,
    run_id: str | None = None,
    event_type: str | None = None,
    decision: str | None = None,
    tool_name: str | None = None,
    after: str | None = None,
    limit: int = 100,
) -> tuple[list[AuditEvent], str | None]:
    clauses = ["project_id = ?"]
    params: list[object] = [project_id]
    for column, value in (
        ("intent_id", intent_id),
        ("run_id", run_id),
        ("event_type", event_type),
        ("decision", decision),
        ("tool_name", tool_name),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if after is not None:
        created_at, event_id = _decode_cursor(after)
        clauses.append("(created_at > ? OR (created_at = ? AND event_id > ?))")
        params.extend([created_at, created_at, event_id])

    params.append(limit + 1)
    rows = conn.execute(
        f"SELECT * FROM audit_events WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at, event_id LIMIT ?",
        params,
    ).fetchall()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = _encode_cursor(page_rows[-1]["created_at"], page_rows[-1]["event_id"]) if has_more else None
    return [_event_from_row(row) for row in page_rows], next_cursor


def _encode_cursor(created_at: str, event_id: str) -> str:
    raw = json.dumps([created_at, event_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid audit cursor") from exc
    if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, str) and item for item in value):
        raise ValueError("invalid audit cursor")
    return value[0], value[1]


def _matches_request(existing: AuditEvent, requested: AuditEventCreate, payload_sha256: str) -> bool:
    return (
        existing.action_id == requested.action_id
        and existing.run_id == requested.run_id
        and existing.project_id == requested.project_id
        and existing.intent_id == requested.intent_id
        and existing.worker == requested.worker
        and existing.phase == requested.phase
        and existing.event_type == requested.event_type
        and existing.tool_name == requested.tool_name
        and existing.decision == requested.decision
        and existing.rule_id == requested.rule_id
        and existing.reason == requested.reason
        and existing.payload_sha256 == payload_sha256
    )


def _event_from_row(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        event_id=row["event_id"],
        action_id=row["action_id"],
        run_id=row["run_id"],
        project_id=row["project_id"],
        intent_id=row["intent_id"],
        worker=row["worker"],
        phase=row["phase"],
        event_type=row["event_type"],
        tool_name=row["tool_name"],
        decision=row["decision"],
        rule_id=row["rule_id"],
        reason=row["reason"],
        payload=json.loads(row["payload_json"]),
        payload_sha256=row["payload_sha256"],
        truncated=bool(row["truncated"]),
        created_at=row["created_at"],
    )
