# Cairn Dispatcher Design (Pi-only Safety MVP)

## Responsibilities

The Dispatcher reads the Cairn fact graph, schedules bootstrap/reason/explore tasks, manages one execution environment per project, maintains leases, and is the only component that writes Agent outcomes back to the graph. Pi receives prompts and returns structured output; it does not claim Intents or call graph mutation APIs directly.

## Production worker

Pi Coding Agent is the only production worker type. Configuration uses `type: pi` and the `PI_MODEL`, `PI_BASE_URL`, `PI_API_KEY`, and `PI_PROVIDER_API` variables. The image pins `@mariozechner/pi-coding-agent@0.73.0`.

Pi runs with `--no-extensions`, `--no-skills`, and `--no-context-files`, then loads exactly one packaged Cairn extension with `-e`. Test-only mock behavior is injected from tests and is not registered in production configuration.

## Scheduling

- `bootstrap` handles a new project through its reserved bootstrap Intent.
- `reason` reads the graph and proposes independent Intents or completes the project when existing Facts prove the Goal.
- `explore` advances one claimed Intent and concludes it with one Fact.
- A project in `stopped` or `completed` state receives no new work.
- Safety closure affects only the current Intent; it never stops or completes the project.

## Safety preflight

The trusted Pi extension captures the shared safety token and removes it from the child process environment. Before every tool call it sends project, Intent, run, worker, phase, tool, input, working directory, and resource budget to `POST /internal/safety/preflight`.

The Server applies only explicit high-confidence rules. It commits `ACTION_DECISION` before returning allow, block, or resource_pause. Ambiguous commands stay allowed and audited. If preflight is unavailable, the extension fails closed for that call and emits a `CAIRN_SAFETY_FALLBACK` stderr record; the Dispatcher backfills it immediately after process communication.

## Resource defaults for 4 CPU / 8 GB

- bulk concurrency: 2;
- unattended bulk duration: 600 seconds;
- authentication concurrency: 1;
- authentication attempts: 10 per minute and 30 per autonomous batch.

Known credential-tool syntax and explicit high-parallelism are enforced in preflight. Resource matches are pauses, not vulnerability findings.

## V1 and R1 closure

A destructive block must conclude the current Intent with a Fact beginning `[V1][BRANCH_CLOSED]`. A resource pause must use `[R1][RESOURCE_PAUSED]`. Pi gets one same-session conclude opportunity; invalid or missing output is replaced by a conservative Dispatcher-generated Fact. Bootstrap never calls project completion for either marker. Reason owns no current Intent, so it records the event and releases its reason lease.

## Audit journal

The Server stores append-only `ACTION_DECISION`, `ACTION_RESULT`, `ASSISTANT_MESSAGE`, `AGENT_END`, and `AUDIT_BACKFILL` events. Correlation uses project_id, intent_id, run_id, action_id, worker, and phase. Payloads have a SHA-256 digest and bounded storage preview. The Web Audit tab supports decision, event type, Intent, tool, and cursor filters.

## Enforcement limits

Container mode is the production enforcement claim. Local mode is best-effort. The classifier does not semantically inspect arbitrary binaries, encoded payloads, or opaque script internals. The design intentionally avoids broad risk scores, a general action broker, or manual approval for every tool call.
