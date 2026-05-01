# Cairn 协作探索协议

---

## 本质

衍迹（Cairn）将目标导向的探索过程建模为一张有向无环图：从已知事实出发，不断探索产出新事实，直到某些事实共同满足终点。名称取自登山者用石头垒起的路标——每个 Fact 是一块石头，Agent 沿着前人的 cairn 继续探索，同时垒新的 cairn 给后来者。

这套 API 管理这张图的生长过程。多个消费者（人或 Agent）并发读取完整图，各自声明探索意图，各自产出新事实，最终由某个消费者判断终点已达成。对于一些很简单的问题，消费者也可以在项目最初阶段直接围绕起点和终点做一次快速推进，而不必先拆成多轮显式规划；这种“起步即做”的过程仍通过 Intent/Fact 正常落图。

图仍然是知识与因果关系的唯一表达；但协议也允许少量**项目级协调状态**，用于消费者之间做互斥和可见性协作。这类状态不是事实，不属于图，不参与因果推理。当前唯一内置的项目级协调状态是 `Project.reason`，用于标识“某个消费者正在对整个项目做一次 `reason` 判断”。

系统不做任何推理和决策，只负责图的一致性维护。

### 设计渊源

这套协议是**黑板架构**（Blackboard Architecture）的现代化重构。1970 年代 CMU 的 Hearsay-II 系统提出了同样的范式：多个专家围绕一块共享黑板工作，各自读取当前状态，各自贡献新知识，没有中央调度。Fact 对应黑板上的内容，Intent 对应专家的行动记录，Hint 对应旁注。区别在于：经典黑板只有"当前状态"，本协议通过 Intent 保留了完整的因果链，使得图同时是知识库和推理路径的审计日志。

协调机制上，这套设计等价于蚁群的**信息素机制**（stigmergy）——个体之间不直接通信，而是通过改变共享环境间接协调。Agent 读图、写 Fact，就是在环境上留下信息素，其他 Agent 据此决策。

每个消费者的工作循环（读图→判断→声明意图→执行→产出事实）构成一个独立的 **OODA 循环**（Observe-Orient-Decide-Act），多个循环通过共享图间接同步。这与军事指挥中的**任务式指挥**（Mission Command）同构——上级只下达目标（goal），各单位根据态势自主决策，Hint 扮演"指挥官意图"的角色。核心优势：决策速度不受中心调度瓶颈限制。

### 状态变化的表达

Fact 只增不改，状态变化通过追加新 Fact 表达。例如在多层网络渗透中：

- `f003`: "获得 host-A 的 root shell"
- `f007`: "通过 host-A 跳板发现内网 10.0.2.0/24"
- `f025`: "host-A shell 已断开"

"shell 断了"本身就是一个新的客观事实。Agent 读图时看到 f003 和 f025，自行判断当前可达性，从 f025 出发声明新的探索意图（重建 shell、换跳板）。不需要修改历史 Fact，不需要标记失效——事实的时序本身就携带了状态变化的信息。

---

## 核心概念

### Project

一个有明确起点和终点的问题实例，包含完整的图数据。

三种状态：

- `active`：进行中，接受探索写操作
- `stopped`：硬停止，不接受探索写操作，可恢复为 `active`
- `completed`：当前已完成，不接受探索写操作；若外部确认之前的完成判断有误，可显式调用 `reopen` 撤销完成态并继续

### Fact

图中的节点，代表一个已确认的客观事实。只有描述文本，没有状态标记。只增不改，永久保留。

描述文本应是探索结论的提炼，而非原始数据。当原始输出较大时（如扫描结果），描述应包含关键洞见和原始数据的文件引用，例如："nmap 全端口扫描发现 22/80/443/8080 开放，80 和 8080 均为 HTTP 服务，详细结果见 /tmp/scans/nmap_192.168.1.10.xml"。这保证图保持轻量，同时原始数据仍可追溯。

每个 Project 有两个特殊 Fact，在 Project 创建时写入：

- `origin`：起点
- `goal`：终点

普通 Fact 由系统生成 id，如 `f001`。

### Intent

图中的边，代表从一个或多个 Fact 出发的探索过程。有三种状态：

- **进行中**：Intent 已被某个消费者认领，`worker` 不为 null
- **未认领**：Intent 尚无结论，且当前 `worker` 为 null，等待消费者接手
- **已结论**：`to` 不为 null，探索已完成，产出了结论 Fact

未结论的 Intent（`to=null`）表示这个探索方向已声明但尚未有结论。声明时可只写下方向而不认领，也可同时认领。已结论的 Intent 不可修改。

协议本身不区分 Intent 的“类型”，所有 Intent 在服务端都一视同仁；但消费者可以约定某些**保留语义**。例如可约定一条：

- `from = ["origin"]`
- `description = "bootstrap"`

的 open Intent 表示“项目最初阶段的一次直接推进尝试”。这类保留 intent 仍然走同样的 heartbeat / release / conclude 生命周期，不需要额外接口；区别只在于消费者会把它解释为“先自己分析并执行，先把关键证明性事实落图；若该事实已足以满足 goal，再继续声明 complete”，而不是普通的细分探索方向。

这个约定适合那些可能很快完成、或至少很快得到首个关键突破的问题。这样项目在前端和审计记录中仍然是可见、可理解的：读者能明确看到系统先做过一次 `bootstrap` 尝试，产出了什么关键事实；像 flag、shell、权限证明这类对后续系统有价值的结果，不会因为直接完成项目而绕开事实图。

**超边语义：** `from` 允许多个 Fact id。`from` 包含多个 id 时，这条 Intent 在图中等价于一条超边——多个源节点共同指向同一个目标节点，共享同一个 description、creator 和 worker：

```
f002 ──┐
       ├──(intent)──→ f006
f004 ──┘
```

这完整保留了"多个已知事实共同支撑一次探索"的因果关系，而不是被迫选一个主 Fact 丢失其余上下文。

### Hint

图外输入，策略建议或补充说明。不属于事实图，不影响因果关系，供消费者读图时参考。

Hint 代表外部补充的高纬输入，不属于探索执行本身。因此项目处于 `stopped` 或 `completed` 时，仍允许继续补充 Hint。`stopped` 项目恢复为 `active` 后，后续消费者会在下一次读图时看到这些新增提示；`completed` 项目上的新 Hint 会被保留，若之后需要继续处理该项目，仍应通过 `reopen` 回到 `active`。

除了人工补充的策略提示，Hint 也适合用于消费者之间传递**态势评估**。随着图增长，大量已排除的方向会稀释有效信息。消费者在完成一轮探索后，可将对当前局势的判断写为 Hint（如"SSH 和 SQL 注入方向均已排除，攻击面集中在文件上传功能"），帮助后续消费者快速定位重点，降低每轮读图的认知成本。

---

## 数据结构

### Project

```
id
title           # 项目名称
status          # "active" | "stopped" | "completed"
created_at
reason          # 当前项目级 reason lease，null 表示当前无人执行 reason
```

### Project.reason

```
worker             # 当前执行 reason 的消费者
trigger            # 本次 reason 的触发原因，由消费者定义
started_at         # 本次 reason 开始时间
last_heartbeat_at  # 最近一次 reason 心跳时间
```

### Fact

```
id              # "origin" | "goal" | 系统生成（如 f001）
description     # 客观事实描述
```

### Intent

```
id
from            # 出发 Fact 的 id 数组，至少一个
to              # 结论 Fact 的 id，null 表示尚无结论
description     # 意图描述
creator         # 声明意图的消费者，创建时写入，不可变
worker          # 执行者标识，语义随状态变化（见下方说明）
last_heartbeat_at  # 最后一次心跳时间，仅尚无结论时有意义，永久保留，不清空
created_at
concluded_at    # 结论时间，null 表示尚无结论
```

`creator` 在创建时写入，标识谁洞察局势提出了这个探索方向，不可变。`worker` 可为空；若创建时同时认领，则 `worker` 必须等于 `creator`，并写入首个 `last_heartbeat_at`。

请求体里的文本字段（如 `title`、`content`、`description`、`creator`、`worker`、`trigger`）以及 `from` 中的 Fact id，服务端都会先做首尾空白裁剪；裁剪后若为空，则返回 `422`。

如果消费者采用 `bootstrap` 约定，常见做法是使用固定 `creator`，例如 `dispatcher.bootstrap`，以便在图上与普通探索 intent 清晰区分；这只是消费者约定，不是服务端约束。

**worker 字段语义：**

| Intent 状态           | worker 含义                      |
| --------------------- | -------------------------------- |
| 尚无结论，worker=null | 当前无人处理，等待认领           |
| 尚无结论，worker 有值 | 有消费者正在处理                 |
| 尚无结论，worker 超时 | worker 清空为 null，等待重新认领 |
| 已结论                | 产出结论的消费者，永久保留       |

超时清空 worker 只针对尚无结论的 Intent。已结论的 Intent 不参与超时逻辑，worker 永久保留为产出结论的消费者标识。

### Hint

```
id
content
creator         # 提示的作者
created_at
```

### Settings

```
intent_timeout  # 单位秒，超过此时间无心跳则 worker 清空为 null
reason_timeout  # 单位秒，超过此时间无心跳则 Project.reason 清空为 null
```

---

## 接口列表

### 全局设置

#### GET /settings

返回全局配置。

响应：

```json
{
  "intent_timeout": 5,
  "reason_timeout": 5
}
```

---

#### PUT /settings

更新全局配置。

Body：

```json
{
  "intent_timeout": 5,
  "reason_timeout": 5
}
```

---

### Projects

#### GET /projects

返回所有项目列表，含元信息和统计摘要，不含图数据。

响应：

```json
[
  {
    "id": "proj_001",
    "title": "xx渗透测试",
    "status": "active",
    "created_at": "2026-03-21T10:00:00Z",
    "reason": {
      "worker": "dispatcher-worker-A",
      "trigger": "new_facts",
      "started_at": "2026-03-21T10:06:00Z",
      "last_heartbeat_at": "2026-03-21T10:06:03Z"
    },
    "fact_count": 8,
    "intent_count": 5,
    "working_intent_count": 2,
    "unclaimed_intent_count": 1,
    "hint_count": 3
  }
]
```

---

#### POST /projects

创建新项目。`origin` 和 `goal` 写入 `facts` 作为特殊 Fact。`hints` 可选。

Body：

```json
{
  "title": "xx渗透测试",
  "origin": "目标 http://192.168.1.10",
  "goal": "拿到 flag",
  "hints": [
    { "content": "优先看 web 服务", "creator": "human" },
    { "content": "注意 80 端口", "creator": "human" }
  ]
}
```

---

#### GET /projects/{project_id}

返回项目完整数据，包含 facts、intents、hints。

响应：

```json
{
  "project": {
    "id": "proj_001",
    "title": "xx渗透测试",
    "status": "active",
    "created_at": "2026-03-21T10:00:00Z",
    "reason": {
      "worker": "dispatcher-worker-A",
      "trigger": "new_facts",
      "started_at": "2026-03-21T10:06:00Z",
      "last_heartbeat_at": "2026-03-21T10:06:03Z"
    }
  },
  "facts": [
    { "id": "origin", "description": "目标 http://192.168.1.10" },
    { "id": "goal",   "description": "拿到 flag" },
    { "id": "f001",   "description": "发现开放端口 22/80/443" }
  ],
  "intents": [
    {
      "id": "i001",
      "from": ["origin"],
      "to": "f001",
      "description": "nmap 全端口扫描",
      "creator": "agent-A",
      "worker": "agent-A",
      "last_heartbeat_at": "2026-03-21T10:02:00Z",
      "created_at": "2026-03-21T10:01:00Z",
      "concluded_at": "2026-03-21T10:02:00Z"
    },
    {
      "id": "i002",
      "from": ["f001"],
      "to": null,
      "description": "http 服务探测与目录扫描",
      "creator": "agent-A",
      "worker": "agent-B",
      "last_heartbeat_at": "2026-03-21T10:04:00Z",
      "created_at": "2026-03-21T10:03:00Z",
      "concluded_at": null
    }
  ],
  "hints": [
    { "id": "h001", "content": "优先看 web 服务", "creator": "human", "created_at": "2026-03-21T10:00:00Z" },
    { "id": "h002", "content": "注意 80 端口", "creator": "human", "created_at": "2026-03-21T10:00:00Z" }
  ]
}
```

---

#### DELETE /projects/{project_id}

删除项目及其所有数据。

如果某个 Dispatcher 仍持有该项目对应的运行容器，容器不由 Server 直接管理；Dispatcher 在后续轮询中发现项目已不存在后，会把该项目视为 `deleted`，取消本地仍在运行的任务，并删除对应的 orphan 容器。

---

#### PUT /projects/{project_id}/title

修改项目标题。该操作不改变项目图，也不影响 `reason` lease、open intent claim 或项目状态；无论项目当前是 `active`、`stopped` 还是 `completed`，都允许修改标题。

Body：

```json
{
  "title": "xx渗透测试（复盘）"
}
```

响应：

```json
{
  "id": "proj_001",
  "title": "xx渗透测试（复盘）",
  "status": "completed",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": null
}
```

---

#### PUT /projects/{project_id}/status

更新项目状态。仅允许 `active` 和 `stopped` 之间切换。`completed` 项目不可通过该接口再次变更状态；若需要撤销完成态，必须调用专用的 `reopen` 接口。该接口属于项目管理操作，不属于探索写操作。

当项目切到 `stopped` 时，Server 会立即把所有尚无结论的 Intent 的 `worker` 清空为 `null`，并把 `project.reason` 清空，使这些 claim 立刻失效。这样项目恢复后可以马上重新认领，不必等待超时。`stopped` 的语义是硬停止：Server 负责拒绝后续探索写操作并清空 open intent claim / reason lease；消费者拿到这个信号后应立刻取消本地仍在运行的任务，并停止对应项目容器。

已知问题：当前协议里，Intent 级只保存“当前 claim 持有者”这一份信息，也就是 `intent.worker`。因此项目一旦被切到 `stopped`，这些 open intent 的 `worker` 会被立即清空；停止后从项目详情里将无法直接看出“该 intent 在停止前最后是由哪个 worker 在推进”。后续可以考虑增加类似 `worker_history` 的 Intent 级历史字段来保留这部分可见性，但当前版本尚未实现。

Body：

```json
{
  "status": "stopped"
}
```

响应：

```json
{
  "id": "proj_001",
  "title": "xx渗透测试",
  "status": "stopped",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": null
}
```

---

#### POST /projects/{project_id}/reason/claim

认领项目级 `reason` lease。它不属于图，不产生 Intent/Fact，只表示“当前有人正在对整个项目做一次 `reason` 判断”。单个项目同一时刻最多只能有一个 `reason` lease。

若当前 `project.reason` 为空，则 claim 成功并写入 `worker`、`trigger`、`started_at`、`last_heartbeat_at`。若当前已被其他消费者占用且未超时，返回 409。若当前已被同一 `worker` 占用，则幂等返回当前状态。该接口属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "worker": "dispatcher-worker-A",
  "trigger": "new_facts"
}
```

响应：

```json
{
  "id": "proj_001",
  "title": "xx渗透测试",
  "status": "active",
  "created_at": "2026-03-21T10:00:00Z",
  "reason": {
    "worker": "dispatcher-worker-A",
    "trigger": "new_facts",
    "started_at": "2026-03-21T10:06:00Z",
    "last_heartbeat_at": "2026-03-21T10:06:00Z"
  }
}
```

---

#### POST /projects/{project_id}/reason/heartbeat

当前 `worker` 对项目级 `reason` lease 续约，更新 `last_heartbeat_at`。仅当前持有该 lease 的 `worker` 可以续约；若当前无人持有，返回 409；若被其他消费者占用且未超时，返回 409。该接口属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "worker": "dispatcher-worker-A"
}
```

---

#### POST /projects/{project_id}/reason/release

当前 `worker` 主动释放项目级 `reason` lease，使其立刻回到无人执行 `reason` 的状态。仅当前持有者本人可以释放；若当前已为空，则幂等返回当前状态；若被其他消费者占用且未超时，返回 409。该接口属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "worker": "dispatcher-worker-A"
}
```

---

### Hints

#### POST /projects/{project_id}/hints

向项目补充一条策略提示。Hint 属于图外输入写操作，`active`、`stopped` 和 `completed` 项目均允许。

Body：

```json
{
  "content": "尝试文件上传绕过",
  "creator": "agent-A"
}
```

---

### Intents

#### POST /projects/{project_id}/intents

从某个 Fact 出发声明探索意图，产生一条尚无结论的 Intent。可选择只声明，或声明并同时认领。Intent 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "from": ["f001"],
  "description": "http 服务探测与目录扫描",
  "creator": "agent-A",
  "worker": null
}
```

`from` 为数组，至少包含一个 Fact id。`worker` 可为 `null` 或等于 `creator`；`null` 表示只声明不认领，等于 `creator` 表示声明并立即认领。多个 Fact 共同驱动同一次探索时，全部列入 `from`，完整保留因果关系。例如：

`from` 不能包含 `goal`。

服务端不校验 `description` 的业务语义，因此像 `description = "bootstrap"` 这样的保留 intent 也只是普通 Intent。是否把它解释为“项目最初阶段的直接推进尝试”，由消费者自己约定。

```json
{
  "from": ["f002", "f004"],
  "description": "用 f004 获取的凭据登录 f002 发现的后台",
  "creator": "agent-A",
  "worker": "agent-A"
}
```

响应：新创建的 Intent 对象。

---

#### POST /projects/{project_id}/intents/{intent_id}/heartbeat

消费者上报存活，更新 `worker` 和 `last_heartbeat_at`。仅对尚无结论的 Intent 有效。该接口用于续约，或在某条 Intent 已超时变为未认领后重新接手。若当前 `worker` 为 `null`，任意消费者可通过 heartbeat 认领；若当前 `worker` 已被其他消费者占用且未超时，返回 409。Heartbeat 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

消费者通常会按固定心跳周期继续 heartbeat 或轮询项目；项目进入 `stopped` 或 `completed` 后，继续 heartbeat 会返回 403，而轮询读取会看到项目 `status` 变化。由于 Server 在 `stopped` 时会立即清空 open intent 的 `worker`，消费者应把它当作硬停止信号，尽快终止本地推进，而不是继续依赖旧 claim。

Body：

```json
{
  "worker": "agent-B"
}
```

---

#### POST /projects/{project_id}/intents/{intent_id}/release

当前 `worker` 主动释放一条尚无结论的 Intent，使其回到未认领状态，供其他消费者立即接手。仅当前 `worker` 本人可以释放；若被其他消费者占用且未超时，返回 409。若该 Intent 当前已未认领，则幂等返回当前状态。`last_heartbeat_at` 保留，不清空。Release 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

Body：

```json
{
  "worker": "agent-B"
}
```

响应：更新后的 Intent 对象。

---

#### POST /projects/{project_id}/intents/{intent_id}/conclude

探索结束，产出新 Fact 并将 Intent 结论落定。原子操作。仅对尚无结论的 Intent 有效。claim 规则与 heartbeat 相同：未认领可直接 conclude；由当前 `worker` conclude；若被其他消费者占用且未超时，返回 409。Conclude 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

`worker` 写入后永久保留，标识本次结论由谁产出。

Body：

```json
{
  "worker": "agent-B",
  "description": "80端口运行 nginx 1.18，存在目录遍历"
}
```

响应：

```json
{
  "fact": {
    "id": "f002",
    "description": "80端口运行 nginx 1.18，存在目录遍历"
  },
  "intent": {
    "id": "i002",
    "from": ["f001"],
    "to": "f002",
    "description": "http 服务探测与目录扫描",
    "creator": "agent-A",
    "worker": "agent-B",
    "last_heartbeat_at": "2026-03-21T10:05:00Z",
    "created_at": "2026-03-21T10:03:00Z",
    "concluded_at": "2026-03-21T10:05:00Z"
  }
}
```

---

#### POST /projects/{project_id}/complete

消费者判断某些 Facts 共同满足 goal 后调用，声明项目完成。原子操作。Complete 属于探索写操作，仅 `active` 项目允许；`stopped` 或 `completed` 返回 403。

创建一条 `from` 指向所列 Facts、`to` 指向 `goal` 的已结论 Intent。完成声明无探索过程，服务端将 `creator` 和 `worker` 均设为请求中的 `worker` 值。Project 状态变为 `completed`，并立即清空当前 `project.reason`。

如果后续外部验证发现这次完成判断有误，可再调用 `POST /projects/{project_id}/reopen` 撤销这条完成边，并把纠错信息写成新的 Fact 后继续探索。

`from` 不能包含 `goal`。

Body：

```json
{
  "from": ["f007", "f012", "f015"],
  "description": "三个 shell 均已获取，满足 goal 要求",
  "worker": "agent-A"
}
```

---

#### POST /projects/{project_id}/reopen

外部确认项目其实尚未完成时，显式撤销当前完成态。该接口属于项目管理操作，不属于探索写操作；仅 `completed` 项目允许调用。

服务端会：

- 找到当前唯一一条 `to=goal` 的完成边
- 读取它原本的 `from`
- 删除这条完成边，不保留“曾完成过一次”的图内历史
- 新建一个普通 Fact，内容为调用者提供的客观纠错说明
- 新建一条已结论 Intent，`description` 固定为 `external_feedback`，其 `from` 继承原完成边的 `from`，`to` 指向这个新 Fact
- 将 `creator` 和 `worker` 都写为请求中的 `creator`
- 将项目状态改回 `active`
- 清空当前 `project.reason`

这适合诸如“提交的 flag 是错的，需要继续寻找正确 flag”这类图外反馈。反馈本身被记成图中的新 Fact，而 `reopen` 只是触发这次改写的控制动作。

Body：

```json
{
  "description": "flag FLAG{fake} 是错误的，需要继续寻找正确 flag",
  "creator": "judge"
}
```

响应：

```json
{
  "project": {
    "id": "proj_001",
    "title": "xx渗透测试",
    "status": "active",
    "created_at": "2026-03-21T10:00:00Z",
    "reason": null
  },
  "fact": {
    "id": "f016",
    "description": "flag FLAG{fake} 是错误的，需要继续寻找正确 flag"
  },
  "intent": {
    "id": "i021",
    "from": ["f008"],
    "to": "f016",
    "description": "external_feedback",
    "creator": "judge",
    "worker": "judge",
    "last_heartbeat_at": "2026-03-21T10:10:00Z",
    "created_at": "2026-03-21T10:10:00Z",
    "concluded_at": "2026-03-21T10:10:00Z"
  }
}
```

---

### 导出

#### GET /projects/{project_id}/export?format=yaml

返回项目图的 YAML 结构化快照，供消费者读取。不含 `last_heartbeat_at`。尚无结论的 Intent（`to=null`）出现在 YAML 中，`worker` 字段直接反映当前是否有消费者在处理。`project.reason` 不在导出中出现，因为它是项目级协调状态，不属于事实图。读取时服务端仍会执行超时清理，因此导出结果反映的是清理后的当前状态。`created_at` 和 `concluded_at` 按服务端当前时区格式化为 `YYYY-MM-DD HH:mm:ss`。

```yaml
project:
  title: "xx渗透测试"
  origin: "目标 http://192.168.1.10"
  goal: "拿到 flag"

hints:
  - content: "优先看 web 服务"
    creator: "human"
    created_at: "2026-03-21 18:00:00"
  - content: "注意 80 端口"
    creator: "human"
    created_at: "2026-03-21 18:00:00"

facts:
  - id: origin
    description: "目标 http://192.168.1.10"
  - id: goal
    description: "拿到 flag"
  - id: f001
    description: "发现开放端口 22/80/443"
  - id: f002
    description: "80端口运行 nginx 1.18，存在目录遍历"
  - id: f003
    description: "遍历发现 /backup/db.sql，包含用户表"
  - id: f004
    description: "获得凭据 admin/passwd123"
  - id: f005
    description: "ssh 登录失败，凭据不通用"
  - id: f006
    description: "web后台登录成功，发现文件上传功能"
  - id: f007
    description: "上传 webshell 成功，获得 www-data shell"
  - id: f008
    description: "在 /var/www/html/flag.txt 找到 flag{abc123}"

intents:
  - from: [origin]
    to: f001
    description: "nmap 全端口扫描"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:01:00"
    concluded_at: "2026-03-21 18:02:00"

  - from: [f001]
    to: f002
    description: "http 服务探测与目录扫描"
    creator: "agent-A"
    worker: "agent-B"
    created_at: "2026-03-21 18:02:00"
    concluded_at: "2026-03-21 18:04:00"

  - from: [f002]
    to: f003
    description: "利用目录遍历下载 /backup/db.sql"
    creator: "agent-B"
    worker: "agent-B"
    created_at: "2026-03-21 18:04:00"
    concluded_at: "2026-03-21 18:05:00"

  - from: [f003]
    to: f004
    description: "解析 db.sql 提取用户凭据"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:05:00"
    concluded_at: "2026-03-21 18:05:30"

  - from: [f004]
    to: f005
    description: "尝试 ssh 登录"
    creator: "agent-A"
    worker: "agent-C"
    created_at: "2026-03-21 18:05:30"
    concluded_at: "2026-03-21 18:06:00"

  - from: [f002, f004]
    to: f006
    description: "用 f004 获取的凭据登录 f002 发现的后台"
    creator: "agent-A"
    worker: "agent-B"
    created_at: "2026-03-21 18:05:30"
    concluded_at: "2026-03-21 18:06:00"

  - from: [f006]
    to: f007
    description: "上传 php webshell 绕过扩展名检测"
    creator: "agent-B"
    worker: "agent-B"
    created_at: "2026-03-21 18:06:00"
    concluded_at: "2026-03-21 18:07:00"

  - from: [f007]
    to: f008
    description: "在 shell 中搜索 flag 文件"
    creator: "agent-B"
    worker: "agent-A"
    created_at: "2026-03-21 18:07:00"
    concluded_at: "2026-03-21 18:08:00"

  - from: [f008]
    to: goal
    description: "flag{abc123} 满足 goal 要求"
    creator: "agent-A"
    worker: "agent-A"
    created_at: "2026-03-21 18:08:00"
    concluded_at: "2026-03-21 18:08:01"
```

尚无结论的 Intent 示例（`worker` 不为 null 表示有消费者正在处理）：

```yaml
  - from: [f003]
    to: null
    description: "尝试利用目录遍历读取敏感文件"
    creator: "agent-A"
    worker: "agent-C"
    created_at: "2026-03-21 18:09:00"
    concluded_at: null
```

---

#### GET /projects/{project_id}/export?format=timeline

返回项目的时间线纯文本，按事件发生时间排序。YAML 快照展示图的结构拓扑，timeline 展示事件的先后顺序和因果链，两者互补。事件类型包括 `PROJECT CREATED`、`HINT`、`INTENT DECLARED`、`INTENT CONCLUDED`、`PROJECT COMPLETED`。若项目曾经完成后又被 `reopen`，由于旧的 `goal` 边已被撤销，timeline 中不会保留之前那次 `PROJECT COMPLETED` 事件。

---

## 消费者典型使用流程

```
1. 读取结构化项目数据
   用于调度、状态判断、intent 选择与协议写回

2. GET /projects/{id}/export?format=yaml
   读取图快照（仅供 Dispatcher 渲染给 Agent）

3. 若项目仍处于最初阶段：
   典型判定是 facts 只有 `origin` 和 `goal`，且没有普通 intent；
   某些消费者会允许“没有任何 intent”，或“只存在保留 bootstrap intent”都视为初始态
   a. 若尚不存在保留 bootstrap intent：
      POST /projects/{id}/intents
        { from: ["origin"], description: "bootstrap", creator: "dispatcher.bootstrap", worker: null }
   b. POST /projects/{id}/intents/{intent_id}/heartbeat
        { worker: "dispatcher-worker-A" }
      认领成功后，直接做一轮 `bootstrap`
   c. 推进过程中持续 heartbeat
   d. 若拿到了关键证明性事实：
      POST /projects/{id}/intents/{intent_id}/conclude
        { worker: "dispatcher-worker-A", description: "发现登录页存在默认口令 admin:admin" }
      若该新 fact 已足以满足 goal，也可继续：
      POST /projects/{id}/complete
        { from: ["新写入的 fact id"], description: "为什么该事实已足以满足 goal", worker: "dispatcher-worker-A" }
   e. 若本轮 bootstrap 失败且需要放弃：
      POST /projects/{id}/intents/{intent_id}/release
        { worker: "dispatcher-worker-A" }
   f. 回到步骤 1

4. 若图中存在未认领的普通 Intent（worker=null）：
   a. 选择一条可认领 Intent
   b. POST /projects/{id}/intents/{intent_id}/heartbeat
        { worker: "dispatcher-worker-A" }
      认领成功后，派发一次 `explore`
   c. 探索过程中持续 heartbeat
   d. 探索成功时：
      POST /projects/{id}/intents/{intent_id}/conclude
        { worker: "dispatcher-worker-A", description: "发现 /search 参数存在报错注入" }
   e. 若探索失败且需要放弃：
      POST /projects/{id}/intents/{intent_id}/release
        { worker: "dispatcher-worker-A" }
   f. 回到步骤 1

5. 若图中没有未认领 Intent：
   a. POST /projects/{id}/reason/claim
        { worker: "dispatcher-worker-A", trigger: "new_facts" }
      认领成功后，派发一次 `reason`
   b. 推进过程中持续 POST /projects/{id}/reason/heartbeat
        { worker: "dispatcher-worker-A" }
   c. 若 `reason` 返回完成结论：
      POST /projects/{id}/complete
        { from: ["f008"], description: "flag{abc} 满足 goal 要求", worker: "dispatcher-worker-A" }
      流程结束
   d. 若 `reason` 返回新的 intent：
      POST /projects/{id}/intents
        { from: ["f003"], description: "尝试 SQL 注入", creator: "dispatcher-worker-A", worker: null }
      然后 POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
   e. 若 `reason` 未返回完成结论，也未返回新 intent：
      POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
      本轮不写图
   f. 若 `reason` 执行失败、超时或需要放弃：
      POST /projects/{id}/reason/release
        { worker: "dispatcher-worker-A" }
   g. 回到步骤 1

6. 若项目已 `completed`，但图外验证确认之前的完成判断有误：
   a. POST /projects/{id}/reopen
        { description: "flag FLAG{fake} 是错误的，需要继续寻找正确 flag", creator: "judge" }
   b. 项目回到 `active`，图中新增一条 `external_feedback` 结论边和一个纠错 Fact
   c. 回到步骤 1
```
