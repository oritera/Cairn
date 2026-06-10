import json

from fastapi import APIRouter, Query

from cairn.server.db import get_conn
from cairn.server.models import CreateOpsEventRequest, OpsEvent, OpsSummary, OpsSummaryProject, ProjectDiagnostics
from cairn.server.services import (
    expire_reason_leases,
    expire_workers,
    get_project_or_404,
    project_diagnostics_from_row,
    utcnow,
)

router = APIRouter(tags=["ops"])


PROJECT_DIAGNOSTICS_SELECT = """
    SELECT p.*,
        (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
        (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count,
        (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
        (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NOT NULL) AS working_intent_count,
        (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NULL) AS unclaimed_intent_count,
        (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NOT NULL) AS concluded_intent_count
    FROM projects p
"""


@router.get("/ops/summary", response_model=OpsSummary)
def ops_summary():
    with get_conn() as conn:
        expire_workers(conn)
        expire_reason_leases(conn)
        rows = conn.execute(f"{PROJECT_DIAGNOSTICS_SELECT} ORDER BY p.created_at").fetchall()
        diagnostics = [project_diagnostics_from_row(row) for row in rows]
        projects = [
            OpsSummaryProject(
                project_id=item.project_id,
                title=item.title,
                status=item.status,
                severity=item.severity,
                message=item.message,
                next_action=item.next_action,
                fact_count=item.fact_count,
                hint_count=item.hint_count,
                intent_count=item.intent_count,
                working_intent_count=item.working_intent_count,
                unclaimed_intent_count=item.unclaimed_intent_count,
                reason=item.reason,
            )
            for item in diagnostics
        ]
        return OpsSummary(
            project_count=len(diagnostics),
            active_count=sum(1 for item in diagnostics if item.status == "active"),
            stopped_count=sum(1 for item in diagnostics if item.status == "stopped"),
            completed_count=sum(1 for item in diagnostics if item.status == "completed"),
            running_count=sum(1 for item in diagnostics if item.severity == "running"),
            attention_count=sum(1 for item in diagnostics if item.severity in ("attention", "blocked")),
            working_intent_count=sum(item.working_intent_count for item in diagnostics),
            unclaimed_intent_count=sum(item.unclaimed_intent_count for item in diagnostics),
            reason_count=sum(1 for item in diagnostics if item.reason is not None),
            projects=projects,
        )


@router.get("/projects/{project_id}/diagnostics", response_model=ProjectDiagnostics)
def project_diagnostics(project_id: str):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)
        get_project_or_404(conn, project_id)
        row = conn.execute(
            f"{PROJECT_DIAGNOSTICS_SELECT} WHERE p.id = ?",
            (project_id,),
        ).fetchone()
        assert row is not None
        return project_diagnostics_from_row(row)


def _ops_event_from_row(row) -> OpsEvent:
    details = None
    if row["details_json"]:
        details = json.loads(row["details_json"])
    return OpsEvent(
        id=row["id"],
        project_id=row["project_id"],
        event_type=row["event_type"],
        task_type=row["task_type"],
        worker=row["worker"],
        intent_id=row["intent_id"],
        severity=row["severity"],
        message=row["message"],
        details=details,
        created_at=row["created_at"],
    )


@router.post("/ops/events", response_model=OpsEvent, status_code=201)
def create_ops_event(body: CreateOpsEventRequest):
    with get_conn() as conn:
        if body.project_id is not None:
            get_project_or_404(conn, body.project_id)
        now = utcnow()
        details_json = json.dumps(body.details, ensure_ascii=False, sort_keys=True) if body.details is not None else None
        cursor = conn.execute(
            """
            INSERT INTO ops_events
                (project_id, event_type, task_type, worker, intent_id, severity, message, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.project_id,
                body.event_type,
                body.task_type,
                body.worker,
                body.intent_id,
                body.severity,
                body.message,
                details_json,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM ops_events WHERE id = ?", (cursor.lastrowid,)).fetchone()
        assert row is not None
        return _ops_event_from_row(row)


@router.get("/ops/events", response_model=list[OpsEvent])
def list_ops_events(
    project_id: str | None = None,
    severity: str | None = None,
    event_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    query = "SELECT * FROM ops_events"
    clauses = []
    params: list[str | int] = []
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if severity is not None:
        clauses.append("severity = ?")
        params.append(severity)
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_ops_event_from_row(row) for row in rows]
