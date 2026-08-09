# DeepSeek V4 请求契约 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正官方 DeepSeek V4 thinking/tool-call/keepalive 契约，同时保持聚合网关显式选择。

**Architecture:** 先统一模型族、官方 endpoint 与 thinking 状态识别，再让请求参数、消息清洗和保活共同消费同一判定。

**Tech Stack:** Python 3.12、LiteLLM、pytest、JSON 配置。

## Global Constraints

- 只在 `dev` 分支。
- 完整 `reasoning_content` 缺失立即失败，禁止补空串或剥参重试。
- provider usage/token 记账路径不改。
- 每项先 RED 后 GREEN。

### Task 1: DeepSeek 路由与请求参数

- [ ] 写失败测试：`openai/deepseek-v4-pro` + 官方 endpoint 被识别为 thinking，发送 thinking/max 且不发送 `tool_choice`。
- [ ] 写失败测试：显式关闭 thinking 恢复 `tool_choice=auto`；未显式开启的聚合 endpoint 不套官方参数。
- [ ] 实现统一 DeepSeek family/official/thinking 判定并接入 `_provider_request_options`。
- [ ] 运行聚焦测试并提交 `fix: enforce DeepSeek V4 request contract`。

### Task 2: reasoning_content 完整性

- [ ] 写失败测试：`openai/` 官方路由保留完整 reasoning；thinking 工具轮缺失/空值直接失败。
- [ ] `_strip_media_and_merge` 接收 `api_base` 并使用统一契约；删除补空串逻辑。
- [ ] 删除请求循环中针对 DeepSeek 的任何空值补救，仅保留独立 Kimi 契约。
- [ ] 运行聚焦测试并提交 `fix: require complete DeepSeek tool reasoning`。

### Task 3: 保活和默认配置

- [ ] 写失败测试：官方 DeepSeek 支持保活，聚合 DeepSeek 默认不支持。
- [ ] 写失败测试：不支持 `tool_choice` 的主请求生成的保活请求也不添加该字段，并关闭 thinking。
- [ ] 更新新配置默认值、示例与本地 DeepSeek V4 配置。
- [ ] 运行聚焦测试并提交 `fix: scope DeepSeek keepalive to official routes`。

### Task 4: 完整验证

- [ ] 运行相关 Python 测试与 `py_compile`。
- [ ] 运行全量 pytest。
- [ ] 运行前端测试与 build。
- [ ] 记录证据并提交 `docs: complete DeepSeek V4 contract plan`。

