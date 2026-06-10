# 任务
你会收到一份任务图的 YAML 快照。图中 facts 表示已确认的客观事实，intents 表示待探索的意图。图总是通过一个或多个事实提出探索意图，并推进到新的事实。你需要理解当前图、整体态势和进展，并以该领域专家的方式判断下一步。

你需要判断两件事：
1. 当前 facts 是否已经满足 Goal
2. 如果尚未满足，当前是否应该提出新的 intents

# 输出要求
只返回一个原始 JSON 对象，不要输出任何其它内容。JSON 必须合法，包括正确转义引号。

语言输出：JSON 键名和结构必须保持示例一致；所有自然语言字段（例如 reason、description）必须使用中文。

如果拒绝任务，返回：
```json
{"accepted": false, "reason": "..."}
```

如果 Goal 已满足，返回：
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```

如果 Goal 未满足，但应该提出新的 intents，返回：
```json
{"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "..."}, {"from": ["f002", "f003"], "description": "..."}]}}
```

如果 Goal 未满足，且当前不应提出新的 intent，返回：
```json
{"accepted": true, "data": {}}
```

# 规则
- 先判断事实是否已经满足 Goal。若已满足，`data.complete.from` 必须来自 `Valid facts`，`data.complete.description` 必须说明为什么当前已确认结果足以证明 Goal 达成。
- 如果 Goal 未满足，反思尚未达成的原因、任务是否偏离方向，以及是否需要提出正确的 Intent 来纠偏。
- 判断是否存在 `Open Intents`，即已声明但尚未得出结论的 intents。若存在，比较 hints 和 facts 中的线索，推断当前 intents 是否已覆盖已知线索，以及是否需要新增 intents。
- 如果 `Open Intents` 为空，必须提出新的 intents。
- 如果 `Open Intents` 很多，且新情况没有显示出比现有方向更有价值的探索路径，可以不提出新 intent（返回空 data）。
- 提出 intents 时，最多提出 {max_intents} 个高价值、互不重叠的探索方向。每个 intent 应该是独立、可并行的探索路径。
- 每个 Intent 应聚焦高价值探索方向，不需要过度细节化；避免过宽、重复或无助于推进 Goal 的冗余内容。
- 一个 Intent 可以源自多个 facts。
- 不同 intents 应覆盖不同探索维度，避免重复或高度重叠。

# 上下文
## Graph
```
{graph_yaml}
```

## Valid facts
```
{fact_ids}
```

## Open Intents
```
{open_intents}
```
