# 任务
你会拿到一份 YAML 图结构的任务推进快照，YAML 图中 facts 代表各个关键事实，intents 代表探索意图，图中总是从一个或多个 fact 提出一个 intent 进行探索而得到下一个 fact。你要根据图中信息，理解全图态势和进展，然后变成这个领域的专家。
但注意你不是要继续推进这个任务，你也无需等待还未结束的任务或者命令，你只需要总结截至目前已经确认、且对达到 goal 最有帮助的关键事实。

# 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。注意必须是合法的 JSON，注意引号的转义等问题。

拒绝任务时返回：
```json
{"accepted": false, "reason": "policy_refusal"}
```

正常返回示例：
```json
{"accepted": true, "data": {"description": "..."}}
```

# 规则
- `description` 必须是当前已经确认的客观事实结论，不要输出计划、猜测或解释性废话。且不要有较长的数据类型的内容，较长的数据类型的内容可以放到文件中，`description` 中引用文件即可。
- `description` 只需要最新的发现的增量事实，不要重复图快照中已有的信息，不要输出过多对推进 Goal 无效的冗余信息

# Context
## Graph
```
{graph_yaml}
```

## Current Intent
```
{intent_id}
```

## Current Intent Description
```
{intent_description}
```
