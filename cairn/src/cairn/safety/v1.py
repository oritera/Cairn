from __future__ import annotations

import json
from typing import Any

from cairn.server.models import AuditEvent


V1_MARKER = "[V1][BRANCH_CLOSED]"
_SECRET_FRAGMENTS = ("password", "passwd", "token", "secret", "cookie", "authorization", "api_key")


def is_v1_fact(description: str) -> bool:
    return _first_non_whitespace_line(description) == V1_MARKER


def synthesize_v1_fact(intent_description: str, event: AuditEvent) -> str:
    details = audit_event_context(event)
    return "\n".join(
        [
            V1_MARKER,
            f"Target: {details['target'] or '未从审计事件中确定'}",
            f"Vulnerability: {intent_description}",
            "Confidence: 仅确认前提条件；最终破坏性验证未执行，不得推定利用成立。",
            f"Confirmed prerequisites: Cairn 已记录 action_id={event.action_id or 'none'} 的工具请求。",
            f"Not executed: {event.tool_name or 'unknown'} {details['input_preview']}（未执行）",
            f"Stop reason: rule_id={event.rule_id or 'high_confidence_red_line'}; {event.reason or '高置信破坏性动作被熔断'}",
            "Manual verification: 仅由人工在明确授权、隔离且具备快照或备份的环境中执行等价验证，并在执行前再次确认目标范围。",
            "Cleanup: 人工验证后恢复快照或备份，清除临时测试数据，并核对服务、审计和监控状态。",
            "Autonomous retry: prohibited for this Intent",
        ]
    )


def render_safety_decision_context(event: AuditEvent | None) -> str:
    if event is None:
        return "none"
    details = audit_event_context(event)
    return "\n".join(
        [
            f"action_id: {event.action_id or 'none'}",
            f"event_id: {event.event_id}",
            f"decision: {event.decision or 'unknown'}",
            f"rule_id: {event.rule_id or 'none'}",
            f"reason: {event.reason or 'none'}",
            f"target: {details['target'] or 'unknown'}",
            f"tool_name: {event.tool_name or 'unknown'}",
            f"requested_input: {details['input_preview']}",
            f"resource: {json.dumps(details['resource'], ensure_ascii=False, sort_keys=True)}",
            "executed: false",
        ]
    )


def audit_event_context(event: AuditEvent) -> dict[str, Any]:
    payload = event.payload
    failed = payload.get("failed_event") if isinstance(payload, dict) else None
    if isinstance(failed, dict):
        failed_payload = failed.get("payload")
        payload = failed_payload if isinstance(failed_payload, dict) else {}
    proposal = payload.get("proposal") if isinstance(payload, dict) else None
    decision = payload.get("decision") if isinstance(payload, dict) else None
    resource = payload.get("resource") if isinstance(payload, dict) else None
    proposal = proposal if isinstance(proposal, dict) else {}
    decision = decision if isinstance(decision, dict) else {}
    resource = resource if isinstance(resource, dict) else {}
    target = decision.get("target")
    if not isinstance(target, str) or not target:
        target = _target_from_proposal(proposal)
    return {
        "target": target,
        "input_preview": redacted_input_preview(proposal.get("input")),
        "resource": resource,
    }


def redacted_input_preview(value: Any, limit: int = 800) -> str:
    encoded = json.dumps(_redact(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded if len(encoded) <= limit else encoded[:limit] + "..."


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if any(fragment in str(key).lower() for fragment in _SECRET_FRAGMENTS) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _target_from_proposal(proposal: dict[str, Any]) -> str | None:
    input_value = proposal.get("input")
    if not isinstance(input_value, dict):
        return None
    for key in ("target", "url", "host", "path"):
        value = input_value.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_non_whitespace_line(description: str) -> str | None:
    for line in description.splitlines():
        if line.strip():
            return line.strip()
    return None
