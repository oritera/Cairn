from __future__ import annotations

import json
import time
from collections.abc import Iterator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from cairn.server.db import get_conn
from cairn.server.event_store import insert_runtime_event, runtime_event_from_row
from cairn.server.models import CreateRuntimeEventRequest, RuntimeEvent
from cairn.server.services import get_project_or_404

router = APIRouter(tags=["events"])


@router.get("/projects/{project_id}/events", response_model=list[RuntimeEvent])
def list_project_events(project_id: str, after_id: int = 0, limit: int = Query(default=250, ge=1, le=1000)):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT * FROM runtime_events WHERE project_id = ? AND id > ? ORDER BY id LIMIT ?",
            (project_id, after_id, limit),
        ).fetchall()
        return [runtime_event_from_row(row) for row in rows]


@router.post("/projects/{project_id}/events", response_model=RuntimeEvent, status_code=201)
def create_project_event(project_id: str, body: CreateRuntimeEventRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return insert_runtime_event(conn, project_id=project_id, **body.model_dump())


@router.get("/events/stream")
def stream_events(project_id: str | None = None, after_id: int = 0):
    return StreamingResponse(
        _event_stream(project_id, after_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _event_stream(project_id: str | None, after_id: int) -> Iterator[str]:
    cursor = max(0, after_id)
    heartbeat_at = time.monotonic()
    while True:
        with get_conn() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT * FROM runtime_events WHERE project_id = ? AND id > ? ORDER BY id LIMIT 100",
                    (project_id, cursor),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runtime_events WHERE id > ? ORDER BY id LIMIT 100", (cursor,)
                ).fetchall()
        if rows:
            for row in rows:
                event = runtime_event_from_row(row)
                cursor = event.id
                data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event.id}\nevent: runtime\ndata: {data}\n\n"
            heartbeat_at = time.monotonic()
        elif time.monotonic() - heartbeat_at >= 15:
            yield ": heartbeat\n\n"
            heartbeat_at = time.monotonic()
        time.sleep(0.75)
