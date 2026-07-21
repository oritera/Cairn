from __future__ import annotations

import json
import sqlite3
from typing import Any

from cairn.server.models import RuntimeEvent
from cairn.server.services import utcnow


def insert_runtime_event(
    conn: sqlite3.Connection,
    *,
    project_id: str | None,
    event_type: str,
    status: str,
    message: str,
    phase: str | None = None,
    worker: str | None = None,
    intent_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> RuntimeEvent:
    created_at = utcnow()
    cursor = conn.execute(
        """
        INSERT INTO runtime_events
            (project_id, event_type, phase, status, message, worker, intent_id, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            event_type,
            phase,
            status,
            message,
            worker,
            intent_id,
            json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            created_at,
        ),
    )
    return RuntimeEvent(
        id=int(cursor.lastrowid),
        project_id=project_id,
        event_type=event_type,
        phase=phase,
        status=status,
        message=message,
        worker=worker,
        intent_id=intent_id,
        payload=payload,
        created_at=created_at,
    )


def runtime_event_from_row(row: sqlite3.Row) -> RuntimeEvent:
    payload = json.loads(row["payload_json"]) if row["payload_json"] else None
    return RuntimeEvent(
        id=row["id"],
        project_id=row["project_id"],
        event_type=row["event_type"],
        phase=row["phase"],
        status=row["status"],
        message=row["message"],
        worker=row["worker"],
        intent_id=row["intent_id"],
        payload=payload,
        created_at=row["created_at"],
    )
