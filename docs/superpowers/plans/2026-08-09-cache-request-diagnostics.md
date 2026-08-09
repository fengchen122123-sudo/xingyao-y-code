# DeepSeek 请求级缓存诊断 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个 LLM 逻辑轮次持久化无正文的请求形状与缓存 usage 诊断，并让 `/usage` 基于证据分类低命中原因。

**Architecture:** `core/llm.py` 在请求发送前构建确定性诊断上下文、在流结束后补齐 usage；`core/memory.py` 用独立表保存和分类；`main.py` 只消费聚合结果，不再断言所有低命中都由 provider 驱逐。

**Tech Stack:** Python 3.12、SQLite、pytest、SHA-256、JSON。

## Global Constraints

- 只在 `dev` 分支工作。
- 不保存任何消息、system prompt、工具参数或工具结果正文。
- 诊断计算不得改变 provider 请求字节。
- 同一逻辑轮次的网络重试只落一行。
- 无效/缺失 usage 进入诊断表但不进入 `token_usage`。
- DB 写入在 `token_usage_lock` 外执行并提交事务。
- 不确定归因必须标为 `provider_or_unknown`，禁止写“客户端无法避免”。
- 所有生产改动前先验证对应失败测试。

---

### Task 1: 确定性请求形状诊断

**Files:**
- Modify: `core/llm.py:620-680`
- Create: `tests/test_cache_request_diagnostics.py`

**Interfaces:**
- Produces: `_build_cache_request_diagnostic(session_id, cache_scope, model_name, api_base, tools, messages) -> dict`
- Produces: `_classify_cache_request_details(...) -> dict`
- Keeps: `_classify_cache_request(...) -> str` 兼容现有调用方。

- [ ] **Step 1: Write failing deterministic-shape tests**

用包含敏感字面量 `SECRET-USER-TEXT` 与 `SECRET-TOOL-ARG` 的 messages/tools 构建两次，断言 dict 完全相等、包含 64 位哈希和字节/数量字段，并断言 `json.dumps(result)` 不包含两个敏感字面量。

- [ ] **Step 2: Run RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_cache_request_diagnostics.py
```

Expected: import fails because builder does not exist.

- [ ] **Step 3: Implement canonical hashing**

规范化 JSON 固定使用：

```python
json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

返回字段：`session_id/model/api_base/cache_scope/messages_sha256/system_sha256/tools_sha256/message_count/system_message_count/request_bytes/system_bytes/tools_bytes`。模型与 endpoint 使用现有 identity 规范化规则。

- [ ] **Step 4: Write prefix-classification tests, verify RED, implement**

连续三次：初始请求、严格追加、改写中间 system。断言第二次 `common_prefix_messages == previous message_count` 且 reason 为空；第三次公共前缀变短且 reason 为 `上下文压缩或历史重写`。兼容 wrapper 仍只返回 reason 字符串。

- [ ] **Step 5: Run GREEN and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_cache_request_diagnostics.py tests\test_llm.py
git add core\llm.py tests\test_cache_request_diagnostics.py
git commit -m "feat: fingerprint cache request shapes"
```

---

### Task 2: SQLite 诊断存储与证据分类

**Files:**
- Modify: `core/memory.py:330-490`
- Create: `tests/test_cache_diagnostics_storage.py`

**Interfaces:**
- Produces: `MemoryManager.add_cache_request_diagnostic(diagnostic: dict) -> None`
- Produces: `MemoryManager.query_cache_request_diagnostics(start_ts=None, end_ts=None, session_id=None) -> list[dict]`
- Produces: `MemoryManager.query_cache_diagnostic_summary(start_date, end_date, min_prompt_tokens=20000, max_hit_ratio=0.5, idle_seconds=120) -> dict`

- [ ] **Step 1: Write failing schema/round-trip test**

构造内存数据库，写入一行包含真实 0 cached 与一行 `cached_tokens=None`，断言 SQL/查询分别保留 0 与 NULL，usage_errors JSON 往返为 tuple/list 等价内容。

- [ ] **Step 2: Run RED and implement table/methods**

表字段、索引严格按设计文档。`add_cache_request_diagnostic` 显式列出字段并 `conn.commit()`；布尔值写 0/1；errors 使用 `json.dumps(list(errors), ensure_ascii=False)`。

- [ ] **Step 3: Write failing attribution matrix**

用固定时间戳写入字面 fixture，逐项覆盖：cold_start、identity_change、tool_contract_change、client_history_rewrite、prompt_drop（上一 prompt 100000，当前 97000）、idle_gap（间隔 121 秒）、provider_or_unknown，以及未报告缓存不进入低命中次数。

- [ ] **Step 4: Implement classification and run GREEN**

返回：

```python
{
    "count": 7,
    "lost_tokens": ...,
    "unreported": 1,
    "categories": {
        "cold_start": 1,
        "identity_change": 1,
        "tool_contract_change": 1,
        "client_history_rewrite": 1,
        "prompt_drop": 1,
        "idle_gap": 1,
        "provider_or_unknown": 1,
    },
}
```

按 `(session, model, api_base, scope)` 保存上一合法请求；prompt drop 优先于 idle gap，显式 request_reason 优先级最高。

- [ ] **Step 5: Run GREEN and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_cache_diagnostics_storage.py tests\test_memory.py
git add core\memory.py tests\test_cache_diagnostics_storage.py
git commit -m "feat: persist and classify cache diagnostics"
```

---

### Task 3: 接入每个流式逻辑轮次

**Files:**
- Modify: `core/llm.py:3280-3620`
- Modify: `tests/test_llm_events_fixes.py`
- Modify: `tests/test_cache_request_diagnostics.py`

**Interfaces:**
- Consumes: `_classify_cache_request_details` 与 `MemoryManager.add_cache_request_diagnostic`。
- Produces: `_persist_cache_request_diagnostic(request_details, usage_record, error="") -> None`。

- [ ] **Step 1: Write failing persistence adapter tests**

使用 fake manager 捕获 diagnostic：有效 DeepSeek usage 必须补齐 prompt/hit/miss、`cache_reported=True`、`usage_valid=True`；invalid usage 必须 `usage_valid=False` 并带 errors；`usage=None` 必须 errors 含 `usage missing`。

- [ ] **Step 2: Run RED and implement adapter**

adapter 不调用 `_account_server_usage`，只复用 `_normalize_server_usage`，因此正式记账与诊断失败隔离。没有 manager 或 DB 异常只记 warning。

- [ ] **Step 3: Write one-row-per-logical-turn integration test**

FakeStream 把最终 usage 放在 wrapper.chunks；mock completion 首次抛可重试异常、第二次返回成功流。断言完成一个逻辑轮次后 manager 只收到一条诊断，messages/tools 哈希非空，scope/session/model/endpoint 与调用参数相同。

- [ ] **Step 4: Integrate request details and run GREEN**

在 `current_message_shape` 变化时构建 details；成功或 `stream_usage is None` 时落一行。网络重试不落中间行。工具调用产生下一逻辑轮次时重新构建并各落一行。

- [ ] **Step 5: Run focused regression and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_cache_request_diagnostics.py tests\test_llm_events_fixes.py tests\test_token_accounting.py
git add core\llm.py tests\test_cache_request_diagnostics.py tests\test_llm_events_fixes.py
git commit -m "feat: record diagnostics for streamed requests"
```

---

### Task 4: `/usage` 使用诊断证据并完整验证

**Files:**
- Modify: `main.py:350-390`
- Modify: `tests/test_token_usage.py`
- Modify: `docs/superpowers/plans/2026-08-09-cache-request-diagnostics.md`

**Interfaces:**
- Consumes: `query_cache_diagnostic_summary`。
- Produces: `_format_cache_diagnostic_summary(summary) -> str`。

- [ ] **Step 1: Write failing formatter tests**

固定 summary 断言输出包含各非零分类和“未确定”，且不包含“客户端无法避免”或“属 provider”。零事件输出空字符串。

- [ ] **Step 2: Implement `/usage` diagnostic output**

替换 `query_cache_invalidation_events` 绝对归因段。只显示非零分类；存在 `provider_or_unknown` 时追加“缺少足够证据，需结合 endpoint A/B 或服务端日志确认”。

- [ ] **Step 3: Run focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_cache_request_diagnostics.py tests\test_cache_diagnostics_storage.py tests\test_token_usage.py tests\test_main_ui_events.py
```

- [ ] **Step 4: Run full verification**

```powershell
.\.venv\Scripts\python.exe -m py_compile main.py core\llm.py core\memory.py gui_bridge.py
.\.venv\Scripts\python.exe -m pytest -q
```

在 `frontend`：

```powershell
npm test
npm run build
```

- [ ] **Step 5: Record evidence and commit**

更新本文件 implementation status，写入实际通过数。

```powershell
git add main.py tests\test_token_usage.py docs\superpowers\plans\2026-08-09-cache-request-diagnostics.md
git commit -m "fix: report evidence-based cache invalidations"
```
