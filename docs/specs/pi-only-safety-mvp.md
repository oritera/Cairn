# Pi-Only Safety MVP Specification

## Status and precedence

This specification records the design agreed on 2026-08-20 for the Cairn fork.
It supersedes conflicting parts of `D:/Obsidian/note/奇/cairn 渗透二开.md`.
In particular, the MVP does not implement a general Action Broker, full protocol
semantic analysis, static scope allow-listing, or broad risk scoring.

## Product boundary

- Pi is the only production agent worker.
- Claude Code and Codex support are removed from code, images, configuration,
  examples, tests, and documentation.
- The mock worker remains test-only. It is not a valid production worker type and
  is not registered by the application.
- Container execution is the production enforcement boundary.
- Local execution may remain for development, but must be labelled best-effort:
  Pi and the extension run with the same user permissions, so Cairn must not claim
  that local mode is non-bypassable.

## Safety behavior

The normal Cairn exploration loop remains autonomous. The safety layer does not
assign a risk score to every action and does not require approval for ordinary
reconnaissance, reads, scans, authentication attempts, HTTP requests, or local
workspace writes.

The MVP blocks only explicit, high-confidence destructive red lines:

1. destructive filesystem or block-device operations;
2. destructive database statements such as `DROP`, `TRUNCATE`, and explicit
   `DELETE FROM` execution;
3. stopping, restarting, disabling, or destroying services and hosts;
4. account, group, ACL, GPO, or security-permission mutation;
5. persistence installation;
6. audit or log clearing;
7. explicit denial-of-service or resource-exhaustion commands;
8. explicit outbound file upload or exfiltration commands;
9. access to Cairn control-plane, audit-store, Docker socket, host-root, or
   safety-extension paths;
10. attempts to disable or launch a nested Pi without the trusted safety extension.

Rules are syntax-based and deliberately narrow. Ambiguous commands are allowed and
audited. The MVP does not claim to infer the behavior of arbitrary downloaded
binaries, opaque scripts, encoded commands, or side effects hidden behind a custom
protocol.

## Resource-budget behavior

The safety layer must also prevent autonomous agents from exhausting the operator's
local test environment. The initial deployment target is a 4-core, 8 GB RAM host,
so bulk work runs in a conservative low-resource mode.

Resource-budget controls are not vulnerability red lines. They pause or throttle a
branch when the next step would require too much local CPU, memory, time, or
authentication volume. The agent may still perform small, serial, evidence-driven
checks.

Default resource guidance for soft prompts and hard preflight is:

1. no more than two concurrent processes or threads for bulk work;
2. no single bulk command intended to run for more than 10 minutes unattended;
3. no unbounded loops, background worker pools, or high-concurrency fan-out;
4. no `xargs -P`, GNU `parallel`, shell background-loop patterns, or tool flags
   whose purpose is high parallelism unless bounded at or below the configured
   concurrency limit;
5. stop the current bulk direction when CPU, memory, connection count, or target
   latency becomes visibly abnormal, then summarize the evidence already obtained.

The first implementation should keep these limits configurable, but the documented
defaults are the safe operating profile for a 4-core, 8 GB host.

## Credential-use and password-spraying limits

Credential discovery is allowed, but autonomous credential validation must be
rate-limited. When the agent obtains many usernames, passwords, tokens, keys, or
sessions, it must first organize and de-duplicate them instead of immediately
launching a full Cartesian-product spray.

Default credential-validation limits are:

1. authentication-attempt concurrency is 1;
2. authentication attempts are capped at 10 per minute;
3. a single autonomous batch is capped at 30 authentication attempts;
4. each batch must summarize successes, failures, lockout risk, and the proposed
   next step before another batch is attempted;
5. high-concurrency modes of tools such as Hydra, Medusa, Ncrack,
   NetExec/CrackMapExec, or equivalent wrappers are forbidden;
6. bypassing rate limits, account-lockout policy, MFA, CAPTCHA, or equivalent
   anti-abuse controls is forbidden.

If useful validation requires exceeding these limits, the current Intent is paused
with an R1 resource fact instead of expanding the spray. Reasoning may later explore
other assets, higher-confidence individual credentials, or non-authentication impact
paths, but must not recreate the same large spray under a new Intent.

## Pi integration

Cairn loads one trusted Pi extension explicitly while continuing to disable
extension auto-discovery:

```text
pi --no-extensions -e /tmp/cairn-pi/<worker>/cairn-safety/index.ts [ordinary Pi arguments]
```

Project-local and user-global extensions are not loaded. The trusted extension:

- injects the soft safety instruction in `before_agent_start`;
- intercepts `tool_call` before execution;
- submits the proposed tool call to Cairn safety preflight;
- returns `{ block: true, reason }` for a blocked decision;
- records tool results, assistant messages, and agent completion as audit events;
- removes safety credentials from `process.env` before any Bash child process can
  inherit them.

## Record-before-block invariant

For a matched high-confidence red line, the sequence is:

```text
Pi proposes tool call
  -> extension sends preflight request
  -> server classifies the tool call
  -> server appends ACTION_DECISION(decision=block)
  -> transaction commits
  -> server returns event_id/action_id/rule_id/reason
  -> extension blocks the tool call
  -> Pi receives CAIRN_SAFETY_BLOCKED and continues summarizing
```

The preflight request is idempotent by `event_id`. A retry returns the previously
committed decision rather than writing a duplicate.

If preflight cannot be reached, the extension fails closed for the tool call. It
emits a structured fallback record to stderr so the Dispatcher can backfill the
event after process completion. This fallback reduces but cannot eliminate the
small durability gap caused by simultaneous server and process failure.

## Audit journal

The server owns an append-only `audit_events` table. No API updates or deletes
individual events. Project deletion, if introduced later, must not silently delete
audit rows.

Each event contains:

- `event_id`, `action_id`, `run_id`;
- `project_id`, optional `intent_id`, `worker`, and phase;
- event type and tool name;
- decision, rule identifier, and reason where applicable;
- normalized JSON payload, SHA-256 of the original payload, truncation flag;
- server timestamp.

MVP event types are:

- `ACTION_DECISION` — proposal plus allow/block decision, committed before execution;
- `ACTION_RESULT` — tool result or error for an allowed action;
- `ASSISTANT_MESSAGE` — assistant response text at the end of a turn;
- `AGENT_END` — final process/session summary;
- `AUDIT_BACKFILL` — Dispatcher recovery of a structured fallback record.

Write endpoints require a server/dispatcher shared token. The Pi extension captures
the token during initialization and deletes it from the process environment. Read
queries are exposed through the existing project UI/API trust boundary.

The query API must support project, intent, run, event type, decision, tool, time,
and cursor filters. This data must answer:

- What did the Agent propose?
- Which tool and target did it use?
- Why was the action allowed or blocked?
- Did the action actually execute?
- What result or output reference was recorded?
- Did the blocked action produce a V1 branch closure?
- What answer did the Agent return?

## V1 branch closure

The MVP reuses the existing Fact description; it does not add a Finding table.
When final validation would require a blocked red-line action, the current Intent is
concluded with a Fact beginning with:

```text
[V1][BRANCH_CLOSED]
```

The description contains target, vulnerability hypothesis, confidence, confirmed
prerequisites, the action not executed, stop reason, manual verification procedure,
cleanup guidance, and an explicit autonomous no-retry statement.

The Pi extension tells the Agent not to retry, rephrase, encode, or evade a blocked
action. The explore-conclude prompt receives the blocked action context. If Pi still
does not return a valid V1 Fact, the Dispatcher synthesizes a conservative V1 Fact
from the committed audit event and concludes the Intent itself.

Only the current Intent closes. The project remains active. Reasoning may explore
other attack surfaces, related assets, and non-destructive impact paths, but must not
create a duplicate Intent whose purpose is to retry the same prohibited final
verification.

For a bootstrap branch, a blocked action produces a V1 Fact but never completes the
whole project. For a reason task, a blocked action is audited and the reason lease is
released; reason has no current Intent to conclude.

## R1 resource pause

The MVP also reuses the existing Fact description for resource pauses. When final
progress on the current Intent would require exceeding the configured local resource
or credential-validation budget, the current Intent is concluded with a Fact
beginning with:

```text
[R1][RESOURCE_PAUSED]
```

The description contains target, pause reason, confirmed evidence, credential or
workload counts where applicable, the low-resource validation already performed,
the larger action not executed, recommended manual or isolated-environment
verification, and an explicit autonomous no-expand statement.

An R1 pause is not a vulnerability confirmation and does not prove Goal completion.
It records that Cairn intentionally avoided exhausting the local operator machine or
triggering uncontrolled authentication volume. Only the current Intent pauses; the
project remains active.

## Configuration

Dispatcher configuration adds a required production safety section:

```yaml
safety:
  enabled: true
  endpoint: "http://127.0.0.1:8000/internal/safety"
  token_env: "CAIRN_SAFETY_TOKEN"
  request_timeout_ms: 2000
  max_payload_bytes: 65536
```

`CAIRN_SAFETY_TOKEN` is supplied independently to Cairn Server and Dispatcher. It is
not accepted in `common_env` or worker `env`.

## Acceptance criteria

1. Public configuration accepts only `type: pi`.
2. Production code and worker images contain no Claude Code or Codex adapter/runtime.
3. Mock execution is available only from test support.
4. Pi loads only the explicitly supplied Cairn extension.
5. A benign read command is allowed and produces decision/result events.
6. Every red-line fixture is blocked before execution and has a committed decision
   event whose timestamp precedes the blocked result returned to Pi.
7. Similar but benign fixtures do not match the red-line rules.
8. A blocked explore/bootstrap path closes only its current Intent with a V1 Fact.
9. The project remains active and can schedule a different Intent afterward.
10. Audit queries show the proposal, decision, execution state, output reference,
    V1 linkage, and assistant response.
11. Safety endpoint or token failure never permits a tool call to bypass preflight.
12. High-concurrency bulk-command fixtures are blocked or paused according to the
    configured resource budget.
13. Credential-spraying fixtures above the configured concurrency, rate, or batch
    limits produce an R1 resource pause rather than a full spray.
14. Similar low-rate credential checks within the configured budget remain allowed
    and audited.
15. The full Python test suite and a Pi v0.73.0 extension smoke test pass.
