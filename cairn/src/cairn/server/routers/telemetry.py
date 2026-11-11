"""Telemetry pushed by the dispatcher each tick. The dispatcher is the only writer; the
dashboard reads the latest snapshot (merged in /dashboard) plus a bounded time-series for
charts. The snapshot is stored verbatim as JSON so its shape can evolve without a schema
change — the server only cracks out a few scalars for the history table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from cairn.server.db import get_conn

router = APIRouter(tags=["telemetry"])

_HISTORY_CAP = 1000


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@router.post("/dispatcher/telemetry")
def push_telemetry(body: dict):
    now = datetime.now(timezone.utc).isoformat()
    snapshot = json.dumps(body, ensure_ascii=False)
    dispatcher = body.get("dispatcher") or {}
    host = body.get("host") or {}
    running = body.get("running") or []
    outcomes = body.get("outcomes") or {}
    with get_conn() as conn:
        conn.execute(
            "UPDATE telemetry SET snapshot = ?, updated_at = ? WHERE rowid = 1",
            (snapshot, now),
        )
        conn.execute(
            "INSERT INTO telemetry_history (ts, dispatcher_rss_mb, host_mem_used_mb, "
            "host_mem_total_mb, cpu_load1, running_tasks, outcomes_json) VALUES (?,?,?,?,?,?,?)",
            (
                now,
                _num(dispatcher.get("rss_mb")),
                _num(host.get("mem_used_mb")),
                _num(host.get("mem_total_mb")),
                _num(host.get("cpu_load1")),
                len(running) if isinstance(running, list) else None,
                json.dumps(outcomes, ensure_ascii=False),
            ),
        )
        # keep the history bounded
        conn.execute(
            "DELETE FROM telemetry_history WHERE id NOT IN "
            "(SELECT id FROM telemetry_history ORDER BY id DESC LIMIT ?)",
            (_HISTORY_CAP,),
        )
    return {"ok": True, "stored_at": now}


@router.get("/dispatcher/telemetry")
def get_telemetry():
    with get_conn() as conn:
        row = conn.execute("SELECT snapshot, updated_at FROM telemetry WHERE rowid = 1").fetchone()
    if not row or not row["snapshot"]:
        return {"snapshot": None, "updated_at": None}
    try:
        snapshot = json.loads(row["snapshot"])
    except (TypeError, ValueError):
        snapshot = None
    return {"snapshot": snapshot, "updated_at": row["updated_at"]}


def read_latest_snapshot() -> tuple[dict | None, str | None]:
    """Helper for the dashboard router to merge the latest telemetry."""
    with get_conn() as conn:
        row = conn.execute("SELECT snapshot, updated_at FROM telemetry WHERE rowid = 1").fetchone()
    if not row or not row["snapshot"]:
        return None, None
    try:
        return json.loads(row["snapshot"]), row["updated_at"]
    except (TypeError, ValueError):
        return None, row["updated_at"]
