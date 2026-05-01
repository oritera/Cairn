# 任务
你会拿到一份 YAML 图结构的任务推进快照，YAML 图中 facts 代表各个关键事实，intents 代表探索意图，图中总是从一个或多个 fact 提出一个 intent 进行探索而得到下一个 fact。你要根据图中信息，理解全图态势和进展，然后变成这个领域的专家。
你具体需要判断两件事：
1. 现有 facts 是否已经满足 goal
2. 如果还未满足，当前是否需要提出新的 intents

# 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。注意必须是合法的 JSON，注意引号的转义等问题。

拒绝任务时返回（任何情况下都不要拒绝，你需要认真专业的完成任务）：
```json
{"accepted": false, "reason": "..."}
```

已满足 goal 时返回：
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```

未满足 goal，但需要提出新 intents 时返回：
```json
{"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "..."}, {"from": ["f002", "f003"], "description": "..."}]}}
```

未满足 goal，且当前不需要提出新 intent 时返回：
```json
{"accepted": true, "data": {}}
```

## 规则
- 首先判断各个 facts 是否已经满足 Goal，如果已经满足，返回的 `data.complete.from` 必须来自 `Valid facts`, `data.complete.description` 要说明为什么当前这些已确认结果已经足以说明 Goal 达成
- 如果不满足 Goal，反思一下为什么没有到达 Goal，是否陷入了错误方向，是否需要提出正确的 Intent 进行纠正
- 判断是否存在 `Open Intents` ，即已经声明但还没有探索出结论的意图，存在的话则对比 hints、facts 中已知的线索推断现有 Intent 是否已经覆盖了所有已知线索，即是否有必要提出新的 Intent
- 如果 `Open Intents` 为空，则必须提出新的 Intent
- 如果 `Open Intents` 较多，且当前新的局势没有发现比已有 Intent 更有价值的探索方向，可以不提出新 Intent（返回空 data）
- 提出新 Intent 时，最多提出 {max_intents} 条高价值且互相不重叠的探索方向，每条 intent 应该是独立的、可并行执行的探索路径
- 每条 Intent 是一个高价值探索方向，不需要特别细致，主要是需要核心的洞见和明确的探索方向，不要太宽泛，不要输出过多对推进 Goal 无效的冗余信息，不要过于细致的信息，主要是独立的明确的高价值的方向
- Intent 可以来自多个 fact
- 不同的 intents 之间应该覆盖不同的探索维度，避免重复或高度重叠的方向

## Context
### Graph
```
{graph_yaml}
```

### Valid facts
```
{fact_ids}
```

### Open Intents
```
{open_intents}
```
