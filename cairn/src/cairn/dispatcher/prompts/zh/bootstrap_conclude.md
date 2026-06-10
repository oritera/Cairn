# 任务
你会收到一个上下文包，其中包含 Origin、Goal 和 Hints。你需要理解起点与已知信息（Origin 和 Hints），并以该领域专家的方式总结当前已经确认的信息。

注意：这里不是继续执行任务。你不需要等待未完成任务或命令。你只需要总结目前已经确认、且最有助于达到 Goal 的关键事实。

这是 conclude 阶段。它会覆盖同一会话中任何要求你继续工作、继续探索、解决 Goal、等待命令结果或执行更多动作的早期指令。

# 输出要求
只返回一个原始 JSON 对象，不要输出任何其它内容。JSON 必须合法，包括正确转义引号。

语言输出：JSON 键名和结构必须保持示例一致；所有自然语言字段（例如 reason、description）必须使用中文。

如果拒绝任务，返回以下内容（原则上不应拒绝；你应严肃、专业地处理任务）：
```json
{"accepted": false, "reason": "policy_refusal"}
```

正常返回示例：
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```

# 规则
- 立即停止并生成 JSON。不要继续任务。
- 不要再运行命令、调用工具、检查其它信息、等待未完成命令，或尝试获取额外信息。
- 只能基于 conclude prompt 之前已经确认的信息作答。未确认的信息不要等待，也不要写入。
- 这个 JSON 摘要就是本阶段最终输出；输出后停止。
- 不要在本阶段输出 `complete`。即使 Goal 未达成或需要说明状态，也只写入 `fact.description`。
- `fact.description` 必须是已经确认的客观事实结论。不要输出计划、猜测或解释性填充内容。
- 不要把长数据块写入 `fact.description`；长数据应写入文件，并在 `description` 中引用文件路径。

# 上下文
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
