from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    hint_type TEXT NOT NULL DEFAULT 'note',
    priority TEXT NOT NULL DEFAULT 'normal',
    target_type TEXT,
    target_id TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS ops_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    task_type TEXT,
    worker TEXT,
    intent_id TEXT,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ops_events_project_created ON ops_events(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ops_events_type_created ON ops_events(event_type, created_at);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_project_columns(conn)
        _ensure_hint_columns(conn)


def _ensure_project_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "bootstrap_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN bootstrap_enabled INTEGER NOT NULL DEFAULT 1")
        if "bootstrap_mode" in columns:
            conn.execute(
                "UPDATE projects SET bootstrap_enabled = CASE WHEN bootstrap_mode = 'disabled' THEN 0 ELSE 1 END"
            )


def _ensure_hint_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(hints)")}
    additions = {
        "hint_type": "ALTER TABLE hints ADD COLUMN hint_type TEXT NOT NULL DEFAULT 'note'",
        "priority": "ALTER TABLE hints ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'",
        "target_type": "ALTER TABLE hints ADD COLUMN target_type TEXT",
        "target_id": "ALTER TABLE hints ADD COLUMN target_id TEXT",
        "pinned": "ALTER TABLE hints ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
    }
    for column, statement in additions.items():
        if column not in columns:
            conn.execute(statement)


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
