from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.models import CreateHintRequest, Hint, UpdateHintRequest
from cairn.server.services import check_project_hint_writable, next_hint_id, utcnow

router = APIRouter(tags=["hints"])


@router.post(
    "/projects/{project_id}/hints",
    response_model=Hint,
    status_code=201,
)
def create_hint(project_id: str, body: CreateHintRequest):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)

        now = utcnow()
        hid = next_hint_id(conn, project_id)
        conn.execute(
            "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            (hid, project_id, body.content, body.creator, now),
        )
        return Hint(id=hid, content=body.content, creator=body.creator, created_at=now)


@router.put(
    "/projects/{project_id}/hints/{hint_id}",
    response_model=Hint,
)
def update_hint(project_id: str, hint_id: str, body: UpdateHintRequest):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        row = conn.execute(
            "SELECT * FROM hints WHERE id = ? AND project_id = ?",
            (hint_id, project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Hint not found")
        conn.execute(
            "UPDATE hints SET content = ? WHERE id = ? AND project_id = ?",
            (body.content, hint_id, project_id),
        )
        return Hint(id=hint_id, content=body.content, creator=row["creator"], created_at=row["created_at"])


@router.delete(
    "/projects/{project_id}/hints/{hint_id}",
    status_code=204,
)
def delete_hint(project_id: str, hint_id: str):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        cur = conn.execute(
            "DELETE FROM hints WHERE id = ? AND project_id = ?",
            (hint_id, project_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Hint not found")
