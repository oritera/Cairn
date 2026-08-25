from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Query

from cairn.dispatcher.config import ResourceBudgetConfig
from cairn.safety.policy import SafetyDecision, classify_tool_call
from cairn.server.audit_store import (
    AuditEventCollisionError,
    AuditProjectNotFoundError,
    append_audit_event,
    list_audit_events,
)
from cairn.server.db import get_conn
from cairn.server.models import (
    AuditDecision,
    AuditEvent,
    AuditEventCreate,
    AuditEventPage,
    AuditEventType,
    SafetyPreflightRequest,
    SafetyPreflightResponse,
)

router = APIRouter(tags=["audit"])


def _require_safety_token(provided: str | None) -> None:
    expected = os.environ.get("CAIRN_SAFETY_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="safety token is not configured")
    if provided is None or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid safety token")


@router.post("/internal/safety/events", response_model=AuditEvent, status_code=201)
def create_audit_event(
    body: AuditEventCreate,
    x_cairn_safety_token: str | None = Header(default=None),
):
    _require_safety_token(x_cairn_safety_token)
    try:
        with get_conn() as conn:
            return append_audit_event(conn, body, max_payload_bytes=_max_payload_bytes())
    except AuditEventCollisionError as exc:
        raise HTTPException(status_code=409, detail="event_id collision") from exc
    except AuditProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc


@router.post(
    "/internal/safety/preflight",
    response_model=SafetyPreflightResponse,
    status_code=201,
)
def safety_preflight(
    body: SafetyPreflightRequest,
    x_cairn_safety_token: str | None = Header(default=None),
):
    _require_safety_token(x_cairn_safety_token)
    limits = _effective_limits(_resource_limits(), body.resource_budget)
    try:
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute(
                "SELECT 1 FROM projects WHERE id = ?",
                (body.project_id,),
            ).fetchone()
            if project is None:
                raise HTTPException(status_code=404, detail="project not found")
            if body.intent_id is not None:
                intent = conn.execute(
                    "SELECT 1 FROM intents WHERE id = ? AND project_id = ?",
                    (body.intent_id, body.project_id),
                ).fetchone()
                if intent is None:
                    raise HTTPException(status_code=404, detail="intent not found in project")

            classified = classify_tool_call(
                body.tool_name,
                body.input,
                cwd=body.cwd,
                limits=limits,
            )
            existing = conn.execute(
                "SELECT decision, rule_id, reason FROM audit_events WHERE event_id = ?",
                (body.event_id,),
            ).fetchone()
            if existing is None:
                decision = _apply_rolling_auth_budget(
                    conn,
                    body.project_id,
                    classified,
                    limits=limits,
                )
            else:
                decision = SafetyDecision(
                    decision=existing["decision"],
                    rule_id=existing["rule_id"],
                    reason=existing["reason"],
                    target=classified.target,
                    auth_attempt_count=classified.auth_attempt_count,
                )

            event = append_audit_event(
                conn,
                AuditEventCreate(
                    schema_version=1,
                    event_id=body.event_id,
                    action_id=body.action_id,
                    run_id=body.run_id,
                    project_id=body.project_id,
                    intent_id=body.intent_id,
                    worker=body.worker,
                    phase=body.phase,
                    event_type="ACTION_DECISION",
                    tool_name=body.tool_name,
                    decision=decision.decision,
                    rule_id=decision.rule_id,
                    reason=decision.reason,
                    payload={
                        "proposal": {
                            "tool_name": body.tool_name,
                            "input": body.input,
                            "cwd": body.cwd,
                            "resource_budget": (
                                body.resource_budget.model_dump() if body.resource_budget is not None else None
                            ),
                        },
                        "decision": {
                            "target": decision.target,
                        },
                        "resource": {
                            "auth_attempt_count": decision.auth_attempt_count,
                        },
                    },
                ),
                max_payload_bytes=_max_payload_bytes(),
            )
            return SafetyPreflightResponse(
                event_id=event.event_id,
                action_id=body.action_id,
                decision=decision.decision,
                rule_id=decision.rule_id,
                reason=decision.reason,
                target=decision.target,
                auth_attempt_count=decision.auth_attempt_count,
            )
    except AuditEventCollisionError as exc:
        raise HTTPException(status_code=409, detail="event_id collision") from exc


@router.get("/projects/{project_id}/audit", response_model=AuditEventPage)
def get_project_audit(
    project_id: str,
    intent_id: str | None = None,
    run_id: str | None = None,
    event_type: AuditEventType | None = None,
    decision: AuditDecision | None = None,
    tool_name: str | None = None,
    after: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    with get_conn() as conn:
        project = conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            items, next_cursor = list_audit_events(
                conn,
                project_id,
                intent_id=intent_id,
                run_id=run_id,
                event_type=event_type,
                decision=decision,
                tool_name=tool_name,
                after=after,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return AuditEventPage(items=items, next=next_cursor)


def _max_payload_bytes() -> int:
    raw = os.environ.get("CAIRN_SAFETY_MAX_PAYLOAD_BYTES", "65536")
    try:
        return max(4096, min(int(raw), 1048576))
    except ValueError:
        return 65536


def _apply_rolling_auth_budget(
    conn,
    project_id: str,
    decision: SafetyDecision,
    *,
    limits: ResourceBudgetConfig,
) -> SafetyDecision:
    if decision.decision != "allow" or decision.auth_attempt_count <= 0:
        return decision
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """
        SELECT payload_json
        FROM audit_events
        WHERE project_id = ?
          AND event_type = 'ACTION_DECISION'
          AND decision = 'allow'
          AND created_at >= ?
        """,
        (project_id, cutoff),
    ).fetchall()
    previous_attempts = 0
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
            previous_attempts += int(payload.get("resource", {}).get("auth_attempt_count", 0))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            continue
    projected = previous_attempts + decision.auth_attempt_count
    if projected <= limits.auth_attempts_per_minute:
        return decision
    return SafetyDecision(
        "resource_pause",
        "auth_rate_limit",
        f"rolling authentication attempts {projected} exceed {limits.auth_attempts_per_minute} per minute",
        target=decision.target,
        auth_attempt_count=decision.auth_attempt_count,
    )


def _resource_limits() -> ResourceBudgetConfig:
    defaults = ResourceBudgetConfig()
    return ResourceBudgetConfig(
        max_bulk_concurrency=_bounded_env(
            "CAIRN_SAFETY_MAX_BULK_CONCURRENCY",
            defaults.max_bulk_concurrency,
            lower=1,
            upper=2,
        ),
        max_unattended_bulk_seconds=_bounded_env(
            "CAIRN_SAFETY_MAX_UNATTENDED_BULK_SECONDS",
            defaults.max_unattended_bulk_seconds,
            lower=30,
            upper=600,
        ),
        auth_concurrency=_bounded_env(
            "CAIRN_SAFETY_AUTH_CONCURRENCY",
            defaults.auth_concurrency,
            lower=1,
            upper=1,
        ),
        auth_attempts_per_minute=_bounded_env(
            "CAIRN_SAFETY_AUTH_ATTEMPTS_PER_MINUTE",
            defaults.auth_attempts_per_minute,
            lower=1,
            upper=10,
        ),
        auth_attempts_per_batch=_bounded_env(
            "CAIRN_SAFETY_AUTH_ATTEMPTS_PER_BATCH",
            defaults.auth_attempts_per_batch,
            lower=1,
            upper=30,
        ),
    )


def _effective_limits(
    server: ResourceBudgetConfig,
    requested: ResourceBudgetConfig | None,
) -> ResourceBudgetConfig:
    if requested is None:
        return server
    return ResourceBudgetConfig(
        max_bulk_concurrency=min(server.max_bulk_concurrency, requested.max_bulk_concurrency),
        max_unattended_bulk_seconds=min(
            server.max_unattended_bulk_seconds,
            requested.max_unattended_bulk_seconds,
        ),
        auth_concurrency=min(server.auth_concurrency, requested.auth_concurrency),
        auth_attempts_per_minute=min(
            server.auth_attempts_per_minute,
            requested.auth_attempts_per_minute,
        ),
        auth_attempts_per_batch=min(
            server.auth_attempts_per_batch,
            requested.auth_attempts_per_batch,
        ),
    )


def _bounded_env(name: str, default: int, *, lower: int, upper: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(lower, min(value, upper))
