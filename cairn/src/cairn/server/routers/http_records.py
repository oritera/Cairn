from __future__ import annotations

import json

from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import CreateHttpRecordRequest, HttpRecord
from cairn.server.services import get_project_or_404, utcnow

router = APIRouter(tags=["http-records"])


@router.get("/projects/{project_id}/http-records", response_model=list[HttpRecord])
def list_http_records(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT * FROM http_records WHERE project_id = ? ORDER BY created_at, id", (project_id,)
        ).fetchall()
        return [_record_from_row(row) for row in rows]


@router.post("/projects/{project_id}/http-records", response_model=HttpRecord, status_code=201)
def create_http_record(project_id: str, body: CreateHttpRecordRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        counter = conn.execute(
            "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = 'http_record'",
            (project_id,),
        ).fetchone()
        value = (counter["value"] if counter else 0) + 1
        conn.execute(
            "INSERT OR REPLACE INTO scoped_counters (project_id, kind, value) VALUES (?, 'http_record', ?)",
            (project_id, value),
        )
        record_id = f"r{value:03d}"
        created_at = utcnow()
        conn.execute(
            """
            INSERT INTO http_records
                (id, project_id, intent_id, worker, method, url, request_headers_json, request_body,
                 response_status, response_headers_json, response_body, significance, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                project_id,
                body.intent_id,
                body.worker,
                body.method,
                body.url,
                json.dumps(body.request.headers, ensure_ascii=False),
                body.request.body,
                body.response.status,
                json.dumps(body.response.headers, ensure_ascii=False),
                body.response.body,
                body.significance,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM http_records WHERE project_id = ? AND id = ?", (project_id, record_id)
        ).fetchone()
        return _record_from_row(row)


def _record_from_row(row) -> HttpRecord:
    return HttpRecord(
        id=row["id"],
        project_id=row["project_id"],
        intent_id=row["intent_id"],
        worker=row["worker"],
        method=row["method"],
        url=row["url"],
        request={"headers": json.loads(row["request_headers_json"]), "body": row["request_body"]},
        response={
            "status": row["response_status"],
            "headers": json.loads(row["response_headers_json"]),
            "body": row["response_body"],
        },
        significance=row["significance"],
        created_at=row["created_at"],
    )
