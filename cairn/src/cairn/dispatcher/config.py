from __future__ import annotations

from importlib import resources
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TaskType = Literal["reason", "explore", "bootstrap"]
WorkerType = Literal["pi"]
CompletedAction = Literal["remove", "stop"]
WorkerHealthcheckMode = Literal["startup_and_task", "startup_only", "disabled"]
ExecutionMode = Literal["container", "local"]
LocalCompletedAction = Literal["keep", "remove"]

RESERVED_RUNTIME_ENV_KEYS = frozenset(
    {
        "CAIRN_PROJECT_ID",
        "CAIRN_INTENT_ID",
        "CAIRN_RUN_ID",
        "CAIRN_WORKER",
        "CAIRN_PHASE",
    }
)

WORKER_ENV_KEYS: dict[WorkerType, tuple[str, ...]] = {
    "pi": (
        "PI_MODEL",
        "PI_BASE_URL",
        "PI_API_KEY",
        "PI_PROVIDER_API",
    ),
}

DEFAULT_PROMPT_REQUIRED_TOKENS: dict[str, tuple[str, ...]] = {
    "reason.md": ("{graph_yaml}", "{fact_ids}", "{open_intents}", "{max_intents}"),
    "explore.md": ("{graph_yaml}", "{intent_id}", "{intent_description}"),
    "explore_conclude.md": ("{graph_yaml}", "{intent_id}", "{intent_description}", "{safety_decision_context}"),
    "bootstrap.md": ("{origin}", "{goal}", "{hints}"),
    "bootstrap_conclude.md": ("{origin}", "{goal}", "{hints}", "{safety_decision_context}"),
}

PROMPT_REQUIRED_TOKENS_BY_GROUP: dict[str, dict[str, tuple[str, ...]]] = {
    "mock": {
        "reason.md": ("{fact_ids}", "{open_intents}", "{max_intents}"),
        "explore.md": ("{intent_id}",),
        "explore_conclude.md": ("{intent_id}",),
        "bootstrap.md": ("{origin}", "{goal}", "{hints}"),
        "bootstrap_conclude.md": ("{origin}", "{goal}", "{hints}"),
    }
}

class ReasonTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    max_intents: int = Field(gt=0, default=3)


class ExploreTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class BootstrapTaskConfig(BaseModel):
    timeout: int = Field(gt=0)
    conclude_timeout: int = Field(gt=0)


class TasksConfig(BaseModel):
    bootstrap: BootstrapTaskConfig
    reason: ReasonTaskConfig
    explore: ExploreTaskConfig


class ContainerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str
    network_mode: str
    completed_action: CompletedAction
    cap_add: list[str] = Field(default_factory=list)

    # --- hardening -----------------------------------------------------------------
    # "host" hands every worker the dispatcher host's network stack, which defeats the
    # egress boundary the container is supposed to be. It needs an explicit opt-in so a
    # stale config cannot quietly lose the boundary.
    allow_host_network: bool = False
    # Read-only rootfs. Pair it with writable tmpfs/volumes for every path the agent writes
    # (at minimum /tmp and the workspace), otherwise evidence capture breaks.
    read_only: bool = False
    tmpfs: dict[str, str] = Field(default_factory=dict)
    volumes: list[str] = Field(default_factory=list)
    # NOTE: "no-new-privileges:true" blocks setuid and therefore breaks the worker image's
    # passwordless sudo. Leave empty unless the image stops needing sudo at runtime.
    security_opt: list[str] = Field(default_factory=list)
    pids_limit: int | None = Field(default=None, gt=0)
    mem_limit: str | None = None
    nano_cpus: int | None = Field(default=None, gt=0)
    dns: list[str] = Field(default_factory=list)
    user: str | None = None

    @model_validator(mode="after")
    def validate_host_network(self) -> "ContainerConfig":
        if self.network_mode == "host" and not self.allow_host_network:
            raise ValueError(
                "container.network_mode 'host' shares the dispatcher host network stack and "
                "bypasses the egress boundary; set container.allow_host_network: true to accept it"
            )
        return self


class LocalConfig(BaseModel):
    workspace_root: str | None = None
    completed_action: LocalCompletedAction = "keep"


class RuntimeConfig(BaseModel):
    max_workers: int = Field(gt=0)
    max_running_projects: int = Field(gt=0)
    max_project_workers: int = Field(gt=0)
    interval: int = Field(gt=0)
    healthcheck_timeout: int = Field(gt=0)
    worker_healthcheck: WorkerHealthcheckMode = "startup_only"
    execution: ExecutionMode = "container"
    prompt_group: str = Field(min_length=1)


class ResourceBudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_bulk_concurrency: int = Field(default=2, ge=1, le=2)
    max_unattended_bulk_seconds: int = Field(default=600, ge=30, le=600)
    auth_concurrency: int = Field(default=1, ge=1, le=1)
    auth_attempts_per_minute: int = Field(default=10, ge=1, le=10)
    auth_attempts_per_batch: int = Field(default=30, ge=1, le=30)


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    endpoint: str = Field(min_length=1)
    token_env: str = Field(default="CAIRN_SAFETY_TOKEN", min_length=1)
    request_timeout_ms: int = Field(default=2000, ge=100, le=10000)
    max_payload_bytes: int = Field(default=65536, ge=4096, le=1048576)
    resource_budget: ResourceBudgetConfig = Field(default_factory=ResourceBudgetConfig)


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: WorkerType
    task_types: list[TaskType]
    max_running: int = Field(gt=0)
    priority: int = Field(ge=0)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("task_types")
    @classmethod
    def validate_task_types(cls, value: list[TaskType]) -> list[TaskType]:
        if not value:
            raise ValueError("task_types must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("task_types must be unique")
        return value

    @model_validator(mode="after")
    def validate_env(self) -> "WorkerConfig":
        # Required LLM env keys (base_url / key / model) are enforced per execution mode by
        # DispatchConfig: container mode needs them, local mode reuses the host CLI config.
        # The checks below are mode-independent and always apply.
        if self.type == "pi":
            _validate_optional_positive_int_env(self.name, self.env, "PI_MODEL_CONTEXT_WINDOW")
        return self


class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: str
    runtime: RuntimeConfig
    tasks: TasksConfig
    container: ContainerConfig | None = None
    local: LocalConfig | None = None
    safety: SafetyConfig | None = None
    common_env: dict[str, str] = Field(default_factory=dict)
    workers: list[WorkerConfig]

    @model_validator(mode="before")
    @classmethod
    def merge_common_env(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        common_env = data.get("common_env")
        if common_env is None:
            common_env = {}
        workers = data.get("workers")
        if not isinstance(common_env, dict) or not isinstance(workers, list):
            return data

        reserved = {key for key in common_env if _is_reserved_safety_env(key)}
        for worker in workers:
            if not isinstance(worker, dict):
                continue
            worker_env = worker.get("env")
            if isinstance(worker_env, dict):
                reserved.update(key for key in worker_env if _is_reserved_safety_env(key))
        if reserved:
            raise ValueError(f"reserved safety env keys are managed by Cairn: {', '.join(sorted(reserved))}")

        merged = dict(data)
        merged_workers: list[Any] = []
        for worker in workers:
            if not isinstance(worker, dict):
                merged_workers.append(worker)
                continue
            worker_env = worker.get("env")
            if worker_env is None:
                worker_env = {}
            if not isinstance(worker_env, dict):
                merged_workers.append(worker)
                continue
            worker_copy = dict(worker)
            worker_copy["env"] = {**common_env, **worker_env}
            merged_workers.append(worker_copy)
        merged["workers"] = merged_workers
        return merged

    @model_validator(mode="after")
    def validate_workers(self) -> "DispatchConfig":
        names = [worker.name for worker in self.workers]
        if len(set(names)) != len(names):
            raise ValueError("worker names must be unique")
        if not self.workers:
            raise ValueError("workers must not be empty")
        if self.runtime.max_project_workers > self.runtime.max_workers:
            raise ValueError("max_project_workers cannot exceed max_workers")
        return self

    @model_validator(mode="after")
    def validate_execution_mode(self) -> "DispatchConfig":
        if self.runtime.execution == "container":
            if self.container is None:
                raise ValueError("container config is required when runtime.execution is container")
            if self.safety is None or not self.safety.enabled:
                raise ValueError("safety config is required and must be enabled when runtime.execution is container")
            for worker in self.workers:
                required = WORKER_ENV_KEYS[worker.type]
                missing = [key for key in required if not worker.env.get(key)]
                if missing:
                    raise ValueError(f"worker {worker.name} missing env keys: {', '.join(missing)}")
        else:  # local: workers reuse the host CLI config, so no LLM env keys are required
            if self.local is None:
                self.local = LocalConfig()
        return self

    @classmethod
    def load(cls, path: Path) -> "DispatchConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        config = cls.model_validate(data)
        validate_prompt_resources(config.runtime.prompt_group)
        return config


def _validate_optional_positive_int_env(worker_name: str, env: dict[str, str], key: str) -> None:
    value = env.get(key)
    if value is None or not value.strip():
        return
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"worker {worker_name} env {key} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"worker {worker_name} env {key} must be greater than 0")


def _is_reserved_safety_env(key: object) -> bool:
    return isinstance(key, str) and (key.startswith("CAIRN_SAFETY_") or key in RESERVED_RUNTIME_ENV_KEYS)


def resolve_safety_token(config: SafetyConfig) -> str:
    value = os.environ.get(config.token_env, "").strip()
    if not value:
        raise ValueError(f"missing safety token environment variable: {config.token_env}")
    return value


def validate_prompt_resources(prompt_group: str) -> None:
    prompts_dir = resources.files("cairn.dispatcher.prompts")
    group_dir = prompts_dir.joinpath(prompt_group)
    if not group_dir.is_dir():
        raise ValueError(f"missing prompt group: {prompt_group}")
    required_tokens = PROMPT_REQUIRED_TOKENS_BY_GROUP.get(prompt_group, DEFAULT_PROMPT_REQUIRED_TOKENS)
    for name, tokens in required_tokens.items():
        try:
            content = group_dir.joinpath(name).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"prompt group {prompt_group} missing resource: {name}") from exc
        missing = [token for token in tokens if token not in content]
        if missing:
            raise ValueError(f"prompt group {prompt_group} resource {name} missing placeholders: {', '.join(missing)}")
