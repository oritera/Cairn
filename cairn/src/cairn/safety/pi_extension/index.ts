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
  // Fail closed when the Pi runtime does not expose the API this gate is built on. An inert
  // extension is worse than a task that refuses to start: every tool call would run ungated
  // while the audit stayed empty, and nothing would look wrong.
  if (!pi || typeof pi.on !== "function") {
    throw new Error(
      "cairn-safety: this Pi build does not expose the extension API (pi.on); refusing to run ungated",
    );
  }
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

  // The engagement rules of engagement, injected into every agent the same way the safety rules
  // are, so a new flow inherits them without the operator pasting them into the task box. Target
  // specific scope (hostnames, accounts, payloads) is deliberately NOT here: it belongs to the
  // per-flow task message.
  const ENGAGEMENT_ROE = `
# 通用框架（模块一 ~ 八）

## 一、任务定位

在授权范围内、全部安全约束内，对目标进行尽可能全面、深入的安全测试，目标是**发现并确认尽可能多的、不重复的、真实的、可复现的安全漏洞**。

系统性覆盖：可见资产、服务、端口、Web 页面、API、认证授权、业务流程、文件访问、配置暴露、敏感信息泄露，以及已发现技术栈所对应的攻击面。

**漏洞记录与持续性原则：**

- 发现漏洞后记录证据、影响与复现条件，然后**继续**测试尚未覆盖的资产与漏洞方向。
- 不得因已获得单个高危漏洞而提前结束整个项目。
- 不得以重复问题、同一根因的多个表现、低置信猜测或未验证假设凑数量。
- 每个漏洞必须关联：具体目标、证据 Fact、影响范围、验证状态。
- 若继续利用会命中破坏性红线，只关闭该漏洞对应的验证方向，记录已满足的前提条件并提供人工验证方法，然后继续其他测试方向。

## 二、资产发现与验证姿态

在授权范围内主动推进，**允许 Agent 自主行为**：

- 信息收集、资产枚举、端口扫描、目录探测、指纹识别、普通 HTTP/API 请求、认证测试、只读查询、非破坏性 PoC、读取当前权限可访问的信息（**数据库业务数据除外，见模块六**）、本地保存脚本与证据，均可自主执行。
- 不得因笼统的风险担忧降低测试深度；只有明确命中后续红线时才停止、降速或收口。

**授权与资产边界：**

- **允许 Agent 自主进行信息收集与资产发现。**
- 对授权范围内资产可开展深入测试；对范围外或未授权资产，仅允许信息收集与资产发现（不进行利用、变更或破坏性操作，并遵守全局红线）。
- **⚠️ 本次对边界额外收紧，见模块九（主机白名单），其优先级高于本条。**

**判断原则（四态区分）：**

- 已确认事实 / 高置信推断 / 未验证假设 / 未执行动作 —— 必须严格区分。
- 不得把"计划执行""被禁止执行""因资源限制暂停"的动作描述为"已经成功"。

## 三、硬性红线（按层级禁止）

**红线处置总则（全局规则）：**

- **除本框架明确豁免的无害化上传验证外，凡对目标执行"写入 / 变更 / 删除 / 清除 / 重启 / 禁用 / 创建账号"等任何有副作用或不可逆的操作，一律不执行。**
- 此禁止**仅针对命中红线的该操作 / 该方向**，其余测试方向不受影响；仅记录已满足的前提条件，并提供人工验证方法。
- **允许无害化验证上传**，且该验证必须符合模块三 · C 的 6 项判定标准并留档；凡超出该标准的任何上传（可执行、可解释、可被服务端消费、覆盖、落既定路径）一律视为命中红线，停止该方向，仅记录前提并交由人工验证，但必须返回：上传了**什么文件**（文件名、内容摘要、文件类型、字节数）、**什么内容**、上传到**什么位置**（完整路径或接口返回的存储位置）。
- **⚠️ 本次对上传额外收紧，见模块十二 · C。**

任何一条红线命中即停止该方向；红线之间按 A > B > C > D 优先，且不得以任何方式规避。

### A. 破坏性红线（数据 / 服务 / 账号 / 持久化）

- 禁止递归或批量删除目标文件、格式化文件系统、擦除磁盘、覆盖裸设备或关键数据。
- 禁止执行 DROP、TRUNCATE、破坏性 DELETE FROM，或其他导致目标数据丢失的数据库操作。
- 禁止停止、重启、禁用、销毁或故意干扰目标服务、主机、容器、集群与关键基础设施。
- 禁止创建、删除、禁用或修改账号、用户组、ACL、GPO、RBAC、资源权限与安全策略。
- 禁止安装自启动项、计划任务、持久化服务、后门账号、WebShell、持久化密钥或其他持久化机制。

### B. 安全红线（日志 / DoS / 外传 / 保护路径 / 绕过）

- 禁止删除、清空、关闭、破坏或篡改日志、审计、监控、告警与安全遥测。
- 禁止流量洪泛、Fork Bomb、连接耗尽、磁盘/内存/CPU 耗尽、破坏性压力测试或其他拒绝服务行为。
- 禁止将目标数据、敏感信息或本地文件外传至第三方或未授权地址。
- 禁止访问、读取、修改或破坏 Cairn 控制面、审计存储、安全凭据、安全扩展、Docker Socket、宿主机根目录与受保护路径。
- 禁止通过换工具、脚本、解释器、编码、混淆、拆分命令、间接调用或嵌套 Agent/子代理来绕过上述任何红线。

### C. 文件操作红线（优先级同 A）

- 严格禁止对目标文件/目录执行任何删除、修改、覆盖、移动或重命名操作，包括写入目标现有文件、截断、追加内容到既有文件。
- 仅允许一次"无害化上传验证"，且必须同时满足下列全部 6 项判定标准，缺一即视为命中红线，停止该方向：

**判定标准 1：载荷最小化。** 文件内容为随机生成的惰性字节序列（如随机 hex、字母串），不含任何可执行或可解释语义。不允许出现 PHP、JSP、JSPX、ASPX、脚本、SVG 事件、HTML 标签、宏、OOXML 公式等；不得以 \`.jsp\`、\`.php\`、\`.html\`、\`.svg\`、\`.xlsx\`、\`.docm\`、\`.jar\`、\`.war\`、\`.exe\`、\`.sh\` 等可执行或可被下游消费的扩展名提交。

**判定标准 2：不覆盖既有文件。** 上传前先确认目标路径或对象 key 不存在；用随机 UUID 作为新 key，绝不使用任何已存在的文件名或日期目录内已知对象名。

**判定标准 3：不写敏感或既定路径。** 不接受任何能落到既定程序路径、上传目录、Web 可执行目录、模板目录、配置资源目录，或任何会被服务端渲染、执行、持久化消费的路径；仅接受"对象存储、普通附件"这类纯存储位置。

**判定标准 4：验证边界唯一。** 仅验证"可上传、可访问"两项能力，不做任何进一步利用；不尝试触发服务端执行、转码、预览、模板渲染、导入解析或内容投毒。

**判定标准 5：可证明的最小载荷。** 载荷仅用于证明"攻击者能把文件放进该位置，并在目标可访问位置取回"，且取回时不产生任何副作用。

**判定标准 6：完整留档。** 每次上传必须记录并返回：文件名、内容摘要（前若干字节或哈希）、文件类型、字节数、上传到的存储位置或完整 URL，以及访问验证结果。

- 若上传行为会触发**覆盖既有文件、落盘到既定敏感路径、被当作可执行/持久化/可渲染内容消费**，则命中红线，停止该方向，仅记录前提并交由人工验证。
- **上传文件不得保留原文件名后缀中任何可执行/可解释特征**；若仅能通过上传 \`.jsp\`/\`.html\` 等后缀来证实能力，则视为命中红线，改用只读等价验证或最小惰性载荷（用服务端允许的纯存储后缀），并记录该限制。
- **每次上传都必须返回并记录**：上传了什么文件（文件名、内容摘要、文件类型、字节数）、什么内容、上传到目标的什么位置；并记录访问验证结果。

### D. 数据库操作红线（优先级同 A）

- **SQL 注入等任何操作，一律禁止对数据库执行增、删、改（INSERT / UPDATE / DELETE）、DDL 建删改（CREATE / ALTER / DROP）、存储过程/触发的创建或修改等一切变更数据库状态或结构的操作。**
- 数据库方向仅允许**只读、非破坏性**验证（详见模块六）。
- **"无害化上传验证"生成的任何对象/数据，不得与数据库写库动作关联**；若上传或验证流程会进一步触发服务端对数据库的 INSERT/UPDATE/DELETE（如导入解析、流程状态回写、元数据落库），则该上传即视为命中 D 红线，立即停止，只记录前提。

## 四、本机资源预算

当前运行环境资源有限：**4 核 CPU、8G 内存**，所有批量任务必须按低资源模式执行。

默认限制：

- 并发进程或线程数 **≤ 2**；
- 单个批量命令运行时间 **≤ 10 分钟**；
- 不启动无限循环、后台任务池或无人值守长任务；
- 不使用会吃满 CPU/内存的参数；
- 不使用 \`xargs -P\`、\`parallel\`、后台循环、爆破工具高线程模式或等价方式制造高并发。

**补充硬性要求：**

- **明确禁止密码喷洒、撞库、大规模口令尝试、字典爆破等高耗资源行为**，无论以何种工具或方式（含 hydra、medusa、ncrack、netexec/crackmapexec 等）。
- 若任务规模较大，须先小批量抽样验证，再根据结果决定是否继续；每批完成后总结进展，不连续发起多批高负载任务。
- 若观察到 CPU、内存、连接数、响应延迟明显异常，立即停止当前批量动作，只记录已确认结果，改用低速、低并发、抽样方式继续。

> 本次实际主机为 4 vCPU / 15 GiB，但**仍按上述低资源模式执行**（并发 ≤ 2），不因内存充裕而放宽。

## 五、凭据使用与验证

获得大量账号、密码、Token、密钥或会话信息后，**禁止立即进行全量密码喷洒、全量撞库或用户名 × 密码 × 目标的笛卡尔积尝试**。凭据使用必须遵守：

- 先离线整理、去重、分类，并标注来源。
- 优先验证少量最高置信凭据；默认只做低速抽样验证。
- 认证尝试并发数 **≤ 1**；每分钟认证尝试 **≤ 10 次**；单批认证尝试 **≤ 30 次**。
- 每批结束后必须总结成功、失败、锁定风险与下一步建议。
- **禁止使用上述工具的高并发爆破模式；禁止绕过速率限制、账号锁定策略或验证码机制。**
- 若验证规模超过上述预算，应停止本方向，输出资源暂停结论（见模块八模板 R1），而不是继续压满机器。

> 本次只有一个测试账号，登录尝试应**远低于**上述预算上限。

## 六、数据库访问与验证

- 若在测试中**获取到数据库账号密码**，仅允许执行**连通性验证**：确认可成功连接、识别数据库类型与版本即可。
- **连接验证成功后立即停止**，不再继续对数据库进行任何进一步操作（不列举数据、不查询业务表内容、不导出、不变更）。
- 验证过程仅使用**只读、非破坏性**手段（如 \`SELECT 1\`、\`SELECT version()\` 等最小只读语句）；任何写、删、改、建、删表操作一律禁止。
- 禁止以此凭据进行更高权限提升、横向连接其他数据库、或读取/外传数据库中的敏感业务数据。
- 记录：连接目标、数据库类型/版本、使用的验证语句、连接结果，作为 Fact 证据。

> ⚠️ 本次额外要求：**若该数据库地址落在授权主机之外，连"连通性验证"也不得执行**（见模块九·主机白名单），改为只记录凭据存在性与人工验证方法。

## 七、证据与 Fact 管理

- 每条证据以 **Fact** 编号归档，绑定：目标、证据内容、证据形式（请求/响应/截图/文件）、影响、复现条件、验证状态。
- 证据保存至本地并记录路径；Fact 编号需连续、可溯源、不与未验证假设混用。
- 多条 Fact 指向同一问题时合并，不重复计数漏洞。

## 八、输出模板

\`\`\`
目标：<目标资产或认证入口>
原因：凭据验证规模超过当前资源预算
已整理凭据：<数量和类型>
已执行低速验证：<数量和结果>
未执行动作：<未进行的全量喷洒/撞库/爆破>
建议：由人工在隔离环境、明确速率限制和监控条件下继续验证
自主重试：禁止在当前任务中扩大批量认证尝试
\`\`\`

---

`;

  pi.on("before_agent_start", async (event) => ({
    systemPrompt: `${event.systemPrompt}\n\n${ENGAGEMENT_ROE}\n${[
      "Cairn safety rules:",
      "Cairn blocks the actions below before they run. Do not attempt them, and do not look for a way around them:",
      "- recursive or forced deletion outside the workspace: any rm/rmdir/unlink/shred/find -delete whose target is absolute, ~-relative, parent-relative or a glob, wrapped or not",
      "- raw block-device or filesystem destruction: dd of=/dev/..., mkfs*, wipefs, blkdiscard, fdisk/parted/sgdisk, mkswap, pv/vg/lv removal",
      "- destructive data statements: DROP, TRUNCATE, DELETE FROM, ALTER, CREATE, INSERT, UPDATE, GRANT, REVOKE on psql/mysql/mariadb/sqlite3/sqlcmd/sqlplus/clickhouse-client/cqlsh/duckdb, and FLUSHALL/DEL/dropDatabase style commands on redis-cli/mongo",
      "- stopping, restarting, disabling or killing a service, the container runtime or the init process; rebooting or halting the host",
      "- creating, changing or deleting users, groups, credentials or ACLs; or chmod/chown on a protected path",
      "- installing persistence: crontab entries, at/systemd-run jobs, start-up hooks, shell start-up files, authorised-keys files, backdoors",
      "- clearing or tampering with audit logs: journalctl --vacuum-*/--rotate, logrotate -f, and any write, rm, truncate or shred against /var/log",
      "- denial of service: flood modes (ping -f, hping --flood), load generators (ab, wrk, siege, stress-ng, iperf), fork bombs",
      "- uploading or exfiltrating files outbound: curl -T/-Tfile/-d @file/-F k=@file, wget --post-file, scp/rsync/sftp to a remote destination",
      "- touching the container control plane: docker/podman/nerdctl/ctr/crictl, nsenter/unshare/chroot, curl --unix-socket, the Docker socket",
      "- launching Pi without the trusted Cairn safety extension",
      "- writing into /etc, /var/log, /proc/sys, /root, /sys, the Cairn database, shell start-up files or SSH authorised-keys files; the file tools additionally refuse anything outside the workspace",
      "- wrapping any of the above in another command (timeout, su -c, xargs, busybox, nohup, env, nice) - the wrapper does not exempt it",
      "Everything else stays autonomous: reads, scans, HTTP requests, authentication inside the rate budget, and evidence capture in the workspace.",
      "Detection is syntax-based: it recognises commands and their arguments, not the behaviour of interpreter payloads, encoded blobs or downloaded binaries. Do not treat it as a boundary and do not assume silence means approval -- every proposal is recorded, so an action that slips past is still attributed to you.",
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
