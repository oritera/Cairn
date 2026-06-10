from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)


class Fact(BaseModel):
    id: str
    description: str


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    hint_type: Literal["strategy", "evidence", "correction", "note"] = "note"
    priority: Literal["low", "normal", "high"] = "normal"
    target_type: Literal["project", "fact", "intent"] | None = None
    target_id: str | None = None
    pinned: bool = False
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    bootstrap_enabled: bool
    created_at: str
    reason: ProjectReason | None = None


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]


class ProjectDiagnostics(BaseModel):
    project_id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    severity: Literal["idle", "running", "attention", "blocked", "completed", "stopped"]
    message: str
    next_action: str
    fact_count: int
    hint_count: int
    intent_count: int
    open_intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    concluded_intent_count: int
    reason: ProjectReason | None = None


class OpsSummaryProject(BaseModel):
    project_id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    severity: Literal["idle", "running", "attention", "blocked", "completed", "stopped"]
    message: str
    next_action: str
    fact_count: int
    hint_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    reason: ProjectReason | None = None


class OpsSummary(BaseModel):
    project_count: int
    active_count: int
    stopped_count: int
    completed_count: int
    running_count: int
    attention_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    reason_count: int
    projects: list[OpsSummaryProject]


class OpsEvent(BaseModel):
    id: int
    project_id: str | None = None
    event_type: str
    task_type: str | None = None
    worker: str | None = None
    intent_id: str | None = None
    severity: Literal["debug", "info", "warning", "error"]
    message: str
    details: dict[str, Any] | None = None
    created_at: str


class CreateOpsEventRequest(BaseModel):
    project_id: str | None = None
    event_type: str
    task_type: str | None = None
    worker: str | None = None
    intent_id: str | None = None
    severity: Literal["debug", "info", "warning", "error"] = "info"
    message: str
    details: dict[str, Any] | None = None

    @field_validator("project_id", "event_type", "task_type", "worker", "intent_id", "message")
    @classmethod
    def validate_optional_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintInline(BaseModel):
    content: str
    creator: str
    hint_type: Literal["strategy", "evidence", "correction", "note"] = "note"
    priority: Literal["low", "normal", "high"] = "normal"
    target_type: Literal["project", "fact", "intent"] | None = None
    target_id: str | None = None
    pinned: bool = False

    @field_validator("content", "creator", "target_id")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    bootstrap_enabled: bool = True
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str
    hint_type: Literal["strategy", "evidence", "correction", "note"] = "note"
    priority: Literal["low", "normal", "high"] = "normal"
    target_type: Literal["project", "fact", "intent"] | None = None
    target_id: str | None = None
    pinned: bool = False

    @field_validator("content", "creator", "target_id")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str
    description: str

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent
