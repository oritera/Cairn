from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.startup_healthcheck import (
    StartupHealthcheckResult,
    format_failure_summary,
)
from cairn.dispatcher.scheduler.loop import (
    SERVER_UNAUTHORIZED_MESSAGE,
    DispatcherLoop,
    server_rejected_credentials,
)

from conftest import make_config


def test_client_sends_bearer_token_only_when_configured() -> None:
    assert "Authorization" not in CairnClient("http://server/")._session().headers
    assert "Authorization" not in CairnClient("http://server/", token="")._session().headers
    session = CairnClient("http://server/", token="secret")._session()
    assert session.headers["Authorization"] == "Bearer secret"


def test_server_rejected_credentials_only_matches_http_401() -> None:
    unauthorized = requests.Response()
    unauthorized.status_code = 401
    forbidden = requests.Response()
    forbidden.status_code = 403

    assert server_rejected_credentials(requests.HTTPError(response=unauthorized))
    assert not server_rejected_credentials(requests.HTTPError(response=forbidden))
    assert not server_rejected_credentials(requests.ConnectionError("offline"))
    assert "server_token" in SERVER_UNAUTHORIZED_MESSAGE


def test_dispatcher_loop_stops_instead_of_retrying_when_server_rejects_token() -> None:
    unauthorized = requests.Response()
    unauthorized.status_code = 401

    class Client:
        def get_settings(self):
            raise requests.HTTPError("401 Client Error: Unauthorized", response=unauthorized)

        def close(self) -> None:
            return None

    class Containers:
        def close(self) -> None:
            return None

    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = make_config()
    loop.client = Client()
    loop.container_manager = Containers()
    loop.executor = ThreadPoolExecutor(max_workers=1)
    loop.cleanup_executor = ThreadPoolExecutor(max_workers=1)
    loop.futures = {}
    loop._settings_checked = False
    loop._startup_healthchecks_checked = True

    with pytest.raises(RuntimeError, match="HTTP 401"):
        loop.run(once=False)


def test_client_request_failure_returns_status_zero() -> None:
    class Session:
        def request(self, *_args, **_kwargs):
            raise requests.ConnectionError("offline")

    client = CairnClient("http://server/")
    client._local.session = Session()

    result = client.create_intent("proj_001", ["f001"], "investigate", "reasoner")

    assert result.status_code == 0
    assert result.text == "offline"


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
