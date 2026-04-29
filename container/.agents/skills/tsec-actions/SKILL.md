---
name: tsec-actions
description: 在 tsec CTF/智能渗透比赛中，当需要对某道题提交 flag 时使用
---

## 用法

* 直接使用 `curl` 调 API，TSEC_SERVER_HOST 和 TSEC_AGENT_TOKEN 已存在于环境变量
* flag 统一都是 `flag{...}` 格式

提交 flag：

```bash
curl -X POST "http://${TSEC_SERVER_HOST}/api/submit" \
  -H "Agent-Token: ${TSEC_AGENT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"code": "<challenge_code>", "flag": "<flag>"}'
```

## 规则

- 只在高置信时提交 flag，不要把它当成爆破接口
- 提交 flag 以接口返回结果为准，不要自行假设提交成功
