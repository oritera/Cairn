from __future__ import annotations

import importlib

from cairn.server.models import AuditEvent


def _blocked_event() -> AuditEvent:
    return AuditEvent.model_validate(
        {
            "event_id": "decision-1",
            "action_id": "action-1",
            "run_id": "run-1",
            "project_id": "proj_001",
            "intent_id": "i001",
            "worker": "test-worker",
            "phase": "explore_execute",
            "event_type": "ACTION_DECISION",
            "tool_name": "bash",
            "decision": "block",
            "rule_id": "destructive_delete",
            "reason": "recursive deletion of protected data is prohibited",
            "payload": {
                "proposal": {
                    "tool_name": "bash",
                    "input": {"command": "rm -rf /srv/data", "token": "must-not-leak"},
                    "cwd": "/workspace",
                },
                "decision": {"target": "/srv/data"},
                "resource": {"auth_attempt_count": 0},
            },
            "payload_sha256": "a" * 64,
            "truncated": False,
            "created_at": "2026-08-25T01:02:03Z",
        }
    )


def test_v1_contract_requires_an_exact_first_non_whitespace_marker() -> None:
    v1 = importlib.import_module("cairn.safety.v1")

    assert v1.is_v1_fact("\n [V1][BRANCH_CLOSED]\nTarget: host")
    assert not v1.is_v1_fact("prefix [V1][BRANCH_CLOSED]\nTarget: host")
    assert not v1.is_v1_fact("[R1][RESOURCE_PAUSED]\nTarget: host")


def test_synthesized_v1_is_conservative_redacted_and_manually_verifiable() -> None:
    v1 = importlib.import_module("cairn.safety.v1")

    description = v1.synthesize_v1_fact("validate destructive prerequisite", _blocked_event())

    assert description.startswith("[V1][BRANCH_CLOSED]\n")
    for label in (
        "Target:",
        "Vulnerability:",
        "Confidence:",
        "Confirmed prerequisites:",
        "Not executed:",
        "Stop reason:",
        "Manual verification:",
        "Cleanup:",
        "Autonomous retry: prohibited for this Intent",
    ):
        assert label in description
    assert "must-not-leak" not in description
    assert "未执行" in description
    assert "已成功利用" not in description
