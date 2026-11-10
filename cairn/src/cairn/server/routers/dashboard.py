"""Navigation / ops dashboard — live engine vitals built entirely from the DB plus the
dispatcher's own log (no dispatcher changes required to read it).

The DB gives structure and live activity: an intent with a `worker` and no `concluded_at`
is a running explore; `projects.reason_*` is a running reason; `intents.created_at` /
`concluded_at` give real wall-clock timeline and per-task durations. Task OUTCOMES
(timeout vs clean) are not in the DB — a timed-out explore still sets `concluded_at` via
the conclude fallback — so timeout counts are parsed from the dispatcher log, whose path is
discovered from the live `cairn dispatch` process (`/proc/<pid>/fd/1`, the stdout redirect).
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.routers.settings import _SETTINGS_COLUMNS, _row_to_settings
from cairn.server.routers.telemetry import read_latest_snapshot
from cairn.server.routers.wizard import supervisor_status

router = APIRouter(tags=["dashboard"])

_LOG_TAIL_BYTES = 1_000_000  # parse only the tail of the dispatcher log
_TIMEOUT_RE = re.compile(r"tasks\.(explore|reason|bootstrap)\s+(\w[\w ]*?)\s+(?:timed out|conclude failed)")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(value: str | None, now: datetime) -> float | None:
    dt = _parse_iso(value)
    if dt is None:
        return None
    return (now - dt).total_seconds()


def _find_dispatcher() -> dict:
    """Locate the live `cairn dispatch` process and its log file via /proc."""
    info: dict = {"running": False, "pid": None, "config_path": None, "log_path": None}
    proc = Path("/proc")
    if not proc.is_dir():
        return info
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\x00")
        except (OSError, ValueError):
            continue
        parts = [p.decode("utf-8", "replace") for p in cmdline if p]
        if not any("cairn" in p for p in parts) or "dispatch" not in parts:
            continue
        # skip our own wrapper shells; require the actual dispatch invocation
        if not any(p.endswith("cairn") or p == "cairn" for p in parts):
            continue
        info["running"] = True
        info["pid"] = int(entry.name)
        if "--config" in parts:
            idx = parts.index("--config")
            if idx + 1 < len(parts):
                info["config_path"] = parts[idx + 1]
        try:
            target = os.readlink(str(entry / "fd" / "1"))
            if target and not target.startswith(("pipe:", "socket:", "/dev")):
                info["log_path"] = target
        except OSError:
            pass
        break
    return info


def _env_log_override() -> str | None:
    return os.environ.get("CAIRN_DISPATCH_LOG") or None


def _parse_timeouts(log_path: str | None) -> dict:
    """Count timeout / conclude-failure events by task type from the dispatcher log tail."""
    stats = {"explore": 0, "reason": 0, "bootstrap": 0, "recent": [], "log_path": log_path, "available": False}
    if not log_path:
        return stats
    path = Path(log_path)
    if not path.is_file():
        return stats
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _LOG_TAIL_BYTES:
                fh.seek(size - _LOG_TAIL_BYTES)
                fh.readline()  # drop partial line
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return stats
    stats["available"] = True
    for line in data.splitlines():
        if "timed out" not in line and "conclude failed" not in line:
            continue
        m = _TIMEOUT_RE.search(line)
        if not m:
            continue
        task = m.group(1)
        stats[task] = stats.get(task, 0) + 1
        ts = line[1:20] if line.startswith("[") else ""
        intent = ""
        im = re.search(r"intent=(\w+)", line)
        if im:
            intent = im.group(1)
        kind = "conclude failed" if "conclude failed" in line else "timed out"
        stats["recent"].append({"ts": ts, "task": task, "intent": intent, "kind": kind})
    stats["recent"] = stats["recent"][-15:]
    stats["total"] = stats["explore"] + stats["reason"] + stats["bootstrap"]
    return stats


def _settings_and_ceilings(conn) -> tuple:
    srow = conn.execute(
        f"SELECT {', '.join(_SETTINGS_COLUMNS)} FROM settings WHERE rowid = 1"
    ).fetchone()
    settings = _row_to_settings(srow).model_dump()
    crow = conn.execute(
        "SELECT max_workers, max_running_projects, max_project_workers, config_path, reported_at "
        "FROM dispatcher_ceilings WHERE rowid = 1"
    ).fetchone()
    ceilings = dict(crow) if crow else {}
    return settings, ceilings


def _project_rows(conn, now: datetime) -> tuple:
    projects = []
    totals = {"active": 0, "facts": 0, "open_intents": 0, "running": 0}
    for p in conn.execute("SELECT * FROM projects ORDER BY id").fetchall():
        pid = p["id"]
        fact_count = conn.execute(
            "SELECT COUNT(*) c FROM facts WHERE project_id = ? AND id NOT IN ('origin','goal')",
            (pid,),
        ).fetchone()["c"]
        open_count = conn.execute(
            "SELECT COUNT(*) c FROM intents WHERE project_id = ? AND concluded_at IS NULL",
            (pid,),
        ).fetchone()["c"]
        concluded_count = conn.execute(
            "SELECT COUNT(*) c FROM intents WHERE project_id = ? AND concluded_at IS NOT NULL",
            (pid,),
        ).fetchone()["c"]
        # running explores: claimed (worker set) + not concluded
        running = []
        for ir in conn.execute(
            "SELECT id, description, worker, created_at, last_heartbeat_at FROM intents "
            "WHERE project_id = ? AND concluded_at IS NULL AND worker IS NOT NULL",
            (pid,),
        ).fetchall():
            running.append(
                {
                    "kind": "explore",
                    "id": ir["id"],
                    "description": (ir["description"] or "")[:200],
                    "worker": ir["worker"],
                    "elapsed_s": _age_seconds(ir["created_at"], now),
                    "heartbeat_age_s": _age_seconds(ir["last_heartbeat_at"], now),
                }
            )
        if p["reason_worker"]:
            running.append(
                {
                    "kind": "reason",
                    "id": None,
                    "worker": p["reason_worker"],
                    "elapsed_s": _age_seconds(p["reason_started_at"], now),
                    "heartbeat_age_s": _age_seconds(p["reason_last_heartbeat_at"], now),
                    "trigger": p["reason_trigger"],
                }
            )
        # average + last explore duration from concluded intents
        durations = []
        last_activity = None
        for ir in conn.execute(
            "SELECT created_at, concluded_at FROM intents WHERE project_id = ? AND concluded_at IS NOT NULL",
            (pid,),
        ).fetchall():
            start = _parse_iso(ir["created_at"])
            end = _parse_iso(ir["concluded_at"])
            if start and end:
                durations.append((end - start).total_seconds())
            if end and (last_activity is None or end > last_activity):
                last_activity = end
        avg_s = round(sum(durations) / len(durations), 1) if durations else None
        projects.append(
            {
                "id": pid,
                "title": p["title"],
                "status": p["status"],
                "fact_count": fact_count,
                "open_intents": open_count,
                "concluded_intents": concluded_count,
                "running": running,
                "avg_explore_s": avg_s,
                "last_activity_age_s": _age_seconds(last_activity.isoformat() if last_activity else None, now),
            }
        )
        if p["status"] == "active":
            totals["active"] += 1
        totals["facts"] += fact_count
        totals["open_intents"] += open_count
        totals["running"] += len(running)
    return projects, totals


def _activity_feed(conn, now: datetime, limit: int = 10) -> dict:
    """Running tasks with descriptions + most recent facts across all active projects."""
    running = []
    for p in conn.execute("SELECT id, title FROM projects WHERE status = 'active'").fetchall():
        pid = p["id"]
        for ir in conn.execute(
            "SELECT id, description, worker, created_at FROM intents "
            "WHERE project_id = ? AND concluded_at IS NULL AND worker IS NOT NULL "
            "ORDER BY created_at DESC",
            (pid,),
        ).fetchall():
            running.append({
                "project": pid,
                "project_title": p["title"],
                "kind": "explore",
                "id": ir["id"],
                "description": (ir["description"] or "")[:300],
                "worker": ir["worker"],
                "elapsed_s": _age_seconds(ir["created_at"], now),
            })
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        if row and row["reason_worker"]:
            running.append({
                "project": pid,
                "project_title": p["title"],
                "kind": "reason",
                "id": None,
                "description": row["reason_trigger"] or "planning next actions",
                "worker": row["reason_worker"],
                "elapsed_s": _age_seconds(row["reason_started_at"], now),
            })
    recent_facts = []
    for f in conn.execute(
        "SELECT f.id, f.project_id, f.description, p.title as project_title "
        "FROM facts f JOIN projects p ON f.project_id = p.id "
        "WHERE f.id NOT IN ('origin','goal') "
        "ORDER BY CAST(REPLACE(f.id, 'f', '') AS INTEGER) DESC LIMIT ?",
        (limit,),
    ).fetchall():
        recent_facts.append({
            "id": f["id"],
            "project": f["project_id"],
            "project_title": f["project_title"],
            "description": (f["description"] or "")[:300],
        })
    recent_intents = []
    for i in conn.execute(
        "SELECT i.id, i.project_id, i.description, i.concluded_at, p.title as project_title "
        "FROM intents i JOIN projects p ON i.project_id = p.id "
        "WHERE i.concluded_at IS NOT NULL "
        "ORDER BY i.concluded_at DESC LIMIT ?",
        (limit,),
    ).fetchall():
        recent_intents.append({
            "id": i["id"],
            "project": i["project_id"],
            "project_title": i["project_title"],
            "description": (i["description"] or "")[:300],
            "concluded_age_s": _age_seconds(i["concluded_at"], now),
        })
    return {"running": running, "recent_facts": recent_facts, "recent_intents": recent_intents}


def _timeline(conn, now: datetime, hours: int = 1, buckets: int = 30) -> dict:
    span = hours * 3600
    created = [0] * buckets
    concluded = [0] * buckets
    cutoff = now.timestamp() - span
    for ir in conn.execute("SELECT created_at, concluded_at FROM intents").fetchall():
        for value, series in ((ir["created_at"], created), (ir["concluded_at"], concluded)):
            dt = _parse_iso(value)
            if not dt:
                continue
            ts = dt.timestamp()
            if ts < cutoff:
                continue
            idx = min(buckets - 1, int((ts - cutoff) / span * buckets))
            if 0 <= idx < buckets:
                series[idx] += 1
    return {"hours": hours, "buckets": buckets, "created": created, "concluded": concluded}


@router.get("/dashboard")
def get_dashboard():
    now = datetime.now(timezone.utc)
    dispatcher = _find_dispatcher()
    log_path = _env_log_override() or dispatcher.get("log_path")
    with get_conn() as conn:
        settings, ceilings = _settings_and_ceilings(conn)
        projects, totals = _project_rows(conn, now)
        timeline = _timeline(conn, now)
        activity = _activity_feed(conn, now)
    timeouts = _parse_timeouts(log_path)
    snapshot, telemetry_at = read_latest_snapshot()
    telemetry_age = _age_seconds(telemetry_at, now)
    telemetry = {
        "snapshot": snapshot,
        "updated_at": telemetry_at,
        "age_s": telemetry_age,
        # the dispatcher pushes every tick (~3s); treat >20s as stale / dispatcher not reporting
        "stale": telemetry_age is None or telemetry_age > 20,
    }
    try:
        supervisor = supervisor_status()
    except Exception:
        supervisor = {"state": "absent", "present": False}
    return {
        "now": now.isoformat(),
        "dispatcher": dispatcher,
        "settings": settings,
        "ceilings": ceilings,
        "vitals": totals,
        "projects": projects,
        "timeline": timeline,
        "timeouts": timeouts,
        "telemetry": telemetry,
        "supervisor": supervisor,
        "activity": activity,
    }


_TMP_CLEANUP_DIRS = ["cairn-prompts"]


@router.post("/dashboard/cleanup-tmp")
def cleanup_tmp():
    """Remove known temp garbage directories from /tmp."""
    freed_mb = 0.0
    removed: list[str] = []
    errors: list[str] = []
    for name in _TMP_CLEANUP_DIRS:
        target = Path("/tmp") / name
        if not target.exists():
            continue
        try:
            size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
            shutil.rmtree(target)
            freed_mb += size / 1024**2
            removed.append(name)
        except OSError as exc:
            try:
                for f in target.rglob("*"):
                    if f.is_file():
                        try:
                            sz = f.stat().st_size
                            f.unlink()
                            freed_mb += sz / 1024**2
                        except OSError:
                            pass
                removed.append(f"{name} (partial)")
            except OSError:
                errors.append(f"{name}: {exc}")
    return {"freed_mb": round(freed_mb, 1), "removed": removed, "errors": errors}
