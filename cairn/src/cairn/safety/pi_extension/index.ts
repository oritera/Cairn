import { createHash, randomUUID } from "node:crypto";
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

import { postJson } from "./transport.mjs";


const ENV_KEYS = [
  "CAIRN_SAFETY_ENDPOINT",
  "CAIRN_SAFETY_TOKEN",
  "CAIRN_PROJECT_ID",
  "CAIRN_INTENT_ID",
  "CAIRN_RUN_ID",
  "CAIRN_WORKER",
  "CAIRN_PHASE",
  "CAIRN_SAFETY_TIMEOUT_MS",
  "CAIRN_SAFETY_MAX_PAYLOAD_BYTES",
  "CAIRN_SAFETY_MAX_BULK_CONCURRENCY",
  "CAIRN_SAFETY_MAX_UNATTENDED_BULK_SECONDS",
  "CAIRN_SAFETY_AUTH_CONCURRENCY",
  "CAIRN_SAFETY_AUTH_ATTEMPTS_PER_MINUTE",
  "CAIRN_SAFETY_AUTH_ATTEMPTS_PER_BATCH",
] as const;

type CapturedEnvironment = Record<(typeof ENV_KEYS)[number], string>;

type ActionState = {
  actionId: string;
  decisionEventId: string;
  toolName: string;
  input: Record<string, unknown>;
};

type PreflightResponse = {
  event_id: string;
  action_id: string;
  decision: "allow" | "block" | "resource_pause";
  rule_id?: string | null;
  reason: string;
  target?: string | null;
  auth_attempt_count?: number;
};


function captureEnvironment(): CapturedEnvironment {
  const captured = {} as CapturedEnvironment;
  for (const key of ENV_KEYS) {
    captured[key] = process.env[key] ?? "";
    delete process.env[key];
  }
  return captured;
}


export default function cairnSafetyExtension(pi: ExtensionAPI) {
  const env = captureEnvironment();
  const endpoint = env.CAIRN_SAFETY_ENDPOINT.replace(/\/$/, "");
  const timeoutMs = Number(env.CAIRN_SAFETY_TIMEOUT_MS || "2000");
  const maxPayloadBytes = Number(env.CAIRN_SAFETY_MAX_PAYLOAD_BYTES || "65536");
  const resourceBudget = {
    max_bulk_concurrency: Number(env.CAIRN_SAFETY_MAX_BULK_CONCURRENCY || "2"),
    max_unattended_bulk_seconds: Number(env.CAIRN_SAFETY_MAX_UNATTENDED_BULK_SECONDS || "600"),
    auth_concurrency: Number(env.CAIRN_SAFETY_AUTH_CONCURRENCY || "1"),
    auth_attempts_per_minute: Number(env.CAIRN_SAFETY_AUTH_ATTEMPTS_PER_MINUTE || "10"),
    auth_attempts_per_batch: Number(env.CAIRN_SAFETY_AUTH_ATTEMPTS_PER_BATCH || "30"),
  };
  const actions = new Map<string, ActionState>();

  const contextFields = () => ({
    schema_version: 1 as const,
    run_id: env.CAIRN_RUN_ID,
    project_id: env.CAIRN_PROJECT_ID,
    intent_id: env.CAIRN_INTENT_ID || null,
    worker: env.CAIRN_WORKER,
    phase: env.CAIRN_PHASE,
  });

  const fallback = (failedEvent: Record<string, unknown>, error: unknown) => {
    const transportError = error instanceof Error ? error.message : String(error);
    let record = {
      schema_version: 1,
      failed_event: failedEvent,
      transport_error: transportError,
    };
    let encoded = JSON.stringify(record);
    const prefix = "CAIRN_SAFETY_FALLBACK ";
    if (Buffer.byteLength(`${prefix}${encoded}\n`, "utf8") > maxPayloadBytes) {
      const original = JSON.stringify(failedEvent);
      const metadataKeys = [
        "schema_version", "event_id", "action_id", "run_id", "project_id", "intent_id",
        "worker", "phase", "event_type", "tool_name", "decision", "rule_id", "reason",
      ];
      const metadata = Object.fromEntries(
        metadataKeys.filter((key) => key in failedEvent).map((key) => [key, failedEvent[key]]),
      );
      record = {
        schema_version: 1,
        failed_event: {
          ...metadata,
          payload_sha256: createHash("sha256").update(original).digest("hex"),
          payload: {
            truncated: true,
            original_bytes: Buffer.byteLength(original, "utf8"),
            preview: original.slice(0, Math.min(4096, Math.floor(maxPayloadBytes / 4))),
          },
        },
        transport_error: transportError,
      };
      encoded = JSON.stringify(record);
    }
    process.stderr.write(`${prefix}${encoded}\n`);
  };

  const postEventBestEffort = async (
    event: Record<string, unknown>,
    signal?: AbortSignal,
  ) => {
    try {
      await postJson(`${endpoint}/events`, env.CAIRN_SAFETY_TOKEN, event, {
        timeoutMs,
        maxPayloadBytes,
        signal,
      });
    } catch (error) {
      fallback(event, error);
    }
  };

  pi.on("before_agent_start", async (event) => ({
    systemPrompt: `${event.systemPrompt}\n\n${[
      "Cairn safety rules:",
      "- remain autonomous and continue pursuing all safe directions within the project.",
      "- Never retry, encode, rephrase, rename, or otherwise evade a Cairn safety block.",
      "- CAIRN_SAFETY_BLOCKED requires a [V1][BRANCH_CLOSED] Fact with a manual verification procedure.",
      "- CAIRN_RESOURCE_PAUSED requires an [R1][RESOURCE_PAUSED] Fact and no expansion of the same bulk direction.",
      "- A block or resource pause closes only the current Intent; continue the project's other directions.",
    ].join("\n")}`,
  }));

  pi.on("tool_call", async (event, ctx) => {
    const actionId = randomUUID();
    const eventId = randomUUID();
    const input = event.input as Record<string, unknown>;
    const proposal = {
      ...contextFields(),
      event_id: eventId,
      action_id: actionId,
      tool_name: event.toolName,
      input,
      cwd: ctx.cwd,
      resource_budget: resourceBudget,
    };

    let response: PreflightResponse;
    try {
      response = await postJson(
        `${endpoint}/preflight`,
        env.CAIRN_SAFETY_TOKEN,
        proposal,
        { timeoutMs, maxPayloadBytes, signal: ctx.signal },
      ) as PreflightResponse;
      if (!response.event_id || !response.action_id || !response.reason) {
        throw new Error("invalid_preflight_response");
      }
      if (response.event_id !== eventId || response.action_id !== actionId) {
        throw new Error("mismatched_preflight_identity");
      }
      if (!["allow", "block", "resource_pause"].includes(response.decision)) {
        throw new Error("invalid_preflight_decision");
      }
    } catch (error) {
      fallback({
        ...proposal,
        event_type: "ACTION_DECISION",
        decision: "block",
        rule_id: "safety_unavailable",
        reason: "safety preflight was unavailable",
        payload: { proposal: { tool_name: event.toolName, input, cwd: ctx.cwd } },
      }, error);
      return {
        block: true,
        reason: `CAIRN_SAFETY_UNAVAILABLE action_id=${actionId} event_id=${eventId}. `
          + "The action was not executed. Close only the current Intent and provide a manual verification procedure.",
      };
    }

    actions.set(event.toolCallId, {
      actionId: response.action_id,
      decisionEventId: response.event_id,
      toolName: event.toolName,
      input,
    });
    if (response.decision === "allow") return;
    if (response.decision === "resource_pause") {
      return {
        block: true,
        reason: `CAIRN_RESOURCE_PAUSED action_id=${response.action_id} event_id=${response.event_id} `
          + `rule_id=${response.rule_id ?? "resource_budget"}. The action was recorded and was not executed. `
          + "Create an [R1][RESOURCE_PAUSED] Fact. Do not expand or rename the same bulk direction; continue other Intents.",
      };
    }
    return {
      block: true,
      reason: `CAIRN_SAFETY_BLOCKED action_id=${response.action_id} event_id=${response.event_id} `
        + `rule_id=${response.rule_id ?? "high_confidence_red_line"}. The action was recorded and was not executed. `
        + "Do not retry, encode, rephrase, or evade it. Close the Current Intent with a "
        + "[V1][BRANCH_CLOSED] Fact and provide a manual verification procedure.",
    };
  });

  pi.on("tool_result", async (event, ctx) => {
    const action = actions.get(event.toolCallId);
    if (!action) return;
    actions.delete(event.toolCallId);
    await postEventBestEffort({
      ...contextFields(),
      event_id: randomUUID(),
      action_id: action.actionId,
      event_type: "ACTION_RESULT",
      tool_name: action.toolName,
      payload: {
        decision_event_id: action.decisionEventId,
        input: action.input,
        content: event.content,
        details: event.details,
        is_error: event.isError,
      },
    }, ctx.signal);
  });

  pi.on("turn_end", async (event, ctx) => {
    await postEventBestEffort({
      ...contextFields(),
      event_id: randomUUID(),
      action_id: null,
      event_type: "ASSISTANT_MESSAGE",
      tool_name: null,
      payload: {
        turn_index: event.turnIndex,
        message: event.message,
        tool_results: event.toolResults,
      },
    }, ctx.signal);
  });

  pi.on("agent_end", async (event, ctx) => {
    await postEventBestEffort({
      ...contextFields(),
      event_id: randomUUID(),
      action_id: null,
      event_type: "AGENT_END",
      tool_name: null,
      payload: { messages: event.messages },
    }, ctx.signal);
  });

}
