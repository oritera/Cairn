# Cairn P0/P1 用户体验与运维能力实施计划

## 范围

本计划覆盖产品评估中列出的所有 P0 和 P1 改进项：

- P0：运行时可观测性、任务/Worker 历史、停滞项目诊断。
- P1：项目列表效率、图谱聚焦模式、详情/搜索/复制工作流、审计时间线过滤、操作安全、Hint 管理、完成/重开证据预览。

实施会按批次推进。每个批次完成后都必须让应用保持可用，并在进入下一批次前完成聚焦测试。

## 进度

- [x] Batch 1：Ops 状态与项目诊断。
- [x] Batch 2：Dispatcher 任务与 Worker 历史。
- [x] Batch 3：Log、搜索与证据复制。
- [x] Batch 4：项目列表效率与操作员安全。
- [x] Batch 5：图谱聚焦、Hint 管理、完成/重开证据。
- [x] 验证：JavaScript 语法检查、`git diff --check`、完整 `pytest cairn/tests` 测试套件均已通过。
- [ ] 延后打磨项：Worker/任务类型图谱颜色模式、隐藏已结束分支、紧凑/列表视图切换、基于浏览器的视觉 smoke test。

## 产品目标

1. [x] 让用户一眼看出 Cairn 正在推进还是已经阻塞。
2. [x] 让每个项目能解释自身当前状态，而不需要用户查看原始 dispatcher 日志。
3. [x] 降低查找、复制和交付证据所需时间。
4. [x] 通过区分观察模式与操作模式，让人工干预更安全。
5. [x] 当项目图谱和日志增长后，仍能保持可导航性。

## Batch 1：Ops 状态与项目诊断 - 已完成

### 用户收益

用户打开项目或项目列表后，可以回答：

- [x] 这个项目是 active、stopped、completed，还是等待中？
- [x] reason 当前是否已被占用？
- [x] 是否存在运行中或未领取的 intents？
- [x] 图谱是否没有变化，因此正在等待新工作？
- [x] 下一步预期动作是什么？

### 后端

基于现有服务端状态新增 `ops` API：

- [x] `GET /ops/summary`
  - 按项目状态统计全局数量。
  - Active 项目数量。
  - Working intent 数量。
  - Unclaimed intent 数量。
  - Reason lease 数量。
  - Stalled/attention 项目数量。
  - 每个项目的诊断摘要。

- [x] `GET /projects/{project_id}/diagnostics`
  - 项目状态。
  - facts、hints、intents 数量。
  - open、working、unclaimed、concluded intent 数量。
  - Reason lease 详情。
  - 诊断严重级别：`idle`、`running`、`attention`、`blocked`、`completed`、`stopped`。
  - 人类可读的诊断说明。
  - 建议的下一步动作。

本批次不要求 dispatcher 写回。它使用 SQLite 中已有的 canonical graph state 和 lease 信息。

### UI

在右侧项目面板中新增 `Ops` 标签页：

- [x] 顶部诊断卡片。
- [x] facts、hints、open intents、running intents、unclaimed intents 计数。
- [x] 当存在 reason lease 时显示详情。
- [x] Open intent 拆解。
- [x] 建议下一步动作。

在项目列表头部新增运维状态条：

- [x] Active、attention、running、unclaimed。
- [x] 可点击筛选延后到 Batch 4 实现。

### 测试

- [x] `/ops/summary` 的单元/API 测试。
- [x] 覆盖 active、stopped、completed、running intent、unclaimed intent、reason claimed 状态的项目诊断单元/API 测试。
- [x] 现有 server API 测试保持通过。

## Batch 2：Dispatcher 任务与 Worker 历史 - 已完成

### 用户收益

用户可以回答：

- [x] 哪个任务启动了？
- [x] 哪个 worker 执行了它？
- [x] 它运行了多久？
- [x] 它是成功、失败、被取消、超时、worker unhealthy，还是被拒绝？
- [x] dispatcher 在停止推进前尝试了什么？

### 后端

新增 append-only 运维事件：

- [x] 表：`ops_events`
  - `id`
  - `project_id`
  - `event_type`
  - `task_type`
  - `worker`
  - `intent_id`
  - `severity`
  - `message`
  - `details_json`
  - `created_at`

新增 API：

- [x] `GET /ops/events?project_id=&limit=&severity=&event_type=`
- [x] `POST /ops/events`
  - dispatcher 使用的内部 protocol endpoint。

Dispatcher 集成：

- [x] dispatch start 时写入事件。
- [x] task finish 时写入事件。
- [x] task cancellation 时写入事件。
- [x] worker unhealthy/rejected 时写入事件。
- [x] no-worker-available、claim failure、submit failure 时写入事件。
- [x] 保持原有日志输出不变；events 是额外的可观测界面。

### UI

在 Ops 标签页中新增 `Events` 区域：

- [x] 最近事件。
- [x] 严重级别 badge。
- [x] Worker/task/intent 元数据。
- [x] 复制事件。

### 测试

- [x] 事件创建/列表过滤 API 测试。
- [x] 使用 mocked client 验证关键事件写入的 dispatcher 逻辑测试。

## Batch 3：Log、搜索与证据复制 - 已完成

### 用户收益

用户可以快速找到并复制相关证据。

### UI

增强 Log 标签页：

- [x] 过滤 chips：Project、Hint、Intent、Conclude、Complete。
- [x] 跨 title、actor、meta 的文本搜索。
- [x] 针对已选 fact/intent lineage 的 `Only selected` 开关。
- [x] Live tail 开关。
- [x] 复制选中/可见条目。

增强 Detail 标签页：

- [x] 复制选中的 Fact。
- [x] 复制选中的 Intent。
- [x] 复制选中的 lineage。
- [x] 以 Markdown 格式复制。

除非服务端导出更方便，本批次不需要后端改动。

### 测试

- [x] 前端语法检查。
- [x] 现有语法检查。

## Batch 4：项目列表效率与操作员安全 - 已完成

### 用户收益

用户可以管理大量项目，并避免意外的 protocol mutation。

### UI

项目列表：

- [x] 按 title/id 搜索。
- [x] 状态筛选。
- [x] 来自 Ops summary 的 attention/running/unclaimed 筛选。
- [x] 按创建时间、标题、facts、intents、active work 排序。
- [ ] 如果卡片网格变得过于嘈杂，增加紧凑/列表视图选项。

操作员安全：

- [x] 新增本地偏好：`mode = observer | operator`。
- [x] 默认 observer。
- [x] 在 observer 模式中隐藏或禁用 mutation 操作：
  - [x] New Intent
  - [x] Claim/Heartbeat
  - [x] Release
  - [x] Conclude
  - [x] Complete
  - [x] Stop/Resume/Delete/Reopen 保持可见，并沿用现有显式流程。
- [x] 当按钮因为 observer 模式禁用时，给出清晰标签。

### 测试

- [x] 实施后现有测试套件通过。
- [ ] Observer/operator toggle 的手动浏览器 smoke test。

## Batch 5：图谱聚焦、Hint 管理、完成/重开证据 - 已完成

### 用户收益

用户可以在大型图谱中保持方向感，管理人工指导，并用更清晰的证据完成或重开项目。

### 图谱 UI

新增聚焦控制：

- [x] Full graph。
- [x] Selected upstream。
- [x] Selected downstream。
- [x] Open intents only。
- [x] Goal path / completed chain。
- [x] Latest activity。

新增视觉叠加：

- [ ] Worker 颜色或任务类型颜色模式。
- [ ] 隐藏已 concluded 的旁支。
- [x] 一键跳转到 latest fact、next unclaimed intent、goal。

### Hint 管理

将 hints 扩展为可管理的协作界面：

- [x] Hint 类型：strategy、evidence、correction、note。
- [x] 优先级：low、normal、high。
- [x] 可选目标绑定：project、fact、intent。
- [x] Pinned hints。
- [ ] 如果 dispatcher 支持需要，可以后续增加 consumed/active 状态。

这需要一次小型 hints schema migration。

### 完成/重开

Complete modal：

- [x] 显示已选 facts。
- [x] 显示上游 lineage 预览。
- [x] 当已选 facts 不包含任何新产生的 facts 时显示警告。

Reopen modal：

- [x] 显示现有 completion intent。
- [x] 显示上一次完成原因。
- [x] 沿用当前实现添加 reopen reason，但让预览更明确。

### 测试

- [x] DB migration 已由完整测试套件覆盖。
- [x] 服务端 API 兼容性测试。
- [x] 针对 focus mode 状态变更的前端语法检查。
- [ ] 针对 focus mode 状态变更的浏览器前端 smoke test。

## 实施顺序

1. [x] Batch 1：Ops 状态与项目诊断。
2. [x] Batch 2：Dispatcher 任务与 Worker 历史。
3. [x] Batch 3：Log、搜索与证据复制。
4. [x] Batch 4：项目列表效率与操作员安全。
5. [x] Batch 5：图谱聚焦、Hint 管理、完成/重开证据。

## 非目标

- 不在本计划内增加多 dispatcher 调度语义。
- 除 Batch 5 的 hint metadata 需要外，不改变 Fact/Intent 核心 protocol 语义。
- 不替换 P0/P1 工作中的 Cytoscape 或 Alpine。
- 不依赖外部 telemetry 服务。

## 发布说明

- 优先使用 additive API 和 schema change。
- 保持现有 YAML 和 Timeline export 稳定。
- 通过 operator mode 保留开发者手动 protocol 操作能力。
- 每个批次都应能独立测试，并且不需要 live LLM endpoint。
