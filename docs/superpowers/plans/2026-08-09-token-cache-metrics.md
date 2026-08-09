# Token 与缓存命中统计正确性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Y-CODE 只使用经过严格校验的服务端 usage 记账，并让 TUI/GUI 展示按当前会话、模型、接口和主代理作用域隔离的本轮、热路径与会话全量缓存命中率。

**Architecture:** `core/llm.py` 提供服务端 usage 归一化、身份分桶、生命周期与只读快照接口；流结束处先校验再一次性更新进程计费、主代理缓存遥测与数据库。TUI 和 GUI bridge 只消费同一快照，Vue 前端保留旧字段并增加三种明确口径。

**Tech Stack:** Python 3.12、pytest、prompt_toolkit、PyWebView bridge、Vue 3、Pinia、TypeScript、Vitest、Vite。

**Implementation Status (2026-08-09):** Task 1-6 已按 RED→GREEN 顺序完成。最终验证：聚焦 Python 174 passed、全量 Python 705 passed、前端 44 passed、`npm run build` 成功、AGENTS.md 要求的 Python 语法检查成功。实现提交：`3b362bb`、`bfe8454`、`c293a88`、`5ab6ebe`、`cd427dc`、`e9417f4`。

## Global Constraints

- 只在 `dev` 分支工作，不创建或合并其他分支。
- 服务端 usage 是真实 token 的唯一来源；本地估算只用于请求返回前的上下文窗口占用。
- 命中率只允许使用 `cached_prompt_tokens / prompt_tokens`。
- 异常 usage 拒绝进入正式统计和 `token_usage` 表，不夹断、不补零、不伪造。
- 服务端未报告缓存明细时持久化 `cached_tokens=NULL`；缓存命中率的聚合分母只包含已报告缓存明细的 prompt。
- 缓存遥测按 `session + model + endpoint + scope` 隔离，界面只展示 `main`。
- 所有 token 状态读写必须持有 `token_usage_lock`，持锁期间不做数据库或 UI IO。
- 后台线程不得直接更新 prompt_toolkit 布局或调用 `app.invalidate()`。
- 保持旧 GUI 字段 `cached_pct` 向后兼容。
- 每个生产改动前必须先运行对应失败测试并确认失败原因。

---

### Task 1: 严格归一化与校验服务端 usage

**Files:**
- Modify: `core/llm.py:752-770`
- Create: `tests/test_token_accounting.py`

**Interfaces:**
- Produces: `_normalize_server_usage(usage) -> dict`
- Produces: `_extract_cached_tokens(usage) -> int`，保留现有兼容接口并复用归一化字段读取规则。
- Consumed by: Task 3 的流结束统一记账。

- [ ] **Step 1: Write the failing normalization tests**

创建 `tests/test_token_accounting.py`，使用手工常量验证 DeepSeek 原生明细、OpenAI 明细和 completion 代数推导：

```python
from types import SimpleNamespace

import pytest

from core.llm import _normalize_server_usage


def test_normalize_deepseek_usage_preserves_exact_server_tokens():
    record = _normalize_server_usage({
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "prompt_cache_hit_tokens": 990,
        "prompt_cache_miss_tokens": 10,
    })
    assert record == {
        "valid": True,
        "errors": (),
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "cached_tokens": 990,
        "cache_miss_tokens": 10,
        "cache_reported": True,
    }


def test_normalize_openai_usage_derives_completion_only_from_exact_total():
    usage = SimpleNamespace(
        prompt_tokens=676,
        completion_tokens=None,
        total_tokens=692,
        prompt_tokens_details=SimpleNamespace(cached_tokens=512),
    )
    record = _normalize_server_usage(usage)
    assert record["valid"] is True
    assert record["completion_tokens"] == 16
    assert record["cached_tokens"] == 512
    assert record["cache_reported"] is True
```

- [ ] **Step 2: Run normalization tests to verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_token_accounting.py
```

Expected: collection fails because `_normalize_server_usage` does not exist.

- [ ] **Step 3: Implement exact field extraction**

在 `core/llm.py` 增加 `_usage_field`、`_optional_nonnegative_int` 和 `_normalize_server_usage`。实现必须区分“字段缺失”和“服务端明确返回 0”，拒绝 bool、负数、非整数字符串和小数；缓存字段优先使用 `prompt_tokens_details.cached_tokens`，其次使用 `prompt_cache_hit_tokens`。

返回字典固定包含：

```python
{
    "valid": not errors,
    "errors": tuple(errors),
    "prompt_tokens": prompt,
    "completion_tokens": completion,
    "total_tokens": total,
    "cached_tokens": cached,
    "cache_miss_tokens": miss,
    "cache_reported": cache_reported,
}
```

completion 字段缺失时，只在 prompt 与 total 均合法且 `total >= prompt` 时使用 `total - prompt`；不使用 tokenizer 或平均比例估算。
缓存字段未报告时 `cached_tokens=None`、`cache_miss_tokens=None`、`cache_reported=False`，不得改写成真实零命中。

- [ ] **Step 4: Add invalid-usage tests and verify RED**

追加参数化测试，字面量覆盖：

```python
@pytest.mark.parametrize("usage, expected_error", [
    ({"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0}, "prompt_tokens"),
    ({"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11,
      "prompt_cache_hit_tokens": 11}, "cached_tokens exceeds prompt_tokens"),
    ({"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11,
      "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 1}, "cache hit + miss"),
    ({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 11}, "prompt + completion"),
])
def test_normalize_usage_rejects_inconsistent_server_values(usage, expected_error):
    record = _normalize_server_usage(usage)
    assert record["valid"] is False
    assert any(expected_error in error for error in record["errors"])
```

Run the test and confirm the new cases fail until invariants are implemented.

- [ ] **Step 5: Implement invariants and run GREEN**

实现以下校验：

```text
cached <= prompt
explicit hit + explicit miss == prompt
prompt + explicit completion == total
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_token_accounting.py tests\test_llm_events_fixes.py::test_litellm_patch_preserves_stream_usage_details
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add core\llm.py tests\test_token_accounting.py
git commit -m "fix: validate exact server token usage"
```

---

### Task 2: 按请求身份隔离缓存遥测并提供只读快照

**Files:**
- Modify: `core/llm.py:593-716`
- Modify: `tests/test_token_accounting.py`
- Modify: `tests/test_llm.py:300-320`

**Interfaces:**
- Produces: `_cache_usage_key(session_id, model_name, api_base, cache_scope) -> tuple[str, str, str, str]`
- Produces: `get_token_usage_snapshot(session_id, model_name, api_base, cache_scope="main") -> dict`
- Produces: `rename_cache_usage_session(old_name, new_name) -> None`
- Produces: `clear_cache_usage_session(session_name) -> None`
- Produces: `_mark_cache_usage_unavailable(session_id, model_name, api_base, cache_scope, reason) -> None`
- Changes: `_record_cache_usage(..., model_name="", api_base="") -> dict`
- Consumed by: Task 3 的写入和 Task 4 的 UI 读取。

- [ ] **Step 1: Write failing identity-isolation tests**

向 `tests/test_token_accounting.py` 增加测试：同一 session 的两个模型、两个 endpoint、两个 session 与 `main/subagent` scope 分别写入不同的字面量，并断言 `get_token_usage_snapshot` 只返回完全匹配桶的数据。关键断言：

```python
_record_cache_usage("s1", "main", 1000, 990, "", "openai/deepseek-v4", "https://a/v1")
_record_cache_usage("s1", "main", 2000, 1000, "", "openai/deepseek-v4", "https://b/v1")

a = get_token_usage_snapshot("s1", "OPENAI/DEEPSEEK-V4", "https://a/v1/", "main")
b = get_token_usage_snapshot("s1", "openai/deepseek-v4", "https://b/v1", "main")
assert a["cache"]["session_all"]["prompt_tokens"] == 1000
assert a["cache"]["session_all"]["rate"] == 99.0
assert b["cache"]["session_all"]["prompt_tokens"] == 2000
assert b["cache"]["session_all"]["rate"] == 50.0
```

- [ ] **Step 2: Run identity tests to verify RED**

Run the new tests and confirm failure because the accessor/key do not exist and current state merges endpoint data.

- [ ] **Step 3: Implement normalized identity buckets and immutable snapshots**

把 `_cache_usage_states` 的 key 改为四元组。`get_token_usage_snapshot` 必须在锁内构造全新的嵌套字典；无记录时 `cache.available=False` 且三个 rate 为 `None`，真实零命中时 `available=True` 且 rate 为 `0.0`。

`_record_cache_usage` 只接受已经归一化的非负整数，若 `cached > prompt` 直接抛出 `ValueError`，不再用 `max(0, ...)` 夹断。`_mark_cache_usage_unavailable` 不改变既有累计值，只把当前身份桶的 `usage_valid=False`、可见 rate 置为不可用并记录原因；下一次有效 usage 恢复可见统计。

进程级 `process` 字段复制 `session_token_usage.cumulative_*`；`window` 字段复制 `latest_*` 与 `max_window`。不得返回内部 state 的引用。

- [ ] **Step 4: Write lifecycle tests and verify RED**

增加两个测试：

```python
rename_cache_usage_session("old", "new")
assert get_token_usage_snapshot("old", model, base)["cache"]["available"] is False
assert get_token_usage_snapshot("new", model, base)["cache"]["available"] is True

clear_cache_usage_session("new")
assert get_token_usage_snapshot("new", model, base)["cache"]["available"] is False
```

另加副本隔离断言：修改一次 accessor 返回的嵌套字段，再次读取的真实值不变。

- [ ] **Step 5: Implement lifecycle and run GREEN**

重命名在同一锁内迁移 `_cache_usage_states` 与 `_cache_request_states` 的 session 前缀；清理删除所有匹配 session 的 key。运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_token_accounting.py tests\test_llm.py
```

- [ ] **Step 6: Commit Task 2**

```powershell
git add core\llm.py tests\test_token_accounting.py tests\test_llm.py
git commit -m "feat: isolate cache metrics by request identity"
```

---

### Task 3: 流结束统一进行有效 usage 记账

**Files:**
- Modify: `core/llm.py:3240-3285`
- Modify: `core/memory.py:379-445`
- Modify: `tests/test_llm_events_fixes.py:1550-1590`
- Modify: `tests/test_token_accounting.py`
- Modify: `tests/test_token_usage.py`

**Interfaces:**
- Consumes: `_normalize_server_usage`、`_record_cache_usage`。
- Produces: 流式响应完成后的唯一记账路径；无新增公共接口。

- [ ] **Step 1: Write failing stream-accounting tests**

新增一个有效 usage 用例，服务端值为 prompt 1000、completion 100、total 1100、hit 990、miss 10，断言：

```text
进程累计 prompt +1000
进程累计 completion +100
进程累计 total +1100
当前 main 窗口 latest_prompt == 1000
当前身份桶 session_all.rate == 99.0
持久化参数与服务端值逐项相等
```

新增一个矛盾 usage 用例（hit 990、miss 9、prompt 1000），断言进程累计、身份桶和持久化调用次数均不变，并捕获一条包含模型/session/scope 的 warning。

- [ ] **Step 2: Run stream tests to verify RED**

Run the two new tests. Expected: current code silently records the inconsistent usage and uses `total - prompt` without validating explicit completion.

- [ ] **Step 3: Replace the stream-end accounting block**

流结束后：

1. 调用 `_normalize_server_usage(stream_usage)`；
2. invalid 时记录 warning 并跳过所有正式统计和 DB 持久化；
3. valid 时在锁内更新进程累计；
4. 只有 `cache_scope == "main"` 才更新 `latest_*` 窗口镜像和缓存身份桶；
5. `_persist_token_usage` 使用归一化后的四个精确字段。

非 main 请求继续进入进程计费累计与数据库，但不得改变 main 的 `latest_*` 或缓存桶。
服务端未报告缓存明细时，prompt/completion/total 仍是有效计费数据；`cached_tokens` 以 `None` 传给持久化层，主缓存桶调用 `_mark_cache_usage_unavailable` 而不是累计成 0%。

`MemoryManager.add_token_usage` 保留 `None` 写入 SQL NULL。`query_token_usage_by_model` 额外返回 `cache_prompt = SUM(CASE WHEN cached_tokens IS NOT NULL THEN prompt_tokens ELSE 0 END)`；所有历史命中率使用 `cached / cache_prompt`，不再把未报告缓存的输入当成 miss。

- [ ] **Step 4: Add non-main isolation test and verify RED/GREEN**

先写测试：主请求写入 99% 后执行 `cache_scope="secondary:test"` 的有效 usage，断言进程累计增长但 main 快照的窗口和三种缓存率完全不变。运行确认 RED，再完成最小逻辑并运行 GREEN。

- [ ] **Step 5: Run focused regressions**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_token_accounting.py tests\test_llm_events_fixes.py tests\test_token_usage.py
```

- [ ] **Step 6: Commit Task 3**

```powershell
git add core\llm.py tests\test_token_accounting.py tests\test_llm_events_fixes.py tests\test_token_usage.py
git commit -m "fix: account only validated server usage"
```

---

### Task 4: TUI 与 GUI bridge 共用当前身份快照

**Files:**
- Modify: `main.py:388-414, 2668-2705`
- Modify: `gui_bridge.py:788-835, 1520-1568`
- Modify: `tests/test_main_ui_events.py:8-22`
- Create: `tests/test_gui_token_usage.py`

**Interfaces:**
- Consumes: `get_token_usage_snapshot`、`rename_cache_usage_session`、`clear_cache_usage_session`。
- Produces: `_format_cache_usage_summary(cache: dict) -> str`。
- Produces: `ApiBridge.get_token_usage()` 扩展后的兼容 payload。

- [ ] **Step 1: Write failing TUI formatter tests**

测试可用快照严格输出：

```text
本轮 99.0% · 热路径 98.8% · 会话全量 91.0%
```

不可用快照严格输出：

```text
本轮 -- · 热路径 -- · 会话全量 --
```

测试中的期望字符串必须手写，不调用生产格式化辅助函数生成。

- [ ] **Step 2: Run TUI tests to verify RED, then implement formatter**

`get_status` 调用 `get_token_usage_snapshot(current_session, current_model, current_url)`，进程计费摘要、缓存摘要和上下文占用均从该快照读取，不再直接读取缓存镜像字段。进程缓存摘要使用 `cumulative_cache_prompt` 作分母；当前会话三种命中率仍使用身份桶各自的 prompt。

- [ ] **Step 3: Write failing GUI payload tests**

用 `object.__new__(ApiBridge)` 构造不启动数据库的 bridge，只设置 `_current_session`；monkeypatch `get_active_model` 与 `core.llm.get_token_usage_snapshot` 返回固定快照。断言：

```python
assert payload["cached_pct"] == 91.0
assert payload["cache_latest_pct"] == 99.0
assert payload["cache_hot_pct"] == 98.8
assert payload["cache_session_pct"] == 91.0
assert payload["prompt_tokens"] == 420000
assert payload["cached_tokens"] == 382200
```

另测 unavailable 时三个新百分比为 `None`、`cache_available=False`，旧 `cached_pct` 为 `0.0`。

- [ ] **Step 4: Implement GUI payload and lifecycle hooks**

`ApiBridge.get_token_usage` 使用 `_current_session + active litellm_model + base_url` 读取快照。切换只改变读取目标；成功重命名后迁移缓存状态；清空/删除后清理缓存状态。TUI 对应的新建、删除、重命名、清空入口调用相同生命周期接口。

- [ ] **Step 5: Run Python UI tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_main_ui_events.py tests\test_gui_token_usage.py tests\test_gui_tool_events.py
```

- [ ] **Step 6: Commit Task 4**

```powershell
git add main.py gui_bridge.py tests\test_main_ui_events.py tests\test_gui_token_usage.py tests\test_gui_tool_events.py
git commit -m "fix: show session-scoped cache metrics in Python UIs"
```

---

### Task 5: Vue 前端保留兼容字段并展示三种口径

**Files:**
- Modify: `frontend/src/types/pywebview.d.ts:93-100`
- Modify: `frontend/src/stores/appStore.ts:193, 394, 896-903`
- Modify: `frontend/src/components/InputArea.vue:55-62, 95-105`
- Create: `frontend/src/stores/appStore.tokenUsage.test.ts`

**Interfaces:**
- Consumes: GUI bridge token payload。
- Produces: 扩展后的 `TokenUsage` TypeScript 类型与 Pinia 状态。

- [ ] **Step 1: Write failing store event test**

创建 Pinia 测试，发送完整 `token_usage` bridge 事件，断言数值和 `null` 不被 `|| 0` 覆盖：

```typescript
store.handleBridgeEvent({ type: 'token_usage', data: {
  used: 1000, window: 128000, cumulative: 1100, cached_pct: 91,
  cache_available: true, cache_latest_pct: 99, cache_hot_pct: 98.8,
  cache_session_pct: 91, cache_hot_turns: 8,
  cache_invalidation_reason: '', prompt_tokens: 420000, cached_tokens: 382200,
}});
expect(store.tokenUsage.cache_latest_pct).toBe(99);
expect(store.tokenUsage.cache_hot_pct).toBe(98.8);
expect(store.tokenUsage.prompt_tokens).toBe(420000);
```

第二个事件使用三个 `null` 百分比并断言仍为 `null`。

- [ ] **Step 2: Run frontend test to verify RED**

```powershell
npm test -- --run frontend/src/stores/appStore.tokenUsage.test.ts
```

在 `frontend` 目录实际执行：

```powershell
npm test -- --run src/stores/appStore.tokenUsage.test.ts
```

Expected: new fields are absent from store/type.

- [ ] **Step 3: Extend type and store with null-safe assignment**

三个新百分比类型为 `number | null`，赋值使用 `data.cache_latest_pct ?? null`。布尔值使用 `Boolean(data.cache_available)`；token 数值使用 `Number(data.prompt_tokens ?? 0)`。

- [ ] **Step 4: Display all three cache rates in InputArea**

在 token 上下文占用旁增加：

```text
缓存: 本轮 99.0% · 热路径 98.8% · 全量 91.0%
```

不可用时对应位置显示 `--`。只渲染纯文本，不使用 Markdown 标记。

- [ ] **Step 5: Run frontend tests and build**

```powershell
npm test
npm run build
```

Expected: Vitest 0 failures；TypeScript 与 Vite build exit 0。

- [ ] **Step 6: Commit Task 5**

```powershell
git add frontend\src\types\pywebview.d.ts frontend\src\stores\appStore.ts frontend\src\components\InputArea.vue frontend\src\stores\appStore.tokenUsage.test.ts
git commit -m "feat: display explicit cache rate scopes in GUI"
```

---

### Task 6: 第 1 项完整回归与交付检查

**Files:**
- Modify: `docs/superpowers/plans/2026-08-09-token-cache-metrics.md`（勾选实际完成步骤）

**Interfaces:**
- Consumes: Task 1-5 的全部产物。
- Produces: 可复核的测试、编译和 Git 状态证据。

- [ ] **Step 1: Run all focused Python tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_token_accounting.py tests\test_llm.py tests\test_llm_events_fixes.py tests\test_main_ui_events.py tests\test_gui_token_usage.py tests\test_gui_tool_events.py tests\test_token_usage.py
```

- [ ] **Step 2: Run syntax checks required by AGENTS.md**

```powershell
.\.venv\Scripts\python.exe -m py_compile main.py core\llm.py core\memory.py core\memory_compressor.py core\themes.py gui_bridge.py
```

- [ ] **Step 3: Run complete Python suite**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

- [ ] **Step 4: Run complete frontend suite and build**

```powershell
npm test
npm run build
```

Run both commands in `frontend`.

- [ ] **Step 5: Audit requirements and diff**

```powershell
git diff HEAD~5 --check
git status --short --branch
git log --oneline -7
```

逐项复算测试 fixture `cached=990 / prompt=1000 = 99%`，确认任何代码都没有使用 `cached / total`，并确认非 main 请求不改变 main 快照。

- [ ] **Step 6: Commit plan completion markers**

```powershell
git add docs\superpowers\plans\2026-08-09-token-cache-metrics.md
git commit -m "docs: complete token cache metrics plan"
```
