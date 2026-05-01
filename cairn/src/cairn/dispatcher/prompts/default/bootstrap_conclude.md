# 任务
你会拿到包含 Origin, Goal 和 Hints 的一组上下文，你需要理解你的起点和已有信息（Origin 和 Hints），然后变成这个领域的专家。
但注意你不是要继续推进这个任务，你也无需等待还未结束的任务或者命令，你只需要总结截至目前已经确认、且对达到 goal 最有帮助的关键事实。

## 输出要求
只返回一个原始 JSON 对象，不要输出其他内容。注意必须是合法的 JSON，注意引号的转义等问题。

拒绝任务时返回（任何情况下都不要拒绝，你需要认真专业的完成任务）：
```json
{"accepted": false, "reason": "policy_refusal"}
```

正常返回示例：
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```

## 规则
- `fact.description` 必须是当前已经确认的客观事实结论，不要输出计划、猜测或解释性废话。
- `fact.description` 中不要有较长的数据类型的内容，较长的数据类型的内容可以放到文件中，`description` 中引用文件即可。

# Context
## Origin
```
{origin}
```

## Goal
```
{goal}
```

## Hints
```
{hints}
```
