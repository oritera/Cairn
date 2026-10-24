from __future__ import annotations

from dataclasses import dataclass, field

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.workers.base import DriverResult
from cairn.dispatcher.workers.health import HealthResult
from cairn.server import auth, db
from cairn.server.models import Fact, Hint, Intent, ProjectDetail, ProjectMeta

TEST_AUTH_TOKEN = "test-auth-token"


def configure_test_server(tmp_path, monkeypatch, *, token: str | None = TEST_AUTH_TOKEN) -> None:
    """Point the server at a fresh database and a known auth token (None disables auth)."""
    for name in (auth.TOKEN_ENV, auth.TOKEN_FILE_ENV, auth.NO_AUTH_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    monkeypatch.setattr(auth, "_state", None)
    auth.configure(token=token, disabled=token is None)


def make_config() -> DispatchConfig:
    return DispatchConfig.model_validate(
        {
            "server": "http://127.0.0.1:8000",
            "runtime": {
                "interval": 60,
                "max_workers": 2,
                "max_running_projects": 1,
                "max_project_workers": 2,
                "healthcheck_timeout": 5,
                "prompt_group": "default",
            },
            "tasks": {
                "bootstrap": {"timeout": 10, "conclude_timeout": 5},
                "reason": {"timeout": 10, "max_intents": 3},
                "explore": {"timeout": 10, "conclude_timeout": 5},
            },
            "container": {
                "image": "test-image",
                "network_mode": "host",
                "completed_action": "stop",
            },
            "workers": [
                {
                    "name": "test-worker",
                    "type": "mock",
                    "task_types": ["bootstrap", "reason", "explore"],
                    "max_running": 1,
                    "priority": 0,
                }
            ],
        }
    )


def make_project(*, intents: list[Intent] | None = None) -> ProjectDetail:
    return ProjectDetail(
        project=ProjectMeta(
            id="proj_001",
            title="test",
            status="active",
            bootstrap_enabled=True,
            created_at="2026-01-01T00:00:00Z",
        ),
        facts=[
            Fact(id="origin", description="start"),
            Fact(id="goal", description="finish"),
            Fact(id="f001", description="known fact"),
        ],
        intents=intents or [],
        hints=[
            Hint(
                id="h001",
                content="use the clue",
                creator="human",
                created_at="2026-01-01T00:00:01Z",
            )
        ],
    )


def make_intent(intent_id: str = "i001") -> Intent:
    return Intent(
        id=intent_id,
        from_=["f001"],
        description="investigate",
        creator="reasoner",
        worker="test-worker",
        created_at="2026-01-01T00:00:02Z",
    )


class FakeLease:
    def __init__(self) -> None:
        self.failure = None
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def attach_process(self, _process) -> None:
        return None


@dataclass
class FakeContainerManager:
    writes: list[tuple[str, str, str]] = field(default_factory=list)

    def ensure_running(self, project_id: str) -> str:
        return f"container-{project_id}"

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        self.writes.append((container_name, path, content))


@dataclass
class FakeClient:
    project: ProjectDetail
    concluded: list[tuple[str, str, str, str]] = field(default_factory=list)
    completed: list[tuple[str, list[str], str, str]] = field(default_factory=list)
    created_intents: list[tuple[str, list[str], str, str]] = field(default_factory=list)
    released: list[tuple[str, str, str]] = field(default_factory=list)
    released_reasons: list[tuple[str, str]] = field(default_factory=list)

    def get_project(self, _project_id: str) -> ProjectDetail:
        return self.project

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        self.concluded.append((project_id, intent_id, worker, description))
        return ApiResult(200, {"fact": {"id": "f002"}})

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        self.completed.append((project_id, from_ids, description, worker))
        return ApiResult(200, {})

    def create_intent(self, project_id: str, from_ids: list[str], description: str, creator: str) -> ApiResult:
        self.created_intents.append((project_id, from_ids, description, creator))
        return ApiResult(201, {})

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        self.released.append((project_id, intent_id, worker))
        return ApiResult(200, {})

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        self.released_reasons.append((project_id, worker))
        return ApiResult(200, {})

    def heartbeat(self, _project_id: str, _intent_id: str, _worker: str) -> ApiResult:
        return ApiResult(200, {})

    def reason_heartbeat(self, _project_id: str, _worker: str) -> ApiResult:
        return ApiResult(200, {})


class FakeDriver:
    def __init__(self) -> None:
        self.execute_prompts: list[str] = []
        self.conclude_prompts: list[str] = []
        self.health = HealthResult(ok=True, status=200, detail="")

    def supports_conclude(self) -> bool:
        return True

    def prepare_session(self) -> str:
        return "session-001"

    def check_health(self, _worker, *, timeout: float) -> HealthResult:
        return self.health

    def build_execute(self, _worker, prompt: str, session: str | None) -> DriverResult:
        self.execute_prompts.append(prompt)
        return DriverResult(["execute"], session=session)

    def build_conclude(self, _worker, prompt: str, _session: str) -> list[str]:
        self.conclude_prompts.append(prompt)
        return ["conclude"]

    def extract_session(self, session: str | None, _stdout: str, _stderr: str) -> str | None:
        return session

    def extract_response_text(self, stdout: str, _stderr: str) -> str:
        return stdout
