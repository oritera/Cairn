# Dispatcher

---

## 本质

Dispatcher 是 Cairn 的客户端执行器。它负责：

1. 协调项目容器生命周期
2. 给 Agent 下发明确任务
3. 代 Agent 调用 Cairn API 写回图

Agent 不直接认领 Intent，不直接 heartbeat，不直接调用 Cairn API。Agent 只接收 Dispatcher 下发的任务，返回结构化结果；Dispatcher 再决定如何请求 Server。

---

## 设计要点

1. Agent 的输出任务收敛成三类：`bootstrap`、`reason` 和 `explore`；`bootstrap` 只在项目初始态运行，让 Agent 直接尝试解决整个问题；主阶段只有在已解决时才返回，且必须同时给出关键 Fact 和 `complete`；若主阶段超时，再由 `bootstrap_conclude` 收尾产出 Fact；`reason` 负责读图判断是否完成或是否需要提出一个新 intent；`explore` 只负责执行一个已认领 intent 并产出一个 Fact 结论。
2. Dispatcher 是唯一的协议写入者和控制面；Agent 不 claim、不 heartbeat、不直接调用 Cairn API。
3. 超时策略按任务类型定义；`bootstrap` 和 `explore` 都支持“第一阶段执行 + timeout / parse-fail 后用同一 session 进入 conclude 收尾”的双阶段模式。
4. Prompt 以 markdown 文件形式随代码分发；支持按 prompt 组切换；Worker 行为由 `claudecode`、`codex`、`mock` 等 driver 实现；`dispatch.yaml` 只描述运行期参数。
5. 调度上，项目初始态优先 `bootstrap`；非初始态项目内始终先 `explore`、后 `reason`；`reason` 的并发约束通过服务端的项目级 `project.reason` lease 表达为“单项目最多一个”，`bootstrap` 的并发约束是“单项目最多一个保留 bootstrap intent 且最多一个 bootstrap 任务”，跨项目允许并行；`runtime.interval` 被刻意复用为主循环节拍和带 claim 任务的 heartbeat 周期。
6. Worker 按独立的 LLM 并发配额单元建模；同一个 key 不拆成多个 Worker，因此并发控制使用 `workers[].max_running` 即可。
7. 运行日志按“状态变化优先”设计：稳定轮询、正常 heartbeat、重复 skip 原则上不刷屏；容器创建、任务派发、容器内新进程启动、健康检查、超时、收尾、释放 intent、worker 进入短暂不可选窗口等事件必须可见。
8. 项目容器收尾不应阻塞主调度循环；多个已完成项目的容器 cleanup 可以并行进行。
9. 项目切到非 `active` 后，Dispatcher 必须把它视为硬停止：不再派发新任务；对本地仍在运行的 `bootstrap`、`explore`、`reason` 任务立即发出取消；对已取消任务不再进入 conclude fallback；并在后续轮询中停止该项目容器，杀掉容器内仍在运行的 Agent 进程。
10. 当前实现按“单 Dispatcher 实例”设计和测试；不支持多个 Dispatcher 同时连接同一服务端共同调度。
11. 若项目曾 `completed` 后又被服务端显式 `reopen` 为 `active`，Dispatcher 不做特殊分支：它会把这视为普通 active 项目继续调度；同时若该项目容器仍处于已排队 cleanup 状态，Dispatcher 会先等待 cleanup 完成，避免与旧的 completed/stopped cleanup 竞态。
12. 已知限制：当前协议只记录当前 claim 持有者，不保留 Intent 的 worker 历史；因此项目被 `stopped` 后，随着 open intent 的 `worker` 被服务端清空，Dispatcher/UI/API 都无法直接展示“停止前最后是谁在推进这个 intent”。后续若要补这部分可观测性，较合理的方向是在 Intent 上增加类似 `worker_history` 的历史字段，而不是改变当前 claim 语义。

补充：

- 项目被删除后，Dispatcher 会把它视为 `deleted`。这和 `stopped` 一样会先取消本地运行中的任务，但容器收尾语义不同：`deleted` 对应的 orphan 容器会被直接删除，而不是仅停止后保留。

---

## 架构概览

这个项目在架构上可以分成 4 个部分：

1. Cairn Server
2. Dispatcher
3. 项目容器
4. Worker / Agent CLI

### 1. Cairn Server

Server 是协议真相源。

它负责：

- 保存 Project / Fact / Intent / Hint
- 提供协议接口
- 维护 Intent 的认领、心跳、结论状态
- 维护项目级 `reason` lease 的认领、心跳与释放状态

### 2. Dispatcher

Dispatcher 是这个工程要实现的核心。

它负责：

- 拉取项目图状态
- 决定当前该派发哪一种任务
- 选择哪个 Worker 来执行
- 管理项目容器和 Worker 进程
- 维护 session、超时、健康检查、收尾
- 把结果写回 Cairn Server

### 3. 项目容器

每个项目对应一个运行容器。

这个容器是该项目的执行环境，通常负责：

- 提供工具链
- 提供网络环境
- 承载该项目下的 Worker 进程

### 4. Worker / Agent CLI

Worker 不是协议参与者本身，而是 Dispatcher 管理下的执行单元。

例如：

- Claude Code CLI
- Codex CLI

它们负责：

- 接收 Dispatcher 渲染好的 prompt
- 在当前容器内执行任务
- 输出结构化 JSON

### 组件关系

```text
                         +----------------------+
                         |     Cairn Server     |
                         |----------------------|
                         | Projects / Facts     |
                         | Intents / Hints      |
                         | Protocol API         |
                         +----------^-----------+
                                    |
                           read / write API
                                    |
+-----------------------------------------------------------+
|                         Dispatcher                        |
|-----------------------------------------------------------|
| Scheduling / Task Dispatch / Session / Timeout / Health   |
| Container Lifecycle / Protocol Writeback                  |
+----------------------+----------------------+-------------+
                       |                      |
             manage container        manage container
                       |                      |
          +------------v-----------+  +------v-------------+
          |   Project Container A  |  | Project Container B|
          |------------------------|  |--------------------|
          | Worker / Agent CLI     |  | Worker / Agent CLI |
          | - Claude Code          |  | - Codex            |
          | - Codex                |  | - ...              |
          +------------------------+  +--------------------+
```

### 执行主链路

Dispatcher 会同时读取两类数据：

- 结构化接口：用于调度、状态判断、intent 选择、协议写回
- `GET /projects/{project_id}/export?format=yaml`：仅用于构造 prompt 所需的图快照

1. Dispatcher 从 Server 读取项目图
2. Dispatcher 依据调度规则选择任务类型和 Worker
3. Dispatcher 渲染 prompt 与命令占位符
4. 如果是 `explore`，Dispatcher 先通过 `POST /projects/{project_id}/intents/{intent_id}/heartbeat` 认领目标 intent；如果是 `reason`，则先通过 `POST /projects/{project_id}/reason/claim` 认领项目级 reason lease
5. Dispatcher 在项目容器内启动 Worker 进程
6. Worker 输出结构化 JSON
7. Dispatcher 解析结果，并调用 `POST /projects/{project_id}/complete`、`POST /projects/{project_id}/intents`、`POST /projects/{project_id}/intents/{intent_id}/conclude`、`POST /projects/{project_id}/intents/{intent_id}/release` 或 `POST /projects/{project_id}/reason/release`

项目若被切到 `stopped`，这条主链路会在下一轮短路：Dispatcher 不再把该项目纳入 active 调度集合，会先取消本地仍在运行的任务，再转入容器 cleanup 流程并停止项目容器。对 `bootstrap` / `explore` 来说，被 `stopped` 取消后不会再进入 conclude fallback，因此不会再额外落 Fact。项目恢复为 `active` 后，Dispatcher 会重新读取图状态，再决定是继续 `explore`、进入 `reason`，还是在初始态重新 `bootstrap`。项目若在 `completed` 后被服务端 `reopen`，对 Dispatcher 来说也等价于“重新变成 active 且图上多了一个新 fact”；下一轮会按普通 active 项目继续调度。

Worker 选择规则：

1. 先按任务类型筛选
2. 再过滤掉已达到 `max_running` 的 Worker
3. 再过滤掉处于短暂不可选窗口内的不健康 Worker
4. 在剩余 Worker 中，优先选择 `priority` 更小的
5. 如果 `priority` 相同，则优先选择当前运行中任务数更少的
6. 如果仍然相同，则随机选择
7. 如果是 `explore`，Dispatcher 先通过 `POST /projects/{project_id}/intents/{intent_id}/heartbeat` claim 成功，再真正启动任务
8. 如果是 `reason`，claim 成功后由 `POST /projects/{project_id}/reason/heartbeat` 维持 lease；真正启动前，再对选中的 Worker 执行一次健康检查；如果失败，则本次任务作废；该 Worker 会进入一个短暂不可选窗口，等待后续轮次再尝试

---

## 配置模型

Dispatcher 使用一个运行期配置文件：

- `dispatch.yaml`

也就是说：

- 使用者只需要提供 `dispatch.yaml`
- 任务 prompt 以 markdown 文件形式随代码分发，并通过 `runtime.prompt_group` 选择目录
- Worker 的健康检查、命令模板、session 处理、二阶段收尾能力由对应 driver 实现

代码目录可以采用类似组织：

```text
dispatcher/
  models.py
  prompting.py
  output_parser.py
  contracts.py
  prompts/
    default/
      bootstrap.md
      bootstrap_conclude.md
      reason.md
      explore.md
      explore_conclude.md
    mock/
      bootstrap.md
      bootstrap_conclude.md
      reason.md
      explore.md
      explore_conclude.md
  workers/
    base.py
    registry.py
    adapters/
      claudecode.py
      codex.py
      mock.py
```

本文档附录给出：

- `dispatch.yaml` 的示例内容
- 上述 markdown prompt 的示例内容

---

## 任务模型

### 三类任务一览

| 任务 | 触发条件 | 输入 | 输出 | 超时策略 |
| --- | --- | --- | --- | --- |
| `bootstrap` | 项目 `active`；facts 只有 `origin` 和 `goal`；当前没有普通 intent；允许不存在 bootstrap intent，或只存在保留的 open `bootstrap` intent | `{origin}`、`{goal}`、`{hints}` | 主阶段成功时固定返回 `fact + complete`；收尾阶段只返回 `fact` | 双阶段：`timeout` 后可进入 `conclude_timeout` 收尾；两阶段都失败则 release 保留 intent，下轮仍按新项目重试 |
| `reason` | 项目 `active`；当前项目无未认领 intent；当前项目内无其他 `reason`；首次触发或满足“新态势”重触发条件 | `{graph_yaml}`、`{fact_ids}`、`{open_intents}` | `complete` 对象；或 `intent` 对象；或空 `data` | 仅 `timeout`；超时或非法结果直接作废，不写图 |
| `explore` | 项目 `active`；存在一个当前可认领的未结论 intent | `{graph_yaml}`、`{intent_id}`、`{intent_description}` | 一个 Fact 结论描述 | 单阶段：超时直接作废；双阶段：超时或输出解析失败时可进入 `conclude` 收尾 |

### `bootstrap`

#### 触发条件

- 当前项目仍然是 `active`
- facts 恰好只有 `origin` 和 `goal`
- intents 为空，或只存在保留的 open `bootstrap` intent
- 保留 `bootstrap` intent 的约定固定为：
  - `description = "bootstrap"`
  - `creator = "dispatcher.bootstrap"`
  - `from = ["origin"]`

#### 输入

- `{origin}`
- `{goal}`
- `{hints}`

其中：

- `{hints}` 是 JSON 数组文本，便于 Worker 在项目起始阶段快速吸收策略信息
- `bootstrap` 不读取图 YAML，不依赖普通 intent 图结构

#### 输出契约

`bootstrap` 使用独立输出契约：

- 主阶段只有在已经解决问题时才返回
- 主阶段返回时必须同时包含 `fact` 和 `complete`
- 如果主阶段没有在超时前解决问题，就不会返回合法结果；Dispatcher 会终止它并进入 `bootstrap_conclude`
- `bootstrap_conclude` 只负责收尾总结，因此只返回 `fact`

```json
{
  "accepted": true,
  "data": {
    "fact": {
      "description": "拿到两个 flag，分别为 flag{...} 与 flag{...}；同时获得管理员 shell，权限证明见 /tmp/proofs/root.txt"
    },
    "complete": {
      "description": "已拿到并验证完成 Goal 所需的全部关键结果，Goal 达成"
    }
  }
}
```

约束：

- 主阶段返回时，`data.fact.description` 和 `data.complete.description` 都必须存在
- 主阶段不允许只返回 `fact`
- `bootstrap_conclude` 只允许返回 `fact`，不允许返回 `complete`

#### 接口映射

`bootstrap` 复用普通 intent 协议，但使用保留 intent：

| 接口 | 用途 | 何时使用 |
| --- | --- | --- |
| `POST /projects/{project_id}/intents` | 创建保留 `bootstrap` intent | 初始态项目首次进入时，如尚不存在该 intent |
| `POST /projects/{project_id}/intents/{intent_id}/heartbeat` | claim 并维持 `bootstrap` intent | 派发前先 claim；执行中按 `interval` 周期发送 |
| `POST /projects/{project_id}/intents/{intent_id}/conclude` | 将 `bootstrap` 产出的关键结果写成 Fact | `bootstrap` 或 `bootstrap_conclude` 返回合法 `fact` 后调用 |
| `POST /projects/{project_id}/complete` | 基于刚写入的 bootstrap fact 直接完成项目 | 仅当 `bootstrap` 主阶段成功返回 `fact + complete` 时调用 |
| `POST /projects/{project_id}/intents/{intent_id}/release` | 放弃本次 bootstrap 尝试 | 两阶段都失败，或命令直接失败时调用 |

#### 超时与失败

- `bootstrap` 第一阶段使用 `timeout`
- `bootstrap_conclude` 第二阶段使用 `conclude_timeout`
- 主阶段如果在 `timeout` 内解决问题，就返回 `fact + complete`
- 第一阶段超时、输出解析失败或返回了不满足契约的结果时，如果 Worker 支持 session / conclude，则进入 `bootstrap_conclude`
- `bootstrap_conclude` 的 prompt 必须明确要求“不继续推进，不等待未完成任务，只总结当前最关键事实”，因此它只产出 `fact`
- 如果 `bootstrap` 主阶段成功返回合法 JSON，则 Dispatcher 会先 conclude 写入 fact，再立即 complete
- 如果 `bootstrap_conclude` 成功返回合法 JSON 且 conclude 写回成功，则保留 intent 被结论落定，项目不再视为初始态
- 如果主阶段的 `complete` 写回失败，已写入的 fact 仍然保留，后续可由下一轮 `reason` 再完成项目
- 如果两阶段都失败，或 conclude 写回失败，则 release 当前 `bootstrap` intent，不写 Fact；项目下轮仍然按新项目处理

### `reason`

#### 触发条件

- 当前项目仍然是 `active`
- 当前项目没有未认领 intent
- 当前项目的 `project.reason` 为空，也就是当前没有其他 `reason` lease 正在运行
- 首次触发只发生在“当前没有任何 open intent”时
- 之后只有出现新的态势才重新触发；这里的“新态势”限定为：
  - Fact 数量增加
  - Hint 数量增加
  - 项目从“存在 open intents”进入“没有 open intents”
- 单次 `explore` 失败、掉心跳、释放但 intent 仍保持 open，不构成新的态势，不应触发新的 `reason`

#### 输入

- `{graph_yaml}`
- `{fact_ids}`
- `{open_intents}`

其中：

- `{fact_ids}` 是 JSON 数组文本，用于显式列出当前合法的 Fact id
- `{open_intents}` 是 JSON 数组文本，用于显式列出当前所有未结论的 intent；因此即使有别的 intent 正在 `explore`，只要出现了新的 Fact / Hint，`reason` 仍可能再次被触发
- 这两个占位符都是 prompt 层辅助，不替代 server 的最终校验

#### 输出契约

已完成：

```json
{
  "accepted": true,
  "data": {
    "complete": {
      "from": ["f008"],
      "description": "flag{abc} 满足 goal 要求"
    }
  }
}
```

未完成，提出新 intent：

```json
{
  "accepted": true,
  "data": {
    "intent": {
      "from": ["f003"],
      "description": "尝试 SQL 注入"
    }
  }
}
```

未完成，不提新 intent：

```json
{
  "accepted": true,
  "data": {}
}
```

约束：

- `data.complete` 存在时，它必须是对象，且 `complete.from` 和 `complete.description` 都必须存在
- `data.complete` 存在时，不应再带 `intent`
- 如果 `intent` 存在，则 `intent.from` 和 `intent.description` 都必须存在
- 如果 `{open_intents}` 为空，说明当前图里没有任何进行中的探索；此时若没有 `data.complete`，则必须返回 `intent`
- 如果 `{open_intents}` 非空，且没有 `data.complete`，则允许不返回 `intent`

#### 接口映射

`reason` 启动前，Dispatcher 必须先 claim 项目级 reason lease，并在执行期间持续 heartbeat；这一状态会直接出现在 `GET /projects` 和 `GET /projects/{project_id}` 的 `project.reason` 字段中，供前端和其他消费者观察。

| `reason` 输出 | Dispatcher 动作 | 备注 |
| --- | --- | --- |
| `data.complete` 存在 | 调用 `POST /projects/{project_id}/complete` | `worker` 使用当前执行该任务的 `workers[].name` |
| `data.complete` 不存在，且带 `intent` | 调用 `POST /projects/{project_id}/intents` | `creator` 使用当前 Worker 名；`worker` 固定写 `null` |
| `data` 为空对象 | 不写图 | 不写 Fact / Intent / Complete |

写回失败的日志语义：

- 如果 `POST /projects/{project_id}/complete` 或 `POST /projects/{project_id}/intents` 返回 `403`，通常表示项目已不再是 `active`，本次任务直接作废，记 `info`
- 其他写入失败也直接作废，不做立即重试，只记日志
- 无论本轮是否写图，只要项目仍是 `active` 且 reason lease 仍在自己手里，Dispatcher 都会在收尾时调用 `POST /projects/{project_id}/reason/release`；如果项目已 `completed` 或 `stopped`，则由服务端直接清空该 lease

#### 超时与失败

- `reason` 只使用 `timeout`
- 超时直接作废
- `accepted: false` 直接作废，记 `warn`
- 其他执行错误也直接作废，例如：
  - 命令退出码非 `0`
  - 输出不是合法 JSON
  - JSON 缺少必要字段
- 以上情况都不写 Fact / Intent / Complete，只记日志

### `explore`

#### 触发条件

- 当前项目仍然是 `active`
- 存在一个当前可认领的、尚无结论的 intent

#### 输入

- `{graph_yaml}`
- `{intent_id}`
- `{intent_description}`

#### 输出契约

正常返回：

```json
{
  "accepted": true,
  "data": {
    "description": "发现 /search 参数存在报错注入"
  }
}
```

即使没有打出漏洞，也应返回一个客观探索结论，而不是空响应。例如：

```json
{
  "accepted": true,
  "data": {
    "description": "对 /search 参数测试常见 SQL 注入 payload，未发现可利用注入迹象"
  }
}
```

约束：

- `data.description` 必须存在，且必须是客观事实结论
- 不允许输出“我拒绝帮助渗透”“这不安全”等文本作为 `description`

#### 接口映射

`explore` 会涉及三类协议接口：

| 接口 | 用途 | 何时使用 |
| --- | --- | --- |
| `POST /projects/{project_id}/intents/{intent_id}/heartbeat` | claim 并维持持有 | 派发前先 claim；执行中按 `interval` 周期发送 |
| `POST /projects/{project_id}/intents/{intent_id}/conclude` | 产出 Fact 并结论落定 Intent | `execute` 或 `conclude` 返回合法结论后调用 |
| `POST /projects/{project_id}/intents/{intent_id}/release` | 放弃当前尝试 | 失败路径使用 |

派发顺序要求固定为：

1. Dispatcher 先选中一个可认领 intent
2. Dispatcher 先调用一次 `POST /projects/{project_id}/intents/{intent_id}/heartbeat` 作为 claim
3. 只有 heartbeat 成功后，才真正启动 `explore` 对应的 Worker

#### 超时与失败

`explore` 要兼容两种模式：

1. 单阶段模式：只有 `execute`
2. 双阶段模式：`execute + session + conclude`

其中 `conclude` 是附加收尾阶段，不是主流程必经阶段。

第一阶段使用 `timeout`。
如果是双阶段模式，第二阶段使用 `conclude_timeout`。

正常完成：

- 如果 `execute` 在 `timeout` 内正常返回合法 JSON，且 `accepted: true`
- Dispatcher 直接调用 `POST /projects/{project_id}/intents/{intent_id}/conclude`
- `POST /projects/{project_id}/intents/{intent_id}/conclude` 成功即完成结论落定，无需额外 `release`
- 如果 `POST /projects/{project_id}/intents/{intent_id}/conclude` 写入失败，本次任务直接作废，不做立即重试，释放当前 intent，只记日志

可进入二阶段收尾的异常：

- 这类异常只在“双阶段 Worker”上进入 `conclude`
- 适用异常只有两种：
  - 执行超时
  - Dispatcher 无法从第一阶段输出里正确提取并解析结果，例如：
    - 输出不是合法 JSON
    - JSON 缺少必要字段
    - `accepted: true` 但 `data` 结构不符合当前任务要求
- 这类异常在“单阶段 Worker”上不进入 `conclude`

双阶段收尾流程固定为：

1. Dispatcher 杀掉当前进程
2. 保留这次任务对应的 session id
3. 在保持 heartbeat 的前提下，用同一个 session 直接进入 `conclude`
4. `conclude` 的 prompt 必须明确要求“不要继续探索，只总结截至目前已经完成的探索与结论”
5. 如果 `conclude` 在 `conclude_timeout` 内返回合法 JSON，且 `accepted: true`：
   - Dispatcher 调用 `POST /projects/{project_id}/intents/{intent_id}/conclude`
   - 成功则结束
6. 如果 `conclude` 再次超时，或输出不合法，或返回 `accepted: false`，或 `POST /projects/{project_id}/intents/{intent_id}/conclude` 写入失败：
   - 整次探索作废
   - 不写任何图数据
   - 释放当前 intent
   - 只记日志

单阶段 Worker 的异常处理：

- 如果当前 Worker 不支持 `session` 或 `conclude`，则它属于单阶段模式
- 这时第一阶段一旦出现“超时”或“输出解析 / 结构校验失败”，直接按失败处理
- 处理方式是：杀进程、整次探索作废、不写任何图数据、释放当前 intent、记 `warn`

直接失败，不进入 `conclude`：

- 第一阶段返回 `accepted: false`
- 命令退出码非 `0`
- Worker 进程根本没有产生可读取结果
- Dispatcher 在进入结果解析前就已经确定本次执行失败

以上情况都：

- 不进入 `conclude`
- 不写任何图数据
- 释放当前 intent
- 清理本地任务状态
- 只记日志

---

## 调度策略

### 全局调度

核心规则：

1. 已运行项目优先，但只优先可立即派发的任务
2. 如果某个运行中项目处于初始态且可执行 `bootstrap`，优先继续它
3. 否则如果某个运行中项目存在可执行的 `explore`，优先继续探索它
4. 如果所有运行中项目都暂时没有可派发任务，且 `runtime.max_running_projects` 还有余量，就启动一个未开始的新项目

`runtime.interval` 的设计约定：

- `runtime.interval` 不只是一个普通轮询间隔
- 它被刻意复用为两个地方的统一节拍：
  - Dispatcher 主循环间隔
  - 带 claim 任务（`bootstrap` / `explore`）的 heartbeat 周期
- 这样做的目标是减少额外时序参数，先保持实现简单
- 这是一项明确设计决策，不是偶然耦合

调度伪代码可以保持成下面这种粒度：

```text
for project in running_projects_round_robin:
  if has_dispatchable_bootstrap(project):
    dispatch_bootstrap(project)
    continue
  if has_dispatchable_explore(project):
    dispatch_explore(project)
    continue
  if has_dispatchable_reason(project):
    dispatch_reason(project)
    continue

if running_project_count < runtime.max_running_projects:
  maybe_start_one_new_project()
```

### 项目内调度

对于单个项目，Dispatcher 读完整项目状态后，按下面顺序调度：

1. 如果项目仍处于初始态，优先确保存在且只存在一个保留 `bootstrap` intent，并只调度 `bootstrap`
2. 对非初始态项目，如果存在未认领 intent，优先派发 `explore`
3. 只有在没有未认领 intent 时，才考虑派发 `reason`
4. `reason` 的去重按“态势”做，而不是按总图变化做：首次只有在当前没有任何 open intent 时才触发；之后只有当前 Fact / Hint 数量增加，或项目从“存在 open intents”进入“没有 open intents”时，才重新触发
5. 如果 `reason` 返回 `data.complete`，Dispatcher 调用 `POST /projects/{project_id}/complete`
6. 如果 `reason` 没有返回 `data.complete` 且带 `intent`，Dispatcher 调用 `POST /projects/{project_id}/intents`
7. 如果 `reason` 既没有返回 `data.complete`，也没有返回 `intent`，则本轮不写图

另外：

- 初始态项目里，如果 `bootstrap` intent 已被 claim，则这一轮不再派发 `reason` 或普通 `explore`
- 即使同一项目里已经有进行中的 `explore`，也允许继续派发一个 `reason` 任务
- 但前提不是“刚新增了 intent”，而是“确实出现了新的 Fact / Hint 等新态势”；仅仅因为上一个 `reason` 刚创建了新的 intent，不应该立刻再次 `reason`
- 仍然要求当前没有未认领 intent、当前项目内没有其他 `reason` 任务在运行、且没有超过 `runtime.max_project_workers`

`reason` 的去重规则建议保持简单：

- 记录该项目上次成功完成 `reason` 时的 Fact 数量、Hint 数量，以及当时是否仍存在 open intents
- Dispatcher 对“当前已观察到、且已有 open intents、但尚无 checkpoint 的 active 项目”建立基线 checkpoint；启动时已有项目和运行中晚到项目都适用，不应吞掉运行过程中第一次新增的 Fact / Hint
- 首次没有历史记录且当前没有 open intents 时，直接触发
- 之后只有 Fact / Hint 数量增加，或项目从“有 open intents”变为“无 open intents”时，才再次触发 `reason`
- 不把“总 intent 数量增加”当作重触发条件；因为这通常只是上一次 `reason` 刚创建了新 intent，并不代表出现了新的态势
- `explore` 的执行失败、掉心跳、临时 release 只会让 intent 重新等待被探索，不会额外触发 `reason`

### 并发约束

- 当前设计下，只支持一个 Dispatcher 实例连接同一服务端执行调度
- 如果同时运行多个 Dispatcher，本地维护的 admission、Worker 健康状态、并发计数、容器清理和 bootstrap 去重都不会跨进程协调，因此不属于支持场景
- 单个项目内，同一时刻最多只能有一个 `bootstrap` 任务在运行
- Dispatcher 会尽力让单个项目在初始态时只保留一个 open `bootstrap` intent
- 单个项目内，同一时刻最多只能有一个 `reason` 任务在运行
- 跨项目允许并行运行多个 `reason`
- `reason` 也计入对应项目的 `runtime.max_project_workers`
- `runtime.max_workers`：Dispatcher 同时运行中的任务总数上限
- `runtime.max_running_projects`：当前 dispatcher 运行期内已接手且仍为 `active` 的项目 admission 上限；项目即使暂时没有可派发任务，只要仍为 `active`，也继续占用该名额，直到其退出 active
- `runtime.max_project_workers`：单个项目内同时运行的任务上限，统一计入 `bootstrap`、`reason` 和 `explore`
- `workers[].max_running`：单个 Worker 自身的并发上限；达到上限后，这个 Worker 暂时不再参与派发

---

## Worker 配置

### 字段定义

Dispatcher 固定从 `stdout` 取全文作为模型正文输出。

`dispatch.yaml` 中与 Worker 相关的运行期字段如下：

| 字段 | 含义 | 说明 |
| --- | --- | --- |
| `name` | Worker 静态标识 | 协议写回时作为 `creator` 或 `worker` |
| `type` | Worker driver 名 | 支持 `claudecode`、`codex`、`mock` |
| `task_types` | 支持的任务类型 | `bootstrap`、`reason`、`explore` |
| `max_running` | Worker 并发上限 | 达到上限后暂不派发 |
| `priority` | 选择优先级 | 数字越小越优先 |
| `env` | 运行时环境变量 | 由对应 driver 使用；`mock` 的 phase 耗时和结果概率也通过这里配置 |

系统提供三类 Worker driver：

- `claudecode`
- `codex`
- `mock`

也就是说：

- `dispatch.yaml` 负责声明“用哪个 driver、能跑什么任务、并发多少、环境变量是什么”；如果是 `mock`，各 phase 的模拟分布也放在 `env`
- 具体怎么健康检查、怎么启动命令、怎么提取 session、怎么恢复 `conclude`，都由 driver 代码负责

### Driver 接口

每个 Agent / CLI 工具在代码里对应一个独立 driver 文件，并实现统一接口。

统一接口至少应覆盖这些能力：

- `build_healthcheck(worker)`：构造健康检查命令
- `prepare_session()`：需要时预先生成 session id
- `build_execute(worker, prompt, session)`：构造第一阶段执行命令
- `extract_session(session, stderr)`：需要时从 `stderr` 提取 session id，或继续使用预生成 session
- `build_conclude(worker, prompt, session)`：在双阶段 `explore` 中恢复同一 session 做收尾
- `supports_conclude()`：声明该 driver 是否支持双阶段 `explore`

这些 driver 的能力约定是：

- `claudecode` 支持双阶段 `explore`
- `codex` 支持双阶段 `explore`
- `mock` 支持双阶段 `explore`

并发建模约定：

- 一个 Worker 应代表一个独立的 LLM 并发配额单元
- 不考虑“多个 Worker 共用同一个 key”的情况
- 并发控制使用 `workers[].max_running`

### 健康检查

健康检查由 driver 实现。

目标不是验证“容器活着”，而是验证某个具体 Worker 的 LLM 配置真的可用：

- base URL 可达
- API key 有效
- model 可调用

执行规则：

- 每次准备启动某个 Worker 进程前，都先执行一次健康检查
- `explore` 进入 timeout / parse-fail 后的 `explore_conclude` fallback 不再重复执行健康检查，而是直接尝试 conclude，并继续依赖 JSON / schema 校验决定是否写回
- 退出码 `0` 视为健康
- 非 `0` / 超时（Dispatcher 外层 watchdog 由 `runtime.healthcheck_timeout` 控制）/ 命令起不来，视为不健康
- driver 内部的 HTTP 探活不再自行设置超时，统一由 Dispatcher 外层 watchdog 限制
- 如果这次失败，本次任务直接作废，并将这个 Worker 放入一个短暂不可选窗口
- 窗口结束后，后续轮次再次选择到这个 Worker 时重新检查
- Dispatcher 内部会为最近失败的 Worker 记录一个本地 `retry_after`，在这之前不再派发给它

Dispatcher 只需要看退出码，不需要理解响应体。

### CLI 接入约定

#### `claudecode` driver

依赖环境变量：

- `ANTHROPIC_MODEL`
- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_AUTH_TOKEN`

健康检查可在 driver 内部按等价方式实现：

```bash
curl -sS --fail -o /dev/null \
  "$ANTHROPIC_BASE_URL/v1/messages" \
  -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  -H "content-type: application/json" \
  -d "{\"model\":\"$ANTHROPIC_MODEL\",\"max_tokens\":10,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

已知行为：

- driver 预先生成 session id
- 首轮可以预先指定 session id
- 如果该 id 已存在，命令会报错，不会复用旧会话
- Dispatcher 固定从 `stdout` 取全文作为结果正文

第一阶段执行：

```bash
claude --session-id "{session}" --dangerously-skip-permissions -p -- "{prompt}"
```

二阶段收尾：

```bash
claude -r "{session}" --dangerously-skip-permissions -p -- "{prompt}"
```

#### `codex` driver

依赖环境变量：

- `CODEX_MODEL`
- `CODEX_BASE_URL`
- `OPENAI_API_KEY`

健康检查可在 driver 内部按等价方式实现：

```bash
curl -sS --fail -o /dev/null \
  "$CODEX_BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "content-type: application/json" \
  -d "{\"model\":\"$CODEX_MODEL\",\"max_tokens\":10,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

已知行为：

- 首轮 session id 会打印在 `stderr`
- 可以用正则 `session id:\s*([0-9a-fA-F-]+)` 提取
- Dispatcher 固定从 `stdout` 取全文作为结果正文

第一阶段执行：

```bash
codex exec --dangerously-bypass-approvals-and-sandbox --model "{env.CODEX_MODEL}" \
  -c 'model_provider="cairn"' \
  -c 'model_providers.cairn.name="cairn"' \
  -c 'model_providers.cairn.wire_api="responses"' \
  -c 'model_reasoning_effort="high"' \
  -c 'model_providers.cairn.base_url="{env.CODEX_BASE_URL}"' \
  -c 'model_providers.cairn.env_key="OPENAI_API_KEY"' \
  -- "{prompt}"
```

二阶段收尾：

```bash
codex exec resume "{session}" --dangerously-bypass-approvals-and-sandbox --model "{env.CODEX_MODEL}" \
  -c 'model_provider="cairn"' \
  -c 'model_providers.cairn.name="cairn"' \
  -c 'model_providers.cairn.wire_api="responses"' \
  -c 'model_reasoning_effort="high"' \
  -c 'model_providers.cairn.base_url="{env.CODEX_BASE_URL}"' \
  -c 'model_providers.cairn.env_key="OPENAI_API_KEY"' \
  -- "{prompt}"
```

#### `mock` driver

`mock` driver 用于本地观察 dispatcher 的成功、失败和超时路径。

行为约定：

- `runtime.prompt_group: "mock"` 时，prompt 本身是结构化 JSON，不再依赖自然语言说明
- `reason` prompt 最少包含 `phase`、`fact_ids`、`open_intents`
- `explore` / `conclude` prompt 最少包含 `phase`、`intent_id`
- `bootstrap` / `bootstrap_conclude` prompt 最少包含 `phase`、`origin`、`goal`、`hints`
- driver 会先解析 prompt 里的 `phase` 字段，再读取对应的 `MOCK_<PHASE>` JSON 环境变量选择当前 phase 的模拟结果
- 每个 phase 都在自己的 JSON 里配置 `delay: [min, max]`；单位是秒，支持小数；随机耗时超过 Dispatcher 外层 timeout 时，就会自然表现为超时
- `reason.noop` 只会在 `open_intents` 非空时参与抽样；mock 会自动避开当前上下文下不合法的结果

`mock` 支持六个 phase：

- `healthcheck`
- `bootstrap`
- `bootstrap_conclude`
- `reason`
- `explore_execute`
- `explore_conclude`

支持的结果如下：

- `healthcheck.outcomes`: `ok`、`fail`
- `bootstrap.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `bootstrap_conclude.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `reason.outcomes`: `complete`、`intent`、`noop`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `explore_execute.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`
- `explore_conclude.outcomes`: `fact`、`rejected`、`invalid_json`、`invalid_payload`、`command_fail`

命名约定：

- 每个 phase 一个变量：`MOCK_<PHASE>`，例如 `MOCK_REASON`
- 变量值必须是 JSON 对象，结构为：`{"delay":[min,max],"outcomes":{...}}`
- `delay` 必须是两个非负数字，单位是秒
- 每个 phase 的所有结果概率都使用 `0` 到 `1` 的小数，并且总和必须严格等于 `1.0`

### 配置校验规则

#### 加载时校验

启动 Dispatcher 时，应完成静态校验：

- `dispatch.yaml` 必须存在且可读取
- `runtime.max_workers` 必须存在
- `runtime.max_running_projects` 必须存在
- `runtime.max_project_workers` 必须存在
- `runtime.interval` 必须存在
- `runtime.healthcheck_timeout` 必须存在
- `runtime.prompt_group` 必须存在
- `tasks.bootstrap.timeout` 必须存在
- `tasks.bootstrap.conclude_timeout` 必须存在
- `tasks.reason.timeout` 必须存在
- `tasks.explore.timeout` 必须存在
- `tasks.explore.conclude_timeout` 必须存在
- 每个 Worker 都必须有 `type`
- 每个 Worker 都必须有 `max_running`
- `task_types` 只允许 `bootstrap`、`reason`、`explore`
- `type` 只允许 `claudecode`、`codex`、`mock`
- `max_running` 必须是正整数
- `claudecode`、`codex`、`mock` 都支持双阶段 `explore`
- `runtime.prompt_group` 对应的 prompt 目录必须存在
- 代码工程中的 prompt 资源必须存在
- 默认 prompt 组下，`reason.md` 必须至少覆盖 `{graph_yaml}`、`{fact_ids}`、`{open_intents}`
- 默认 prompt 组下，`explore.md` 必须至少覆盖 `{graph_yaml}`、`{intent_id}`、`{intent_description}`
- 默认 prompt 组下，`bootstrap.md` 和 `bootstrap_conclude.md` 必须至少覆盖 `{origin}`、`{goal}`、`{hints}`
- `mock` prompt 组下，`reason.md` 必须至少覆盖 `{fact_ids}`、`{open_intents}`
- `mock` prompt 组下，`explore.md` 和 `explore_conclude.md` 必须至少覆盖 `{intent_id}`
- `mock` prompt 组下，`bootstrap.md` 和 `bootstrap_conclude.md` 必须至少覆盖 `{origin}`、`{goal}`、`{hints}`
- `mock` worker 的 `MOCK_*` 变量名只能使用系统支持的 phase
- `mock` worker 每个 phase 的概率都必须在 `0` 到 `1` 之间，且总和必须严格等于 `1.0`

#### 运行时校验

任务真正派发时，还需要做运行时校验：

- driver 必须存在且支持该任务类型
- 真正启动任务前，driver 的健康检查必须能成功执行；退出码 `0` 才算健康
- `explore` 进入 timeout / parse-fail 后的 `explore_conclude` fallback，不再重复执行健康检查，而是直接尝试 conclude 并继续走结果校验
- 只有支持当前任务类型的 Worker 才能被选中
- 只有当前运行中任务数小于 `max_running` 的 Worker 才能被选中
- 处于本地 `retry_after` 窗口内的不健康 Worker 不参与派发
- `bootstrap` 和 `explore` 都必须先完成 claim，成功后才真正启动任务线程
- 如果要走双阶段 `explore`，则第一阶段必须成功拿到 session id，才能进入 `conclude`
- 如果要走双阶段 `bootstrap`，则第一阶段必须成功拿到 session id，才能进入 `bootstrap_conclude`
- Dispatcher 必须对 `stdout` 全文做 JSON 解析和任务级结构校验
- `accepted: false`、JSON 非法、字段缺失、接口写回失败等情况都必须记日志，且不做立即重试

---

## 配置字段速查

### `dispatch.yaml`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `server` | 是 | Cairn Server 的 base URL |

### `runtime.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `runtime.max_workers` | 是 | Dispatcher 同时运行中的任务总数上限 |
| `runtime.max_running_projects` | 是 | 当前 dispatcher 运行期内已接手且仍为 `active` 的项目上限，不受历史遗留容器影响 |
| `runtime.max_project_workers` | 是 | 单个项目内同时运行的任务上限，统一计入 `bootstrap`、`reason` 和 `explore` |
| `runtime.interval` | 是 | 统一节拍配置；既是 Dispatcher 主循环间隔，也是带 claim 任务的 heartbeat 周期 |
| `runtime.healthcheck_timeout` | 是 | Worker 健康检查的统一外层 watchdog 超时 |
| `runtime.prompt_group` | 是 | 当前使用的 prompt 组目录名 |

### `container.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `container.image` | 是 | 项目容器镜像 |
| `container.network_mode` | 是 | 项目容器网络模式 |
| `container.completed_action` | 是 | 项目 completed 后对容器的处理方式 |

`container.completed_action` 可选值：

- `remove`：项目 completed 后删除容器
- `stop`：项目 completed 后只停止容器，保留现场

实现约定：

- completed project 的容器 cleanup 可以异步并行进行，不要求阻塞主调度循环
- 如果项目已从 Server 删除，Dispatcher 会把找不到对应项目的 `cairn-dispatch-*` 容器视为 orphan，并执行 stop 清理

### `tasks.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `tasks.bootstrap.timeout` | 是 | `bootstrap` 第一阶段超时 |
| `tasks.bootstrap.conclude_timeout` | 是 | `bootstrap` 双阶段收尾超时 |
| `tasks.reason.timeout` | 是 | `reason` 的超时 |
| `tasks.explore.timeout` | 是 | `explore` 第一阶段超时 |
| `tasks.explore.conclude_timeout` | 是 | `explore` 双阶段收尾超时 |

### `workers.*`

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `name` | 是 | Worker 静态标识；协议写回时使用这个值作为 `creator` 或 `worker` |
| `type` | 是 | Worker driver 名；支持 `claudecode`、`codex`、`mock` |
| `task_types` | 是 | 该 Worker 支持的任务类型列表 |
| `max_running` | 是 | 该 Worker 自身的并发上限 |
| `priority` | 是 | 当前任务类型的候选 Worker 中，数字越小优先级越高 |
| `env` | 是 | 该 Worker 的变量表；具体必需 key 由对应 driver 决定并在启动时校验 |

补充：

- Worker 选择顺序是：先过滤任务类型、`max_running` 和处于本地 `retry_after` 窗口内的 Worker，再按 `priority`，同优先级优先选当前运行数更少的，最后随机；`bootstrap` 和 `explore` 都会先 claim，再启动任务；真正启动前会做一次健康检查，失败的 Worker 会进入短暂不可选窗口；进入 `bootstrap_conclude` / `explore_conclude` fallback 时不再重复健康检查
- 健康检查、执行命令、session 提取、二阶段 `conclude` 都由对应 driver 代码负责
- prompt 内容从代码工程里的 markdown 资源加载

---

## 附录：示例配置与 Prompt 内容

### `dispatch.yaml`

```yaml
server: "http://127.0.0.1:8000"

runtime:
  max_workers: 5  # total running tasks
  max_running_projects: 3  # total active projects admitted by this dispatcher runtime
  max_project_workers: 2  # per-project running tasks, including bootstrap + reason + explore
  interval: 3  # intentional shared cadence: scheduler loop interval + claim-task heartbeat interval, in seconds
  healthcheck_timeout: 15  # shared watchdog for all worker healthchecks, in seconds
  prompt_group: "default"  # selects prompts/<group>/

tasks:
  bootstrap:
    timeout: 120
    conclude_timeout: 30
  reason:
    timeout: 45
  explore:
    timeout: 600
    conclude_timeout: 120

container:
  image: "tmp:latest"
  network_mode: "host"
  completed_action: "stop"  # options: "remove" | "stop"

common_env:
  TSEC_BASE_URL: "http://<SERVER_HOST>/api"
  TSEC_AGENT_TOKEN: "..."

workers:
  # 同一模型拆成多个 Worker 的原因，应是它们使用不同的 API key，
  # 从而拥有彼此独立的并发配额。
  - name: "claude-sonnet-thinker"
    type: "claudecode"
    task_types: [bootstrap, reason]
    max_running: 1
    priority: 0  # lower number wins; ties prefer fewer running tasks, then choose randomly
    env:
      ANTHROPIC_MODEL: "claude-sonnet-4-6"
      ANTHROPIC_BASE_URL: "https://api.example.com"
      ANTHROPIC_AUTH_TOKEN: "sk-ant-worker-a"

  - name: "claude-sonnet-doer"
    type: "claudecode"
    task_types: [bootstrap, explore]
    max_running: 1
    priority: 1
    env:
      ANTHROPIC_MODEL: "claude-sonnet-4-6"
      ANTHROPIC_BASE_URL: "https://api.example.com"
      ANTHROPIC_AUTH_TOKEN: "sk-ant-worker-b"

  - name: "codex-gpt54"
    type: "codex"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 3
    env:
      CODEX_MODEL: "gpt-5.4"
      CODEX_BASE_URL: "https://api.example.com/v1"
      OPENAI_API_KEY: "sk-worker-c"

  - name: "codex-gpt54-alt"
    type: "codex"
    task_types: [bootstrap, explore]
    max_running: 1
    priority: 4
    env:
      CODEX_MODEL: "gpt-5.4"
      CODEX_BASE_URL: "https://api.example.com/v1"
      OPENAI_API_KEY: "sk-worker-d"

  - name: "mock-observer"
    type: "mock"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 9
    env:
      MOCK_HEALTHCHECK: '{"delay":[0.05,0.15],"outcomes":{"ok":0.9,"fail":0.1}}'
      MOCK_BOOTSTRAP: '{"delay":[0.1,12.0],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_BOOTSTRAP_CONCLUDE: '{"delay":[0.1,2.2],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_REASON: '{"delay":[0.1,2.2],"outcomes":{"complete":0.1,"intent":0.3,"noop":0.1,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.2}}'
      MOCK_EXPLORE_EXECUTE: '{"delay":[0.1,12.0],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
      MOCK_EXPLORE_CONCLUDE: '{"delay":[0.1,2.2],"outcomes":{"fact":0.6,"rejected":0.1,"invalid_json":0.1,"invalid_payload":0.1,"command_fail":0.1}}'
```

补充：

- 这里只是用示例值表达配置结构
- `common_env` 会先并到每个 worker 的环境变量里，然后再被 `worker.env` 覆盖；即 `common_env < worker.env`
- Worker 按独立的 key / model 并发配额单元建模，不应让多个 Worker 共享同一个 key

下面 5 份 markdown 内容对应代码工程里的 prompt 文件。

### `bootstrap.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你需要直接解决这个问题，目标是完成 Goal。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```
## 上下文
### Origin
{origin}
### Goal
{goal}
### Hints JSON 数组
{hints}
````

### `reason.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你当前只做 `reason`。
你要同时判断两件事：
1. 现有 facts 是否已经满足 goal。
2. 如果还未满足，当前是否需要提出一个新的 intent。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
拒绝任务时返回：
```json
{"accepted": false, "reason": "..."}
```
已满足 goal 时返回：
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```
未满足 goal，但需要提出新 intent 时返回：
```json
{"accepted": true, "data": {"intent": {"from": ["f001"], "description": "..."}}}
```
未满足 goal，且当前不需要提出新 intent 时返回：
```json
{"accepted": true, "data": {}}
```
## 规则
- 如果下面的 `open_intents` 为空，说明当前图里没有任何进行中的探索；此时若不返回 `data.complete`，则必须返回 `intent`。
- `intent.from` 只能从下面的合法 fact id 中选择。
## 上下文
### 图快照
{graph_yaml}
### 当前合法的 fact id JSON 数组
{fact_ids}
### 当前所有未结论的 intent JSON 数组
{open_intents}
````

### `explore.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你当前只做 `explore`。
你只处理当前这一条 intent，执行探索并给出最终事实结论。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
拒绝任务时返回：
```json
{"accepted": false, "reason": "policy_refusal"}
```
正常返回示例：
```json
{"accepted": true, "data": {"description": "..."}}
```
## 规则
- `description` 必须是客观探索结论，不要输出解释性废话。
## 上下文
### 图快照
{graph_yaml}
### 当前 intent id
{intent_id}
### 当前 intent 描述
{intent_description}
````

### `explore_conclude.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
你当前正在对同一个 `explore` 做收尾总结。
- 不要继续探索。
- 只总结截至目前已经完成的探索和结论。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
拒绝任务时返回：
```json
{"accepted": false, "reason": "policy_refusal"}
```
正常返回示例：
```json
{"accepted": true, "data": {"description": "..."}}
```
## 规则
- `description` 必须是客观探索结论，不要输出解释性废话。
## 上下文
### 图快照
{graph_yaml}
### 当前 intent id
{intent_id}
### 当前 intent 描述
{intent_description}
````

### `bootstrap_conclude.md`

````md
# 背景
当前场景是授权的 AI 渗透测试比赛 / 靶场环境。
## 任务
- 不要继续推进。
- 不要等待未完成的任务。
- 只总结截至目前已经确认、且对达到 goal 最有帮助的关键事实。
## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```
## 上下文
### Origin
{origin}
### Goal
{goal}
### Hints JSON 数组
{hints}
````
