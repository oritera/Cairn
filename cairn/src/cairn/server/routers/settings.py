from __future__ import annotations

from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import DispatcherCeilings, Settings

router = APIRouter(tags=["settings"])

_SETTINGS_COLUMNS = (
    "intent_timeout",
    "reason_timeout",
    "paused",
    "max_workers",
    "max_running_projects",
    "max_project_workers",
    "explore_timeout",
    "explore_conclude_timeout",
    "reason_task_timeout",
    "bootstrap_timeout",
    "bootstrap_conclude_timeout",
    "reason_max_intents",
)


def _row_to_settings(row) -> Settings:
    return Settings(
        intent_timeout=row["intent_timeout"],
        reason_timeout=row["reason_timeout"],
        paused=bool(row["paused"]),
        max_workers=row["max_workers"],
        max_running_projects=row["max_running_projects"],
        max_project_workers=row["max_project_workers"],
        explore_timeout=row["explore_timeout"],
        explore_conclude_timeout=row["explore_conclude_timeout"],
        reason_task_timeout=row["reason_task_timeout"],
        bootstrap_timeout=row["bootstrap_timeout"],
        bootstrap_conclude_timeout=row["bootstrap_conclude_timeout"],
        reason_max_intents=row["reason_max_intents"],
    )


@router.get("/settings", response_model=Settings)
def get_settings():
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_SETTINGS_COLUMNS)} FROM settings WHERE rowid = 1"
        ).fetchone()
        return _row_to_settings(row)


@router.put("/settings", response_model=Settings)
def update_settings(body: Settings):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE settings SET
                intent_timeout = ?, reason_timeout = ?, paused = ?,
                max_workers = ?, max_running_projects = ?, max_project_workers = ?,
                explore_timeout = ?, explore_conclude_timeout = ?, reason_task_timeout = ?,
                bootstrap_timeout = ?, bootstrap_conclude_timeout = ?, reason_max_intents = ?
            WHERE rowid = 1
            """,
            (
                body.intent_timeout,
                body.reason_timeout,
                int(body.paused),
                body.max_workers,
                body.max_running_projects,
                body.max_project_workers,
                body.explore_timeout,
                body.explore_conclude_timeout,
                body.reason_task_timeout,
                body.bootstrap_timeout,
                body.bootstrap_conclude_timeout,
                body.reason_max_intents,
            ),
        )
        return body


@router.get("/dispatcher/ceilings", response_model=DispatcherCeilings)
def get_ceilings():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT max_workers, max_running_projects, max_project_workers, config_path, reported_at "
            "FROM dispatcher_ceilings WHERE rowid = 1"
        ).fetchone()
        return DispatcherCeilings(
            max_workers=row["max_workers"],
            max_running_projects=row["max_running_projects"],
            max_project_workers=row["max_project_workers"],
            config_path=row["config_path"],
            reported_at=row["reported_at"],
        )


@router.put("/dispatcher/ceilings", response_model=DispatcherCeilings)
def put_ceilings(body: DispatcherCeilings):
    """Called by the dispatcher on startup to publish the upper bounds it can honour
    without a restart (its ThreadPool size and configured project limits)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE dispatcher_ceilings SET max_workers = ?, max_running_projects = ?, "
            "max_project_workers = ?, config_path = ?, reported_at = ? WHERE rowid = 1",
            (
                body.max_workers,
                body.max_running_projects,
                body.max_project_workers,
                body.config_path,
                body.reported_at,
            ),
        )
        return body
