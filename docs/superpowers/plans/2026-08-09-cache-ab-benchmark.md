# Prompt Cache A/B 基准 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用生产请求形状和多指标报告替换会掩盖失效的合成热轮平均基准。

**Architecture:** 纯统计函数校验 provider usage；单 endpoint runner 复用生产 system/tools/provider options；A/B orchestrator 只共享输入契约，不共享 endpoint 缓存身份。

**Tech Stack:** Python 3.12、LiteLLM、pytest、argparse。

## Global Constraints

- token 仅来自 provider usage，禁止估算。
- cached > prompt、usage 缺失立即失败。
- 工具调用立即终止，绝不执行。
- 每项先 RED 后 GREEN。

### Task 1: 多指标与严格 usage

- [x] 写失败测试：all/hot/min/catastrophic 指标分别正确，灾难失效不能被平均值掩盖。
- [x] 写失败测试：负数、cached > prompt、缺字段 usage 直接失败。
- [x] 实现 `summarize_rounds` 并让 `passed` 同时检查阈值与灾难失效。

### Task 2: 生产前缀与请求契约

- [x] 写失败测试：stable messages 使用 `build_stable_system_message`，不存在 6000 行合成填充。
- [x] 写失败测试：官方 DeepSeek thinking 请求不含 `tool_choice`，普通路由使用 `auto`。
- [x] 接入生产前缀、`api_base` 消息清洗与 provider options。

### Task 3: A/B 与 CLI

- [x] 写失败测试：A/B targets 共用 tools/rounds/suite seed，结果独立汇总。
- [x] 实现 `run_ab_benchmark` 和 compare CLI 参数/输出。
- [x] 运行聚焦测试并提交 `feat: add production-shaped cache A/B benchmark`（基准 11 passed；缓存/token 联跑 59 passed）。

### Task 4: 验证与实测

- [x] 运行 benchmark、token/accounting 聚焦测试与 `py_compile`（59 passed；py_compile 成功）。
- [x] 检查真实 A/B 前提：本地仅有官方 DeepSeek V4 与聚合 DeepSeek V3.2，不是同模型；官方单端点付费实测因未获生产 prompt/API 费用外发授权未执行，不生成虚假 99% 结论。
- [x] 运行全量 pytest、前端测试与 build（736 passed；44 passed；build 成功）。
- [x] 记录证据并提交 `docs: complete cache A/B benchmark plan`。
