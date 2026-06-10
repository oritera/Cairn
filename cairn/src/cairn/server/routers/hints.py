from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import CreateHintRequest, Hint
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
            """
            INSERT INTO hints
                (id, project_id, content, creator, hint_type, priority, target_type, target_id, pinned, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hid,
                project_id,
                body.content,
                body.creator,
                body.hint_type,
                body.priority,
                body.target_type,
                body.target_id,
                int(body.pinned),
                now,
            ),
        )
        return Hint(
            id=hid,
            content=body.content,
            creator=body.creator,
            hint_type=body.hint_type,
            priority=body.priority,
            target_type=body.target_type,
            target_id=body.target_id,
            pinned=body.pinned,
            created_at=now,
        )
