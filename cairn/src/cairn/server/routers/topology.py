"""Network-topology tab: derive a host/cred/access graph from a project's facts.

GET  /topology/models                 -> extractor models for the settings UI
GET  /projects/{id}/topology          -> cached topology (if any) + staleness
POST /projects/{id}/topology/refresh  -> (re)generate from current facts, cache, return
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query

from cairn.server import topology_extract
from cairn.server.db import get_conn
from cairn.server.services import get_project_or_404, utcnow

router = APIRouter(tags=["topology"])


@router.get("/topology/models")
def list_topology_models():
    """Extractor models for the settings dropdown (availability + recommended)."""
    return topology_extract.list_models()


def _load_facts(conn, project_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ?", (project_id,)
    ).fetchall()
    return [{"id": r["id"], "description": r["description"]} for r in rows]


def _cached(conn, project_id: str):
    return conn.execute(
        "SELECT data, facts_count, generated_at FROM topology WHERE project_id = ?",
        (project_id,),
    ).fetchone()


@router.get("/projects/{project_id}/topology")
def get_topology(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        live = len(_load_facts(conn, project_id))
        row = _cached(conn, project_id)
        if row is None:
            return {"exists": False, "live_facts": live}
        return {
            "exists": True,
            "data": json.loads(row["data"]),
            "facts_count": row["facts_count"],
            "generated_at": row["generated_at"],
            "live_facts": live,
            "stale": live != row["facts_count"],
        }


@router.post("/projects/{project_id}/topology/refresh")
def refresh_topology(project_id: str, model: str | None = Query(default=None)):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        facts = _load_facts(conn, project_id)
        origin = next((f["description"] for f in facts if f["id"] == "origin"), "")
        goal = next((f["description"] for f in facts if f["id"] == "goal"), "")

    content_facts = [f for f in facts if f["id"] not in ("origin", "goal")]
    if not content_facts:
        raise HTTPException(409, "No discovery facts yet — nothing to map")

    try:
        data = topology_extract.extract_topology(origin, goal, facts, model_id=model)
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    generated_at = utcnow()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO topology (project_id, data, facts_count, generated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET
                 data = excluded.data,
                 facts_count = excluded.facts_count,
                 generated_at = excluded.generated_at""",
            (project_id, json.dumps(data, ensure_ascii=False), len(facts), generated_at),
        )

    return {
        "exists": True,
        "data": data,
        "facts_count": len(facts),
        "generated_at": generated_at,
        "live_facts": len(facts),
        "stale": False,
        "model": model,
    }
