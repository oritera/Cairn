from __future__ import annotations

import pytest

from cairn.dispatcher.config import ResourceBudgetConfig
from cairn.safety.policy import classify_tool_call


@pytest.mark.parametrize(
    ("command", "rule_id"),
    [
        ("cat hosts.txt | xargs -P3 -n1 nmap -sV", "bulk_concurrency_limit"),
        ("cat hosts.txt | xargs --max-procs=3 -n1 nmap -sV", "bulk_concurrency_limit"),
        ("parallel -j 3 scan ::: host1 host2 host3", "bulk_concurrency_limit"),
        ("nuclei -l hosts.txt --concurrency=3", "bulk_concurrency_limit"),
        ("nuclei -l hosts.txt -c 3", "bulk_concurrency_limit"),
        ('for host in a b c; do scan "$host" & done; wait', "background_worker_fanout"),
        ("timeout 601 nmap -iL hosts.txt", "bulk_duration_limit"),
        ("hydra -l admin -p known -t 2 ssh://target", "auth_concurrency_limit"),
        ("hydra -L users.txt -p known ssh://target", "auth_batch_unbounded"),
        ("netexec smb target -u users.txt -p known", "auth_batch_unbounded"),
    ],
)
def test_explicit_over_budget_work_is_paused(command: str, rule_id: str) -> None:
    decision = classify_tool_call(
        "bash",
        {"command": command},
        cwd="/workspace",
        limits=ResourceBudgetConfig(),
    )

    assert decision.decision == "resource_pause"
    assert decision.rule_id == rule_id


@pytest.mark.parametrize(
    "command",
    [
        "cat hosts.txt | xargs -P2 -n1 nmap -sV",
        "parallel --jobs=2 scan ::: host1 host2",
        "nuclei -l hosts.txt --concurrency 2",
        "timeout 600 nmap -iL hosts.txt",
        "hydra -l admin -p known -t 1 ssh://target",
        "curl -u admin:known https://target.example/private",
        "for host in a b c; do scan \"$host\"; done",
    ],
)
def test_at_limit_or_ambiguous_work_remains_allowed(command: str) -> None:
    decision = classify_tool_call(
        "bash",
        {"command": command},
        cwd="/workspace",
        limits=ResourceBudgetConfig(),
    )

    assert decision.decision == "allow"


def test_more_than_thirty_explicit_auth_attempts_are_paused() -> None:
    command = "; ".join(
        f"hydra -l user{index} -p known ssh://target" for index in range(31)
    )

    decision = classify_tool_call(
        "bash",
        {"command": command},
        cwd="/workspace",
        limits=ResourceBudgetConfig(),
    )

    assert decision.decision == "resource_pause"
    assert decision.rule_id == "auth_batch_limit"
    assert decision.auth_attempt_count == 31


def test_one_known_credential_validation_reports_one_attempt() -> None:
    decision = classify_tool_call(
        "bash",
        {"command": "hydra -l admin -p known ssh://target"},
        cwd="/workspace",
        limits=ResourceBudgetConfig(),
    )

    assert decision.decision == "allow"
    assert decision.auth_attempt_count == 1
