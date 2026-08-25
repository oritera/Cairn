from __future__ import annotations

import requests

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.startup_healthcheck import (
    StartupHealthcheckResult,
    format_failure_summary,
)


class _JsonResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_client_request_failure_returns_status_zero() -> None:
    class Session:
        def request(self, *_args, **_kwargs):
            raise requests.ConnectionError("offline")

    client = CairnClient("http://server/")
    client._local.session = Session()

    result = client.create_intent("proj_001", ["f001"], "investigate", "reasoner")

    assert result.status_code == 0
    assert result.text == "offline"


def test_client_backfill_forwards_the_safety_token_header() -> None:
    calls = []

    class Session:
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))
            return _JsonResponse(201, {"event_id": "backfill:event-1"})

    client = CairnClient("http://server/")
    client._local.session = Session()
    payload = {"event_id": "backfill:event-1", "event_type": "AUDIT_BACKFILL"}

    result = client.backfill_audit_event(payload, "shared-secret")

    assert result.ok
    assert calls == [
        (
            ("POST", "http://server/internal/safety/events"),
            {
                "json": payload,
                "headers": {"X-Cairn-Safety-Token": "shared-secret"},
                "timeout": 10.0,
            },
        )
    ]


def test_client_lists_audit_events_with_only_requested_filters() -> None:
    calls = []
    item = {
        "event_id": "event-1",
        "action_id": "action-1",
        "run_id": "run-1",
        "project_id": "proj_001",
        "intent_id": "i001",
        "worker": "worker-a",
        "phase": "explore_execute",
        "event_type": "ACTION_DECISION",
        "tool_name": "bash",
        "decision": "block",
        "rule_id": "destructive_delete",
        "reason": "blocked",
        "payload": {"proposal": {"input": {"command": "rm -rf /srv/data"}}},
        "payload_sha256": "a" * 64,
        "truncated": False,
        "created_at": "2026-08-25T01:02:03Z",
    }

    class Session:
        def get(self, *args, **kwargs):
            calls.append((args, kwargs))
            return _JsonResponse(200, {"items": [item], "next": None})

    client = CairnClient("http://server/")
    client._local.session = Session()

    events = client.list_audit_events(
        "proj_001",
        intent_id="i001",
        run_id="run-1",
        decision="block",
        limit=25,
    )

    assert [event.event_id for event in events] == ["event-1"]
    assert calls == [
        (
            ("http://server/projects/proj_001/audit",),
            {
                "params": {
                    "intent_id": "i001",
                    "run_id": "run-1",
                    "decision": "block",
                    "limit": 25,
                },
                "timeout": 10.0,
            },
        )
    ]


def test_startup_healthcheck_failure_summary_includes_worker_details() -> None:
    results = [
        StartupHealthcheckResult(
            worker_name="worker-a",
            ok=False,
            status=401,
            duration_ms=12,
            detail="unauthorized",
            endpoint="POST http://api/v1/messages",
        ),
        StartupHealthcheckResult(
            worker_name="worker-b",
            ok=True,
            status=200,
            duration_ms=8,
            detail="",
            endpoint="POST http://api/v1/messages",
        ),
    ]

    summary = format_failure_summary(results)

    assert summary == (
        "startup healthchecks failed for all workers: worker-a(http=401, detail=unauthorized)"
    )
