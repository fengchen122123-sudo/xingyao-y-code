# 历史消息字节稳定性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把普通历史维护移出请求准备，并让视觉 canonical user 消息在发送前与数据库字节一致。

**Architecture:** 顶层 `add_message(user)` 负责 turn-boundary 文件维护；`prepare_turn_messages` 低水位只读；视觉描述通过 MemoryManager CAS 更新最后一条 user 消息。

**Tech Stack:** Python 3.12、SQLite、pytest。

## Global Constraints

- 只在 `dev` 分支。
- CAS 失败立即暴露，不降级继续请求。
- `persist=False` 不写数据库。
- 显式 95% context epoch 分支保留物理折叠能力。
- 每项先 RED 后 GREEN。

### Task 1: 把无条件 prune 移到 user 入库边界

**Files:** `core/memory.py`、`core/agent_service.py`、`tests/test_memory.py`、`tests/test_agent_service.py`

- [ ] 写失败测试：低水位 `prepare_turn_messages` 的 manager spy 断言 prune 调用数为 0。
- [ ] 写失败测试：`add_message(user)` 触发一次 prune；`add_messages` 批量 user/assistant 不触发。
- [ ] 删除 prepare 的无条件调用，在 `add_message` 成功后仅对 `role == user` 调用维护。
- [ ] 运行 `tests/test_agent_service.py tests/test_memory.py` 并提交 `fix: move history pruning to turn ingestion`。

### Task 2: CAS 持久化视觉 canonical 消息

**Files:** `core/memory.py`、`core/llm.py`、`tests/test_memory.py`、`tests/test_llm_events_fixes.py`

- [ ] 写 CAS 成功/expected 不匹配失败测试，断言失败时 DB 原文不变。
- [ ] 实现 `replace_latest_user_message_content(session_name, expected_content, canonical_content) -> bool`，复用 add_messages 序列化规则并 commit。
- [ ] 写流式集成失败测试：非视觉主模型经副模型描述后，provider user content 必须等于 DB content；`persist=False` 不调用 CAS。
- [ ] 在 `_describe_images_with_secondary` 前后捕获最新 user content；`stream and parent_session` 时调用 CAS，False 直接抛 RuntimeError。
- [ ] 运行聚焦 LLM/Memory 测试并提交 `fix: persist canonical vision messages before requests`。

### Task 3: 完整验证

- [ ] 运行相关 Python 测试与 `py_compile`。
- [ ] 运行全量 pytest。
- [ ] 运行前端测试与 build。
- [ ] 记录实际证据并提交 `docs: complete history byte stability plan`。
