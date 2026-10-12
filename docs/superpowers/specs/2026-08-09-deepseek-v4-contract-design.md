# DeepSeek V4 请求契约设计

## 目标

让 `openai/deepseek-v4-*` 在 DeepSeek 官方 OpenAI 兼容端点上使用确定、可验证的 thinking/tool-call 契约，同时避免把官方专属行为盲目套到聚合网关。

## 已验证的官方约束

- DeepSeek V4 thinking 通过 `extra_body.thinking.type=enabled` 显式开启，推理强度使用 `reasoning_effort=high|max`。
- thinking 工具轮必须把模型返回的完整 `reasoning_content` 随 assistant tool-call 消息回传；缺失会返回 400。
- DeepSeek 官方 Oh My Pi 集成明确标注 V4 thinking 不支持 `tool_choice`。
- 非官方 OpenAI 兼容网关的 `reasoning_content` 与 thinking 参数兼容性未统一，不能仅凭模型名强行套用官方请求体。

官方资料：

- https://api-docs.deepseek.com/guides/thinking_mode
- https://api-docs.deepseek.com/quick_start/agent_integrations/oh_my_pi/
- https://api-docs.deepseek.com/quick_start/pricing

## 根因

- `_supports_reasoning_fields` 只看 LiteLLM 路由前缀，`openai/deepseek-v4-*` 被误判为不支持原生推理字段。
- `_requires_tool_call_reasoning` 同样要求前缀为 `deepseek/`，官方配置实际使用 `openai/`，导致工具轮契约失效。
- 主请求无条件写入 `tool_choice=auto`，与官方 V4 thinking 契约冲突。
- 缺失 `reasoning_content` 时补空串会隐藏历史数据已经不完整的事实；空串不是“完整回传”。
- prompt-cache 保活仅凭字符串含 `deepseek` 就启用，无法证明聚合网关支持相同缓存与请求契约。

## 设计

### 路由识别

新增三个正交判断：

- DeepSeek 模型族：看最终模型名是否包含 `deepseek`。
- DeepSeek 官方路由：base URL 仅接受 `https://api.deepseek.com` 及其 `/v1`、`/beta` 形式。
- thinking 是否启用：模型条目 `enable_thinking` 显式值最高优先级；官方 V4/legacy reasoner 默认启用；非官方聚合路由不推断。

### 请求参数

官方 V4 thinking 请求固定发送：

```json
{
  "reasoning_effort": "max",
  "extra_body": {"thinking": {"type": "enabled"}}
}
```

模型配置中的合法 `reasoning_effort` 覆盖默认 `max`。thinking 启用时不发送任何 `tool_choice`；显式关闭 thinking 时仍使用 `tool_choice=auto`。

### 推理链完整性

DeepSeek 模型族返回的 `reasoning_content` 保持原值回传。对已确认启用 thinking 的工具轮，assistant tool-call 消息缺少或为空时直接抛出 `ValueError`，禁止补空串继续请求。

### 保活边界

DeepSeek 只在官方 endpoint 自动启用保活。聚合路由默认关闭，不再根据字符串命中。保活请求继承原请求是否支持 `tool_choice`：原请求没有该字段时，保活请求也不添加；thinking 显式切为 disabled，避免 1-token 输出上限与推理模式冲突。

### 配置

- 新生成的 DeepSeek V4 默认模型条目显式写入 `enable_thinking: true`、`reasoning_effort: max`。
- 当前本地 `config.json` 的两个官方 DeepSeek V4 条目同步补齐这两个字段。
- 聚合网关若要启用 thinking，必须在对应模型条目显式配置，不能自动继承官方契约。

## 非目标

- 不伪造或本地估算 provider usage。
- 不把非官方聚合网关宣称为与 DeepSeek 官方缓存等价。
- 不通过空 `reasoning_content`、重试剥参或静默降级掩盖契约错误。
