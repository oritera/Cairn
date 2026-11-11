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
    reason_timeout INTEGER NOT NULL DEFAULT 15,
    paused INTEGER NOT NULL DEFAULT 0,
    max_workers INTEGER NOT NULL DEFAULT 2,
    max_running_projects INTEGER NOT NULL DEFAULT 1,
    max_project_workers INTEGER NOT NULL DEFAULT 2,
    explore_timeout INTEGER NOT NULL DEFAULT 240,
    explore_conclude_timeout INTEGER NOT NULL DEFAULT 90,
    reason_task_timeout INTEGER NOT NULL DEFAULT 240,
    bootstrap_timeout INTEGER NOT NULL DEFAULT 120,
    bootstrap_conclude_timeout INTEGER NOT NULL DEFAULT 60,
    reason_max_intents INTEGER NOT NULL DEFAULT 2
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

-- Upper bounds the dispatcher publishes from its startup config (single row). The UI clamps
-- sliders to these; raising a ceiling needs a dispatcher restart.
CREATE TABLE IF NOT EXISTS dispatcher_ceilings (
    rowid INTEGER PRIMARY KEY CHECK (rowid = 1),
    max_workers INTEGER NOT NULL DEFAULT 8,
    max_running_projects INTEGER NOT NULL DEFAULT 8,
    max_project_workers INTEGER NOT NULL DEFAULT 8,
    config_path TEXT,
    reported_at TEXT
);

INSERT OR IGNORE INTO dispatcher_ceilings (rowid) VALUES (1);

-- Latest telemetry snapshot pushed by the dispatcher each tick (single row, JSON blob) plus a
-- bounded time-series for the dashboard charts.
CREATE TABLE IF NOT EXISTS telemetry (
    rowid INTEGER PRIMARY KEY CHECK (rowid = 1),
    snapshot TEXT,
    updated_at TEXT
);

INSERT OR IGNORE INTO telemetry (rowid, snapshot, updated_at) VALUES (1, NULL, NULL);

CREATE TABLE IF NOT EXISTS telemetry_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    dispatcher_rss_mb REAL,
    host_mem_used_mb REAL,
    host_mem_total_mb REAL,
    cpu_load1 REAL,
    running_tasks INTEGER,
    outcomes_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_telemetry_history_ts ON telemetry_history (ts);

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
    timeout_override INTEGER,
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
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

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

CREATE TABLE IF NOT EXISTS topology (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    facts_count INTEGER NOT NULL,
    generated_at TEXT NOT NULL
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
        _ensure_settings_columns(conn)
        _ensure_intent_columns(conn)


def _ensure_intent_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(intents)")}
    if "timeout_override" not in columns:
        conn.execute("ALTER TABLE intents ADD COLUMN timeout_override INTEGER")


def _ensure_settings_columns(conn: sqlite3.Connection) -> None:
    """Add live-control columns to a pre-existing settings table (CREATE IF NOT EXISTS
    skips them on an old DB)."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
    additions = (
        ("paused", "INTEGER NOT NULL DEFAULT 0"),
        ("max_workers", "INTEGER NOT NULL DEFAULT 2"),
        ("max_running_projects", "INTEGER NOT NULL DEFAULT 1"),
        ("max_project_workers", "INTEGER NOT NULL DEFAULT 2"),
        ("explore_timeout", "INTEGER NOT NULL DEFAULT 240"),
        ("explore_conclude_timeout", "INTEGER NOT NULL DEFAULT 90"),
        ("reason_task_timeout", "INTEGER NOT NULL DEFAULT 240"),
        ("bootstrap_timeout", "INTEGER NOT NULL DEFAULT 120"),
        ("bootstrap_conclude_timeout", "INTEGER NOT NULL DEFAULT 60"),
        ("reason_max_intents", "INTEGER NOT NULL DEFAULT 2"),
    )
    for name, decl in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE settings ADD COLUMN {name} {decl}")


def _ensure_project_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "bootstrap_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN bootstrap_enabled INTEGER NOT NULL DEFAULT 1")
        if "bootstrap_mode" in columns:
            conn.execute(
                "UPDATE projects SET bootstrap_enabled = CASE WHEN bootstrap_mode = 'disabled' THEN 0 ELSE 1 END"
            )


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
