from __future__ import annotations

import importlib

from cairn.server.models import AuditEvent


def _resource_event() -> AuditEvent:
    return AuditEvent.model_validate(
        {
            "event_id": "decision-2",
            "action_id": "action-2",
            "run_id": "run-2",
            "project_id": "proj_001",
            "intent_id": "i001",
            "worker": "test-worker",
            "phase": "explore_execute",
            "event_type": "ACTION_DECISION",
            "tool_name": "bash",
            "decision": "resource_pause",
            "rule_id": "auth_batch_limit",
            "reason": "credential batch contains 64 attempts; limit is 30",
            "payload": {
                "proposal": {
                    "tool_name": "bash",
                    "input": {"command": "hydra -L users -P passwords ssh://target"},
                    "cwd": "/workspace",
                },
                "decision": {"target": "ssh://target"},
                "resource": {"auth_attempt_count": 64},
            },
            "payload_sha256": "b" * 64,
            "truncated": False,
            "created_at": "2026-08-25T01:02:04Z",
        }
    )


def test_r1_contract_requires_an_exact_first_non_whitespace_marker() -> None:
    r1 = importlib.import_module("cairn.safety.r1")

    assert r1.is_r1_fact("\n[R1][RESOURCE_PAUSED]\nTarget: ssh://target")
    assert not r1.is_r1_fact("prefix [R1][RESOURCE_PAUSED]")
    assert not r1.is_r1_fact("[V1][BRANCH_CLOSED]")


def test_synthesized_r1_records_counts_without_claiming_a_vulnerability() -> None:
    r1 = importlib.import_module("cairn.safety.r1")

    description = r1.synthesize_r1_fact("validate supplied credentials", _resource_event())

    assert description.startswith("[R1][RESOURCE_PAUSED]\n")
    assert "64" in description
    assert "30" in description
    assert "不是漏洞确认" in description
    assert "未执行" in description
    assert "Autonomous expansion: prohibited for this Intent" in description
