from __future__ import annotations

from cairn.safety.v1 import audit_event_context
from cairn.server.models import AuditEvent


R1_MARKER = "[R1][RESOURCE_PAUSED]"


def is_r1_fact(description: str) -> bool:
    for line in description.splitlines():
        if line.strip():
            return line.strip() == R1_MARKER
    return False


def synthesize_r1_fact(intent_description: str, event: AuditEvent) -> str:
    details = audit_event_context(event)
    auth_count = details["resource"].get("auth_attempt_count", 0)
    return "\n".join(
        [
            R1_MARKER,
            f"Target: {details['target'] or '未从审计事件中确定'}",
            f"Pause reason: rule_id={event.rule_id or 'resource_budget'}; {event.reason or '资源预算已达到上限'}",
            f"Confirmed evidence: 当前 Intent={intent_description}；已记录的凭据验证尝试数={auth_count}。",
            "Low-resource validation: 保留本轮在限额内已经完成的审计证据；不推断未记录结果。",
            f"Larger action not executed: {event.tool_name or 'unknown'} {details['input_preview']}（未执行）",
            "Assessment: R1 不是漏洞确认，也不代表凭据有效或目标已被攻陷。",
            "Manual verification: 如确需扩大批量验证，由人工在隔离、明确授权且受监控的环境中分批执行，并设置独立的并发和速率上限。",
            "Autonomous expansion: prohibited for this Intent",
        ]
    )
