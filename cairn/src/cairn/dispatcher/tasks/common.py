from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass

from cairn.dispatcher.config import DispatchConfig, SafetyConfig, WorkerConfig, resolve_safety_token
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.backend import ExecutionBackend
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.workers.base import DriverResult
from cairn.server.models import AuditEvent, AuditEventCreate

PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG_PREVIEW_LIMIT = 1200
GRAPH_SNAPSHOT_ROOT = "/tmp/cairn-prompts"
LOG = logging.getLogger(__name__)
SAFETY_FALLBACK_PREFIX = "CAIRN_SAFETY_FALLBACK "


@dataclass(slots=True)
class ConcludeWriteResult:
    status: str
    fact_id: str | None = None


@dataclass(frozen=True, slots=True)
class SafetyRunContext:
    run_id: str
    project_id: str
    intent_id: str | None
    worker: str
    phase: str


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def did_timeout(result: ProcessResult) -> bool:
    return not result.cancelled and (result.timed_out or result.returncode in (124, 137))


def parse_safety_fallbacks(stderr: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in stderr.splitlines():
        if not line.startswith(SAFETY_FALLBACK_PREFIX):
            continue
        try:
            record = json.loads(line[len(SAFETY_FALLBACK_PREFIX) :])
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("schema_version") != 1:
            continue
        failed_event = record.get("failed_event")
        transport_error = record.get("transport_error")
        if not isinstance(failed_event, dict) or not isinstance(transport_error, str) or not transport_error:
            continue
        original_event_id = failed_event.get("event_id")
        if not isinstance(original_event_id, str) or not original_event_id:
            continue
        candidate = {
            "schema_version": 1,
            "event_id": f"backfill:{original_event_id}",
            "action_id": failed_event.get("action_id"),
            "run_id": failed_event.get("run_id"),
            "project_id": failed_event.get("project_id"),
            "intent_id": failed_event.get("intent_id"),
            "worker": failed_event.get("worker"),
            "phase": failed_event.get("phase"),
            "event_type": "AUDIT_BACKFILL",
            "tool_name": failed_event.get("tool_name"),
            "decision": failed_event.get("decision"),
            "rule_id": failed_event.get("rule_id"),
            "reason": failed_event.get("reason"),
            "payload": {
                "original_event_id": original_event_id,
                "failed_event": failed_event,
                "transport_error": transport_error,
            },
        }
        try:
            validated = AuditEventCreate.model_validate(candidate)
        except ValueError:
            continue
        events.append(validated.model_dump(mode="json"))
    return events


def backfill_safety_fallbacks(
    client: CairnClient,
    stderr: str,
    safety: SafetyConfig | None,
) -> int:
    events = parse_safety_fallbacks(stderr)
    if not events or safety is None:
        return 0
    try:
        token = resolve_safety_token(safety)
    except ValueError as exc:
        LOG.error("cannot backfill safety events because the shared token is unavailable: %s", exc)
        return 0
    written = 0
    for event in events:
        response = client.backfill_audit_event(event, token)
        if response.ok:
            written += 1
            continue
        LOG.warning(
            "safety audit backfill failed event=%s project=%s status=%s body=%s",
            event.get("event_id"),
            event.get("project_id"),
            response.status_code,
            response.text,
        )
    return written


def latest_blocked_action(
    client: CairnClient,
    project_id: str,
    run_id: str,
) -> AuditEvent | None:
    events: list[AuditEvent] = []
    for event_type in ("ACTION_DECISION", "AUDIT_BACKFILL"):
        events.extend(
            client.list_audit_events(
                project_id,
                run_id=run_id,
                event_type=event_type,
                limit=100,
            )
        )
    limited = [event for event in events if event.decision in ("block", "resource_pause")]
    if not limited:
        return None
    return max(limited, key=lambda event: (event.created_at, event.event_id))


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def task_healthcheck_enabled(config: DispatchConfig) -> bool:
    if config.runtime.execution == "local":
        return False
    return config.runtime.worker_healthcheck == "startup_and_task"


def write_graph_snapshot_reference(
    container_manager: ContainerManager,
    container_name: str,
    graph_yaml: str,
    *,
    phase: str,
) -> str:
    path = f"{GRAPH_SNAPSHOT_ROOT}/{phase}-{uuid.uuid4().hex[:12]}/graph.yaml"
    container_manager.write_text_file(container_name, path, graph_yaml)
    return (
        "The graph YAML snapshot is stored in this file inside the current container:\n\n"
        f"{path}\n\n"
        "Before using the graph, read the entire file and treat its contents as the YAML snapshot "
        "for this Graph section."
    )


def run_worker_process(
    container_manager: ExecutionBackend,
    container_name: str,
    worker: WorkerConfig,
    command: DriverResult,
    *,
    phase: str,
    timeout_seconds: int,
    safety: SafetyConfig | None = None,
    safety_context: SafetyRunContext | None = None,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
) -> ProcessResult:
    LOG.info(
        "starting container exec container=%s worker=%s phase=%s timeout=%ss",
        container_name,
        worker.name,
        phase,
        timeout_seconds,
    )
    for asset in command.assets:
        container_manager.write_text_file(container_name, asset.path, asset.content)

    exec_env = dict(worker.env)
    if safety is not None and safety_context is not None:
        if safety_context.worker != worker.name:
            raise ValueError("safety context worker does not match worker config")
        if safety_context.phase != phase:
            raise ValueError("safety context phase does not match execution phase")
        exec_env.update(
            {
                "CAIRN_SAFETY_ENDPOINT": safety.endpoint.rstrip("/"),
                "CAIRN_SAFETY_TOKEN": resolve_safety_token(safety),
                "CAIRN_PROJECT_ID": safety_context.project_id,
                "CAIRN_INTENT_ID": safety_context.intent_id or "",
                "CAIRN_RUN_ID": safety_context.run_id,
                "CAIRN_WORKER": safety_context.worker,
                "CAIRN_PHASE": safety_context.phase,
                "CAIRN_SAFETY_TIMEOUT_MS": str(safety.request_timeout_ms),
                "CAIRN_SAFETY_MAX_PAYLOAD_BYTES": str(safety.max_payload_bytes),
                "CAIRN_SAFETY_MAX_BULK_CONCURRENCY": str(
                    safety.resource_budget.max_bulk_concurrency
                ),
                "CAIRN_SAFETY_MAX_UNATTENDED_BULK_SECONDS": str(
                    safety.resource_budget.max_unattended_bulk_seconds
                ),
                "CAIRN_SAFETY_AUTH_CONCURRENCY": str(safety.resource_budget.auth_concurrency),
                "CAIRN_SAFETY_AUTH_ATTEMPTS_PER_MINUTE": str(
                    safety.resource_budget.auth_attempts_per_minute
                ),
                "CAIRN_SAFETY_AUTH_ATTEMPTS_PER_BATCH": str(
                    safety.resource_budget.auth_attempts_per_batch
                ),
            }
        )

    process = container_manager.build_exec_process(
        container_name,
        exec_env,
        command.argv,
        timeout_seconds=timeout_seconds,
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    try:
        return process.communicate(timeout=communicate_timeout(timeout_seconds))
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)


def project_allows_conclude_fallback(client: CairnClient, project_id: str, *, worker_name: str, intent_id: str) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s intent=%s worker=%s status=%s",
        project_id,
        intent_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_reason(client: CairnClient, project_id: str, worker_name: str) -> None:
    response = client.release_reason(project_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "reason release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released reason project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "reason release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def write_conclude_result(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> str:
    return write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
    ).status


def write_conclude_result_with_fact_id(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> ConcludeWriteResult:
    response = client.conclude(project_id, intent_id, worker_name, description)
    if response.ok:
        fact_id: str | None = None
        if isinstance(response.data, dict):
            fact = response.data.get("fact")
            if isinstance(fact, dict):
                candidate = fact.get("id")
                if isinstance(candidate, str) and candidate:
                    fact_id = candidate
        if total_ms is None:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
            )
        else:
            LOG.info(
                "intent concluded project=%s intent=%s worker=%s source=%s phase_ms=%s total_ms=%s",
                project_id,
                intent_id,
                worker_name,
                source,
                phase_ms,
                total_ms,
            )
        return ConcludeWriteResult(status="success", fact_id=fact_id)
    if response.status_code == 403:
        LOG.info(
            "project became inactive during conclude project=%s intent=%s worker=%s",
            project_id,
            intent_id,
            worker_name,
        )
    else:
        LOG.warning(
            "conclude write failed project=%s intent=%s worker=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, intent_id, worker_name)
    return ConcludeWriteResult(status="failed", fact_id=None)


def best_effort_release(client: CairnClient, project_id: str, intent_id: str, worker_name: str) -> None:
    response = client.release(project_id, intent_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released intent project=%s intent=%s worker=%s", project_id, intent_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s intent=%s worker=%s status=%s",
            project_id,
            intent_id,
            worker_name,
            response.status_code,
        )
