from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from cairn.dispatcher.tasks.common import SafetyRunContext, run_worker_process
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.workers.base import DriverResult, RuntimeAsset
from cairn.dispatcher.runtime.process import ProcessResult

from conftest import make_config


def test_pi_loads_only_explicit_cairn_extension() -> None:
    worker = make_config().workers[0]

    for driver in (PiDriver(), PiDriver(local=True)):
        result = driver.build_execute(worker, "prompt", None)

        assert "--no-extensions" in result.argv
        extension_index = result.argv.index("-e")
        extension_path = result.argv[extension_index + 1]
        assert extension_path.replace("\\", "/").endswith("/cairn-safety/index.ts")
        assert {asset.path for asset in result.assets} == {
            extension_path,
            extension_path.replace("index.ts", "transport.mjs"),
        }
        assert all(asset.content.strip() for asset in result.assets)


def test_pi_conclude_reuses_the_explicit_extension_assets() -> None:
    worker = make_config().workers[0]
    driver = PiDriver()

    execute = driver.build_execute(worker, "execute", "session-001")
    conclude = driver.build_conclude(worker, "conclude", "session-001")

    assert isinstance(conclude, DriverResult)
    assert conclude.assets == execute.assets
    assert conclude.argv[conclude.argv.index("-e") + 1] == execute.argv[execute.argv.index("-e") + 1]


@pytest.mark.skipif(os.name != "nt", reason="Windows host path behavior")
def test_local_pi_uses_a_windows_absolute_extension_path() -> None:
    result = PiDriver(local=True).build_execute(make_config().workers[0], "prompt", None)

    extension_path = result.argv[result.argv.index("-e") + 1]
    assert Path(extension_path).is_absolute()
    assert all(Path(asset.path).is_absolute() for asset in result.assets)


class _FinishedProcess:
    def start(self) -> None:
        return None

    def communicate(self, *, timeout: int) -> ProcessResult:
        return ProcessResult(0, "ok", "")


class _RecordingBackend:
    def __init__(self) -> None:
        self.operations: list[tuple] = []
        self.env: dict[str, str] = {}

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        self.operations.append(("write", container_name, path, content))

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> _FinishedProcess:
        self.operations.append(("exec", container_name, tuple(command)))
        self.env = env
        return _FinishedProcess()


def test_runtime_installs_assets_and_trusted_env_before_process_start(monkeypatch) -> None:
    config = make_config()
    assert config.safety is not None
    worker = config.workers[0]
    worker.env["CAIRN_SAFETY_TOKEN"] = "untrusted-worker-value"
    backend = _RecordingBackend()
    command = DriverResult(
        argv=["pi", "-p", "prompt"],
        assets=(RuntimeAsset("/tmp/cairn-safety/index.ts", "extension"),),
    )
    context = SafetyRunContext(
        run_id="run-001",
        project_id="proj-001",
        intent_id="i001",
        worker=worker.name,
        phase="explore_execute",
    )
    monkeypatch.setenv("CAIRN_SAFETY_TOKEN", "trusted-process-value")

    result = run_worker_process(
        backend,
        "container-proj-001",
        worker,
        command,
        phase="explore_execute",
        timeout_seconds=30,
        safety=config.safety,
        safety_context=context,
    )

    assert result.returncode == 0
    assert [operation[0] for operation in backend.operations] == ["write", "exec"]
    assert backend.env["CAIRN_SAFETY_TOKEN"] == "trusted-process-value"
    assert backend.env["CAIRN_SAFETY_ENDPOINT"] == config.safety.endpoint
    assert backend.env["CAIRN_PROJECT_ID"] == "proj-001"
    assert backend.env["CAIRN_INTENT_ID"] == "i001"
    assert backend.env["CAIRN_RUN_ID"] == "run-001"
    assert backend.env["CAIRN_WORKER"] == worker.name
    assert backend.env["CAIRN_PHASE"] == "explore_execute"
    assert backend.env["CAIRN_SAFETY_MAX_BULK_CONCURRENCY"] == "2"
    assert backend.env["CAIRN_SAFETY_AUTH_ATTEMPTS_PER_MINUTE"] == "10"
    assert worker.env["CAIRN_SAFETY_TOKEN"] == "untrusted-worker-value"


def _node_binary() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for Pi extension behavior tests")
    return node


def _run_extension_harness(tmp_path: Path, *, mode: str) -> dict:
    extension = Path(__file__).parents[1] / "src" / "cairn" / "safety" / "pi_extension" / "index.ts"
    harness = tmp_path / "harness.mjs"
    harness.write_text(
        """
const handlers = new Map();
const writes = [];
const requests = [];
const originalWrite = process.stderr.write.bind(process.stderr);
process.stderr.write = (chunk, ...rest) => { writes.push(String(chunk)); return true; };
for (const [key, value] of Object.entries({
  CAIRN_SAFETY_ENDPOINT: "http://safety/internal/safety",
  CAIRN_SAFETY_TOKEN: "secret",
  CAIRN_PROJECT_ID: "proj-001",
  CAIRN_INTENT_ID: "i001",
  CAIRN_RUN_ID: "run-001",
  CAIRN_WORKER: "pi-main",
  CAIRN_PHASE: "explore_execute",
  CAIRN_SAFETY_TIMEOUT_MS: "2000",
  CAIRN_SAFETY_MAX_PAYLOAD_BYTES: "65536",
  CAIRN_SAFETY_MAX_BULK_CONCURRENCY: "2",
  CAIRN_SAFETY_MAX_UNATTENDED_BULK_SECONDS: "600",
  CAIRN_SAFETY_AUTH_CONCURRENCY: "1",
  CAIRN_SAFETY_AUTH_ATTEMPTS_PER_MINUTE: "10",
  CAIRN_SAFETY_AUTH_ATTEMPTS_PER_BATCH: "30",
})) process.env[key] = value;

globalThis.fetch = async (url, options) => {
  if (process.argv[3] === "failure") throw new Error("offline");
  const body = JSON.parse(options.body);
  requests.push({ url, body });
  if (String(url).endsWith("/events")) {
    return new Response(JSON.stringify(body), { status: 201, headers: { "content-type": "application/json" } });
  }
  return new Response(JSON.stringify({
    event_id: process.argv[3] === "mismatch" ? "wrong-event" : body.event_id,
    action_id: process.argv[3] === "mismatch" ? "wrong-action" : body.action_id,
    decision: process.argv[3] === "audit" ? "allow" : process.argv[3] === "resource" ? "resource_pause" : "block",
    rule_id: "filesystem_mass_delete",
    reason: "blocked",
    target: "/var/lib/app",
    auth_attempt_count: 0,
  }), { status: 201, headers: { "content-type": "application/json" } });
};

const extension = await import(process.argv[2]);
extension.default({ on(name, handler) { handlers.set(name, handler); } });
const before = await handlers.get("before_agent_start")({ systemPrompt: "base" }, { cwd: "/workspace" });
const result = await handlers.get("tool_call")({
  toolName: "bash",
  toolCallId: "tool-001",
  input: { command: process.argv[3] === "large_failure" ? "x".repeat(70000) : "rm -rf /var/lib/app" },
}, { cwd: "/workspace" });
if (process.argv[3] === "audit") {
  await handlers.get("tool_result")?.({
    toolName: "bash",
    toolCallId: "tool-001",
    input: { command: "pwd" },
    content: [{ type: "text", text: "/workspace" }],
    details: { exitCode: 0 },
    isError: false,
  }, { cwd: "/workspace" });
  await handlers.get("turn_end")?.({
    turnIndex: 1,
    message: { role: "assistant", content: [{ type: "text", text: "done" }] },
    toolResults: [],
  }, { cwd: "/workspace" });
  await handlers.get("agent_end")?.({
    messages: [{ role: "assistant", content: [{ type: "text", text: "done" }] }],
  }, { cwd: "/workspace" });
}
const deleted = [
  "CAIRN_SAFETY_TOKEN",
  "CAIRN_PROJECT_ID",
  "CAIRN_RUN_ID",
  "CAIRN_SAFETY_MAX_BULK_CONCURRENCY",
  "CAIRN_SAFETY_AUTH_ATTEMPTS_PER_MINUTE",
].every((key) => !(key in process.env));
process.stderr.write = originalWrite;
console.log(JSON.stringify({ before, result, deleted, writes, requests }));
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [_node_binary(), str(harness), extension.as_uri(), mode],
        check=True,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    return json.loads(completed.stdout)


def test_extension_blocks_after_preflight_and_injects_branch_rules(tmp_path: Path) -> None:
    payload = _run_extension_harness(tmp_path, mode="block")

    assert payload["deleted"] is True
    assert "remain autonomous" in payload["before"]["systemPrompt"]
    assert "only the current Intent" in payload["before"]["systemPrompt"]
    assert payload["result"]["block"] is True
    assert payload["result"]["reason"].startswith("CAIRN_SAFETY_BLOCKED ")
    assert "[V1][BRANCH_CLOSED]" in payload["result"]["reason"]
    assert payload["writes"] == []


def test_extension_fails_closed_and_emits_fallback_record(tmp_path: Path) -> None:
    payload = _run_extension_harness(tmp_path, mode="failure")

    assert payload["result"]["block"] is True
    assert payload["result"]["reason"].startswith("CAIRN_SAFETY_UNAVAILABLE ")
    assert len(payload["writes"]) == 1
    assert payload["writes"][0].startswith("CAIRN_SAFETY_FALLBACK ")
    json.loads(payload["writes"][0].removeprefix("CAIRN_SAFETY_FALLBACK "))


def test_extension_fails_closed_on_mismatched_preflight_identity(tmp_path: Path) -> None:
    payload = _run_extension_harness(tmp_path, mode="mismatch")

    assert payload["result"]["block"] is True
    assert payload["result"]["reason"].startswith("CAIRN_SAFETY_UNAVAILABLE ")
    assert payload["writes"][0].startswith("CAIRN_SAFETY_FALLBACK ")


def test_extension_bounds_large_fallback_records(tmp_path: Path) -> None:
    payload = _run_extension_harness(tmp_path, mode="large_failure")

    line = payload["writes"][0]
    assert len(line.encode("utf-8")) <= 65536
    record = json.loads(line.removeprefix("CAIRN_SAFETY_FALLBACK "))
    assert record["failed_event"]["payload"]["truncated"] is True
    assert len(record["failed_event"]["payload_sha256"]) == 64
    assert record["failed_event"]["payload"]["preview"]


def test_extension_returns_distinct_resource_pause_instruction(tmp_path: Path) -> None:
    payload = _run_extension_harness(tmp_path, mode="resource")

    assert payload["result"]["block"] is True
    assert payload["result"]["reason"].startswith("CAIRN_RESOURCE_PAUSED ")
    assert "[R1][RESOURCE_PAUSED]" in payload["result"]["reason"]
    assert "continue other Intents" in payload["result"]["reason"]


def test_extension_records_results_answers_and_agent_end_best_effort(tmp_path: Path) -> None:
    payload = _run_extension_harness(tmp_path, mode="audit")

    event_requests = [
        request for request in payload["requests"] if request["url"].endswith("/events")
    ]
    assert [request["body"]["event_type"] for request in event_requests] == [
        "ACTION_RESULT",
        "ASSISTANT_MESSAGE",
        "AGENT_END",
    ]
    assert event_requests[0]["body"]["action_id"] == payload["requests"][0]["body"]["action_id"]
    assert event_requests[0]["body"]["payload"]["is_error"] is False
    assert event_requests[1]["body"]["payload"]["message"]["role"] == "assistant"
    assert payload["requests"][0]["body"]["resource_budget"] == {
        "max_bulk_concurrency": 2,
        "max_unattended_bulk_seconds": 600,
        "auth_concurrency": 1,
        "auth_attempts_per_minute": 10,
        "auth_attempts_per_batch": 30,
    }
    assert payload["writes"] == []
