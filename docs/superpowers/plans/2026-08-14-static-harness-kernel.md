# Y-code Static Harness Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 GUI/TUI、插件体系、模型可见消息和 DeepSeek/Kimi 请求字节的前提下，将 Y-code 主代理循环拆成可审计、可恢复、可独立测试的固定 Harness 内核。

**Architecture:** 保留 `core.llm` 的公开入口和 `messages` 唯一事实源，在其内部引入固定的 TurnRuntime、StepRuntime、RequestEnvelope、StreamAssembler、ToolPipeline 与 TurnJournal。所有生产路径迁移都由 Provider 调用边界黄金字节、现有 UI 事件和工具结果等价测试先行保护；DeepSeek Prompt Ledger 与 Kimi 缓存亲和继续使用当前实现和顺序。

**Tech Stack:** Python 3、SQLite、pytest、LiteLLM、prompt_toolkit；不新增第三方运行时依赖。

## Global Constraints

- 只在 `codex/harness-kernel-upgrade` 分支实施；不得合并到 `master`。
- `messages` 表继续作为会话唯一事实源；Harness Journal 只存状态、ID、耗时和哈希。
- GUI、TUI、命令、插件、MCP、计划、子代理和模型可见工具结果保持不变。
- DeepSeek 官方主会话继续使用现有不可变 Prompt Ledger；不得移动其适用范围、CAS、追加和切代语义。
- Kimi 不新增 Prompt Ledger；不得改变 tools 顺序、reasoning/thinking 字段、缓存亲和材料或 cache control。
- 新 Harness 元数据不得进入 messages、tools、metadata、extra_body 或 system prompt。
- Provider 请求黄金字节必须在任何生产路径修改前落地；出现一字节差异即停止该任务。
- 用户取消顺序保持“先 set cancel event，再 abort response stream”。
- 崩溃恢复只标记 `interrupted`，绝不自动重放模型请求或副作用工具。
- 不保留长期 feature flag、双执行路径或静默 fallback；阶段迁移完成后删除旧内部路径。
- 所有新状态转换、Journal 序列化和数据库写入失败必须显式失败。
- 使用 `apply_patch` 修改文件；保留用户的未跟踪 `.pytest_tmp/`。
- 测试命令统一使用 `.\venv\Scripts\python.exe`。

## File Structure

### New production files

- `core/harness/__init__.py`：导出固定 Harness 的稳定内部接口。
- `core/harness/models.py`：Turn/Step 状态、严格转换、Journal 事件草稿和请求指纹值对象。
- `core/harness/journal.py`：TurnJournal、进程 owner lease、生命周期写入和崩溃恢复协调。
- `core/harness/runtime.py`：TurnRuntime、StepRuntime 和持久化/非持久化运行模式。
- `core/harness/request_envelope.py`：Provider 实际调用参数的无正文指纹快照。
- `core/harness/stream_assembler.py`：流 chunk、reasoning、tool calls、usage 和 finish reason 组装。
- `core/harness/tool_contracts.py`：内置、插件和 MCP 工具契约的严格解析。
- `core/harness/tool_adapters.py`：文件、计划、AskUser、子代理、后台任务、插件和 MCP 固定执行适配器。
- `core/harness/tool_pipeline.py`：单工具管线、重复调用守卫、并发批次和模型顺序提交。

### Modified production files

- `core/memory.py`：additive Harness 表、原子消息/事件提交、会话清理和查询。
- `core/llm.py`：保留公开外观，接入 Runtime/Envelope/Pipeline/Assembler 并删除迁移后的内部实现。
- `core/tool_registry.py`：增加严格执行入口，旧 `execute()` 保持兼容。
- `core/mcp_client.py`：只读导出 MCP schema 查找，不改变连接或调用安全逻辑。
- `core/turn_events.py`：移除 payload 浅拷贝降级，接入受约束的运行时事件快照。
- `AGENTS.md`：记录固定 Harness、Journal 和缓存字节边界。

### New tests

- `tests/request_payload_snapshot.py`
- `tests/test_provider_payload_golden.py`
- `tests/test_harness_models.py`
- `tests/test_harness_journal.py`
- `tests/test_harness_message_commit.py`
- `tests/test_request_envelope.py`
- `tests/test_harness_runtime.py`
- `tests/test_tool_contracts.py`
- `tests/test_tool_pipeline.py`
- `tests/test_stream_assembler.py`

### Existing regression tests used as gates

- `tests/test_prompt_byte_stability.py`
- `tests/test_deepseek_prompt_ledger.py`
- `tests/test_llm.py`
- `tests/test_llm_events.py`
- `tests/test_llm_events_fixes.py`
- `tests/test_agent_turn_e2e.py`
- `tests/test_cancellation.py`
- `tests/test_memory.py`
- `tests/test_mcp_client.py`
- `tests/test_tools.py`

---

### Task 1: Freeze Provider Payload and UI Behavior Baselines

**Files:**
- Create: `tests/request_payload_snapshot.py`
- Create: `tests/test_provider_payload_golden.py`
- Modify: `tests/test_prompt_byte_stability.py`
- Test: `tests/test_deepseek_prompt_ledger.py`
- Test: `tests/test_llm_events.py`
- Test: `tests/test_agent_turn_e2e.py`

**Interfaces:**
- Produces: `snapshot_provider_kwargs(kwargs: dict) -> bytes`
- Produces: deterministic golden assertions for DeepSeek OpenAI, Kimi OpenAI, Kimi Anthropic and Responses requests.
- Consumes: current unmodified `core.llm.chat_with_model_events` and `_request_stream` behavior.

- [ ] **Step 1: Add the test-only byte snapshot helper**

Create `tests/request_payload_snapshot.py` with the exact serializer below. It preserves insertion order, replaces the API key value without moving the key, and rejects unsupported values instead of stringifying them. Define `ALPHA_TOOL` and `ZETA_TOOL` in the same helper module as minimal function schemas with names `alpha` and `zeta`, descriptions equal to their names, and empty object parameters; both golden test files import these shared constants.

```python
import json
from copy import deepcopy


def snapshot_provider_kwargs(kwargs: dict) -> bytes:
    safe = deepcopy(kwargs)
    if "api_key" in safe:
        safe["api_key"] = "<redacted>"
    return json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
```

- [ ] **Step 2: Write full-payload characterization tests**

Create `tests/test_provider_payload_golden.py`. Use a one-chunk fake completion and patch `estimate_tokens` to return `0`, `get_tools_schema` to return two deliberately reverse-ordered tools, model config to a fixed dictionary, and image analysis to a no-op. Capture the complete kwargs received by `completion`.

The expected dictionaries must list every key in insertion order. The DeepSeek non-thinking case must use this shape:

```python
expected = {
    "model": "openai/deepseek-v4-pro",
    "api_key": "<redacted>",
    "messages": [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "inspect"},
    ],
    "tools": [ALPHA_TOOL, ZETA_TOOL],
    "stream": True,
    "stream_options": {"include_usage": True},
    "num_retries": 2,
    "extra_body": {"thinking": {"type": "disabled"}},
    "tool_choice": "auto",
    "max_tokens": 8192,
    "api_base": "https://api.deepseek.com",
    "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
}
assert snapshot_provider_kwargs(captured[0]) == snapshot_provider_kwargs(expected)
```

Use one helper whose only assertion is a complete byte comparison:

```python
def assert_exact_provider_kwargs(captured, expected):
    assert len(captured) == 1
    assert snapshot_provider_kwargs(captured[0]) == snapshot_provider_kwargs(expected)
```

Create five named tests: `test_deepseek_non_thinking_provider_kwargs_are_byte_golden`, `test_deepseek_thinking_tool_round_provider_kwargs_are_byte_golden`, `test_kimi_openai_provider_kwargs_keep_tool_order_and_cache_lane`, `test_kimi_anthropic_provider_kwargs_keep_thinking_and_metadata`, and `test_responses_provider_kwargs_are_byte_golden`. Each test uses the fake-provider setup from `tests/test_deepseek_prompt_ledger.py::test_official_deepseek_real_requests_reuse_previous_payload_prefix`, calls `assert_exact_provider_kwargs`, and supplies a complete expected dictionary. The Kimi OpenAI dictionary keeps `[ZETA_TOOL, ALPHA_TOOL]` and includes `extra_body.prompt_cache_key`; the Kimi Anthropic dictionary keeps the same tool order and includes the exact `thinking`, `allowed_openai_params`, `max_tokens`, and `metadata.user_id` returned for the fixed session `golden-kimi-anthropic`; the Responses dictionary uses translated `input`, translated Responses tools, `max_output_tokens`, and `timeout`. Partial-key assertions are not sufficient.

- [ ] **Step 3: Strengthen stable prompt and tool schema golden guards**

Modify `tests/test_prompt_byte_stability.py` so the serializer does not sort keys. Assert one fixed SHA-256 for `build_stable_prompt(load_system_prompt())` and one for `build_stable_system_message()` under controlled inputs: monkeypatch `load_system_prompt` to `"base"`, KAIROS memory to `"memory"`, active skills to `"skills"`, and custom subagents to `"subagents"`. Calculate both digests once from the pre-refactor branch, paste the literal 64-character digests into the test, and commit them in this task. Do not assert a fixed digest for the process-wide default tool registry because enabled user plugins and MCP schemas are legitimate inputs. Instead, retain the existing default-schema determinism/order tests and add an exact digest for a controlled `ToolRegistry` containing the same fixed `ZETA_TOOL` and `ALPHA_TOOL` contracts used by `test_provider_payload_golden.py`. The test code must use:

```python
def _bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
```

Run the test once to obtain the stable-prompt, controlled full-system-message, and controlled-tool digests only before any production edit. After the literals are committed, no later task may regenerate them automatically. The full runtime tools array remains protected by the complete Provider payload tests under fixed tool inputs and by the existing dynamic registry order tests.

- [ ] **Step 4: Run the baseline characterization suite**

Run:

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_provider_payload_golden.py tests\test_prompt_byte_stability.py tests\test_deepseek_prompt_ledger.py tests\test_llm_events.py tests\test_agent_turn_e2e.py
```

Expected: all tests pass on the unmodified production path. A failing golden assertion means the fixture is wrong; fix the explicit expected dictionary, not production code.

- [ ] **Step 5: Commit the baseline**

```powershell
git add tests/request_payload_snapshot.py tests/test_provider_payload_golden.py tests/test_prompt_byte_stability.py
git commit -m "test: freeze harness provider payloads"
```

---

### Task 2: Add Strict Harness State and Event Models

**Files:**
- Create: `core/harness/__init__.py`
- Create: `core/harness/models.py`
- Create: `tests/test_harness_models.py`

**Interfaces:**
- Produces: `TurnStatus`, `StepStatus`, `HarnessEventKind`, `InvalidHarnessTransition`.
- Produces: `transition_turn(current, target) -> TurnStatus` and `transition_step(current, target) -> StepStatus`.
- Produces: `JournalEventDraft` and `RequestFingerprint` immutable dataclasses.
- Consumes: Python standard library only.

- [ ] **Step 1: Write failing transition and validation tests**

Create `tests/test_harness_models.py` with these behaviors:

```python
import pytest

from core.harness.models import (
    HarnessEventKind,
    InvalidHarnessTransition,
    JournalEventDraft,
    StepStatus,
    TurnStatus,
    transition_step,
    transition_turn,
)


def test_turn_terminal_state_cannot_change():
    with pytest.raises(InvalidHarnessTransition):
        transition_turn(TurnStatus.COMPLETED, TurnStatus.RUNNING)


def test_step_tool_path_is_explicit():
    state = transition_step(StepStatus.ASSISTANT_READY, StepStatus.TOOLS_RUNNING)
    assert transition_step(state, StepStatus.TOOLS_COMMITTED) is StepStatus.TOOLS_COMMITTED


def test_journal_event_rejects_raw_payload():
    with pytest.raises(TypeError):
        JournalEventDraft(kind=HarnessEventKind.TOOL_STARTED, payload={"secret": "x"})
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_harness_models.py
```

Expected: import failure because `core.harness.models` does not exist.

- [ ] **Step 3: Implement the exact state vocabulary**

Implement these enum values in `core/harness/models.py`:

```python
class TurnStatus(str, Enum):
    CREATED = "created"
    PREPARING = "preparing"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class StepStatus(str, Enum):
    CREATED = "created"
    REQUESTING = "requesting"
    STREAMING = "streaming"
    ASSISTANT_READY = "assistant_ready"
    TOOLS_RUNNING = "tools_running"
    TOOLS_COMMITTED = "tools_committed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
```

Define explicit allowed-transition dictionaries. `transition_turn` and `transition_step` return the target on success and raise `InvalidHarnessTransition` with current and target values on failure.

Define `HarnessEventKind` with: `TURN_CREATED`, `TURN_STATUS`, `STEP_CREATED`, `STEP_STATUS`, `REQUEST_CAPTURED`, `ASSISTANT_COMMITTED`, `TOOL_STARTED`, `TOOL_FINISHED`, `TOOL_RESULT_COMMITTED`, `TURN_ERROR`.

Define `JournalEventDraft` as a frozen dataclass whose first required field is `kind: HarnessEventKind`, followed by these optional scalar fields defaulting to `None`: `step_id`, `message_index`, `tool_name`, `call_ordinal`, `arguments_sha256`, `result_sha256`, `duration_ms`, `result_status`, `error_code`. Do not add a generic payload field.

Define `RequestFingerprint` as a frozen dataclass containing `messages_sha256`, `system_sha256`, `tools_sha256`, `options_sha256`, `cache_lane_sha256`, `message_count`, `system_message_count`, `request_bytes`, `system_bytes`, `tools_bytes`, and `options_bytes`.

- [ ] **Step 4: Run model tests**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_harness_models.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit the models**

```powershell
git add core/harness/__init__.py core/harness/models.py tests/test_harness_models.py
git commit -m "feat: add strict harness state models"
```

---

### Task 3: Add Harness Journal Storage and Owner Leases

**Files:**
- Create: `core/harness/journal.py`
- Create: `tests/test_harness_journal.py`
- Modify: `core/memory.py:create_tables`
- Modify: `core/memory.py:MemoryManager`

**Interfaces:**
- Produces: module-level `PROCESS_OWNER_ID = f"harness-{uuid.uuid4().hex}"`, created once per Python process and never included in Provider payloads.
- Produces: `HarnessJournal(manager: MemoryManager, owner_id: str, clock_ms: Callable[[], int])`.
- Produces: `HarnessJournal.create_turn(turn_id: str, session_name: str) -> None`, `create_step(step_id: str, turn_id: str, ordinal: int) -> None`.
- Produces: `HarnessJournal.transition_turn(turn_id: str, expected: TurnStatus, target: TurnStatus, *, error_code: str | None = None) -> None`.
- Produces: `HarnessJournal.transition_step(step_id: str, expected: StepStatus, target: StepStatus, *, duration_ms: int | None = None, finish_reason: str | None = None, error_code: str | None = None) -> None`.
- Produces: `HarnessJournal.append_event(turn_id: str, draft: JournalEventDraft) -> int`.
- Produces: `MemoryManager.create_harness_turn(turn_id: str, session_name: str, owner_id: str, started_at_ms: int) -> None`.
- Produces: `MemoryManager.create_harness_step(step_id: str, turn_id: str, ordinal: int, started_at_ms: int) -> None`.
- Produces: `MemoryManager.transition_harness_turn(turn_id: str, expected_status: str, target_status: str, now_ms: int, *, error_code: str | None = None) -> None`.
- Produces: `MemoryManager.transition_harness_step(step_id: str, expected_status: str, target_status: str, now_ms: int, *, duration_ms: int | None = None, finish_reason: str | None = None, error_code: str | None = None) -> None`.
- Produces: `MemoryManager.append_harness_event(turn_id: str, draft: JournalEventDraft, created_at_ms: int) -> int`.
- Produces: `heartbeat_harness_owner(owner_id, now_ms)` and `interrupt_stale_harness_runs(stale_before_ms) -> int`.
- Consumes: Task 2 enums and `JournalEventDraft`.

- [ ] **Step 1: Write failing schema, sequence, CAS and lease tests**

Create `tests/test_harness_journal.py` with concrete tests for:

```python
def test_harness_tables_are_additive(tmp_path):
    manager = MemoryManager(str(tmp_path / "journal.db"))
    names = {
        row[0]
        for row in manager.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"harness_owners", "harness_turns", "harness_steps", "harness_events"} <= names


def test_existing_messages_are_not_backfilled_as_harness_turns(tmp_path):
    path = tmp_path / "upgrade.db"
    before = MemoryManager(str(path))
    before.add_message("legacy", "user", "old message")
    before.close()
    after = MemoryManager(str(path))
    assert after.get_history("legacy") == [{"role": "user", "content": "old message"}]
    assert after.conn.execute("SELECT COUNT(*) FROM harness_turns").fetchone() == (0,)


def test_event_table_has_no_generic_or_raw_content_columns(tmp_path):
    manager, _journal = _journal(tmp_path, now_ms=10_000)
    columns = {
        row[1] for row in manager.conn.execute("PRAGMA table_info(harness_events)")
    }
    assert {"payload", "content", "arguments", "result", "reasoning"}.isdisjoint(columns)


def test_parallel_event_appends_allocate_unique_monotonic_sequences(tmp_path):
    manager, journal = _journal(tmp_path, now_ms=10_000)
    barrier = threading.Barrier(8)

    def append(index):
        barrier.wait()
        return journal.append_event("turn-1", JournalEventDraft(
            kind=HarnessEventKind.TOOL_STARTED,
            tool_name=f"tool-{index}",
            call_ordinal=index,
        ))

    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(append, range(8)))
    assert sorted(sequences) == list(range(1, 9))


def test_transition_rejects_stale_expected_status(tmp_path):
    manager, journal = _journal(tmp_path, now_ms=10_000)
    journal.create_turn("turn-1", "session")
    journal.transition_turn("turn-1", TurnStatus.CREATED, TurnStatus.PREPARING)
    with pytest.raises(RuntimeError, match="stale harness turn transition"):
        journal.transition_turn("turn-1", TurnStatus.CREATED, TurnStatus.RUNNING)
    assert manager.get_harness_turn("turn-1")["status"] == "preparing"


def test_only_expired_owner_turns_become_interrupted(tmp_path):
    manager = MemoryManager(str(tmp_path / "lease.db"))
    manager.create_session_explicit("session")
    live = HarnessJournal(manager, "owner-live", clock_ms=lambda: 19_500)
    dead = HarnessJournal(manager, "owner-dead", clock_ms=lambda: 1_000)
    live.create_turn("turn-live", "session")
    dead.create_turn("turn-dead", "session")
    live.recover_stale(now_ms=20_000)
    assert manager.get_harness_turn("turn-live")["status"] == "created"
    assert manager.get_harness_turn("turn-dead")["status"] == "interrupted"
```

The `_journal` test helper creates `MemoryManager(tmp_path / "journal.db")`, creates session `session`, instantiates `HarnessJournal` with owner `owner-test`, registers its heartbeat, creates `turn-1`, and returns both objects. No sleeps are allowed; inject integer clocks.

- [ ] **Step 2: Run the Journal tests to verify RED**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_harness_journal.py
```

Expected: missing tables and methods.

- [ ] **Step 3: Add the exact additive schema**

In `MemoryManager.create_tables`, create:

```sql
CREATE TABLE IF NOT EXISTS harness_owners (
    owner_id TEXT PRIMARY KEY,
    heartbeat_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS harness_turns (
    turn_id TEXT PRIMARY KEY,
    session_id INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    last_sequence INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(owner_id) REFERENCES harness_owners(owner_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS harness_steps (
    step_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    protocol TEXT,
    model TEXT,
    api_base TEXT,
    cache_scope TEXT,
    messages_sha256 TEXT,
    system_sha256 TEXT,
    tools_sha256 TEXT,
    options_sha256 TEXT,
    cache_lane_sha256 TEXT,
    message_count INTEGER,
    system_message_count INTEGER,
    request_bytes INTEGER,
    system_bytes INTEGER,
    tools_bytes INTEGER,
    options_bytes INTEGER,
    request_reason TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    deepseek_generation_id INTEGER,
    started_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    duration_ms INTEGER,
    finish_reason TEXT,
    error_code TEXT,
    UNIQUE(turn_id, ordinal),
    FOREIGN KEY(turn_id) REFERENCES harness_turns(turn_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS harness_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL,
    step_id TEXT,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    message_id INTEGER,
    tool_name TEXT,
    call_ordinal INTEGER,
    arguments_sha256 TEXT,
    result_sha256 TEXT,
    duration_ms INTEGER,
    result_status TEXT,
    error_code TEXT,
    UNIQUE(turn_id, sequence),
    FOREIGN KEY(turn_id) REFERENCES harness_turns(turn_id) ON DELETE CASCADE,
    FOREIGN KEY(step_id) REFERENCES harness_steps(step_id) ON DELETE CASCADE,
    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE SET NULL
);
```

Enable `PRAGMA foreign_keys=ON` immediately after connecting, before table creation. Add indexes on `harness_turns(session_id, status)`, `harness_steps(turn_id, ordinal)` and `harness_events(turn_id, sequence)`.

- [ ] **Step 4: Implement strict low-level storage methods**

Turn transitions must use `UPDATE harness_turns SET status=? WHERE turn_id=? AND status=?`; Step transitions use the corresponding `harness_steps` statement. Both require `cursor.rowcount == 1`. Event append must increment `harness_turns.last_sequence` and insert the event in the same transaction. `JournalEventDraft` fields map one-to-one to columns; no JSON column is permitted.

Use the exact signatures in the Interfaces block. Every method executes complete SQL under `self._lock`, commits only after all related statements succeed, and rolls back then re-raises on failure.

- [ ] **Step 5: Implement owner lease recovery**

`HarnessJournal` must register its owner, throttle heartbeats to once per 2,000 ms, and consider an owner stale after 10,000 ms. `recover_stale()` updates only nonterminal rows whose owner heartbeat is older than `now_ms - 10_000`. It must update Step rows before Turn rows in one transaction and append no fabricated tool events. `MemoryManager.__init__` invokes the same locked stale-row update once after table creation; subsequent throttled Harness heartbeats also call `recover_stale`, so a crash less than 10 seconds before restart converges to interrupted without a background thread.

No background thread is introduced. `TurnRuntime` will call `heartbeat()` from existing request-wait and stream-cancellation checkpoints in Task 6.

- [ ] **Step 6: Run Journal and existing memory tests**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_harness_journal.py tests\test_memory.py tests\test_deepseek_prompt_ledger.py
```

Expected: all tests pass; DeepSeek tables and behavior are unchanged.

- [ ] **Step 7: Commit Journal storage**

```powershell
git add core/harness/journal.py core/memory.py tests/test_harness_journal.py
git commit -m "feat: persist harness lifecycle journal"
```

---

### Task 4: Commit Messages and Journal References Atomically

**Files:**
- Create: `tests/test_harness_message_commit.py`
- Modify: `core/memory.py:add_messages`
- Modify: `core/memory.py:clear_session_messages`
- Modify: `core/memory.py:delete_session`
- Modify: `core/memory.py:undo_last_interaction`
- Modify: `core/harness/journal.py`

**Interfaces:**
- Produces: `MemoryManager.add_messages_with_harness_events(session_name, messages, *, turn_id, step_id, events) -> tuple[int, ...]`.
- Produces: `HarnessJournal.commit_messages(session_name: str, turn_id: str, step_id: str, messages: list[tuple[str, object]], events: list[JournalEventDraft]) -> tuple[int, ...]`.
- Consumes: `JournalEventDraft.message_index` as a zero-based index into the submitted batch.

- [ ] **Step 1: Write failing atomicity and lifecycle tests**

Create `tests/test_harness_message_commit.py` covering:

```python
def test_message_ids_and_committed_events_share_one_transaction(tmp_path):
    manager, journal, turn_id, step_id = _started_step(tmp_path)
    messages = [("assistant", {"content": "a"}), ("tool", {
        "tool_call_id": "c1", "name": "read_file", "content": "r",
    })]
    ids = journal.commit_messages("session", turn_id, step_id, messages, [
        JournalEventDraft(HarnessEventKind.ASSISTANT_COMMITTED, step_id=step_id, message_index=0),
        JournalEventDraft(HarnessEventKind.TOOL_RESULT_COMMITTED, step_id=step_id, message_index=1),
    ])
    linked = [row[0] for row in manager.conn.execute(
        "SELECT message_id FROM harness_events WHERE message_id IS NOT NULL ORDER BY sequence"
    )]
    assert tuple(linked) == ids


def test_event_insert_failure_rolls_back_message_batch(tmp_path):
    manager = MemoryManager(str(tmp_path / "atomic.db"))
    journal, turn_id, step_id = _attach_started_step(manager, "session")
    manager.conn.execute(
        "CREATE TRIGGER fail_harness_event BEFORE INSERT ON harness_events "
        "BEGIN SELECT RAISE(ABORT, 'event write failed'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="event write failed"):
        journal.commit_messages("session", turn_id, step_id, [("assistant", "answer")], [
            JournalEventDraft(HarnessEventKind.ASSISTANT_COMMITTED, step_id=step_id, message_index=0),
        ])
    assert manager.get_history("session") == []


def test_clear_session_removes_turn_journal(tmp_path):
    manager, journal, turn_id, step_id = _started_step(tmp_path)
    journal.commit_messages("session", turn_id, step_id, [("assistant", "answer")], [
        JournalEventDraft(HarnessEventKind.ASSISTANT_COMMITTED, step_id=step_id, message_index=0),
    ])
    manager.clear_session_messages("session")
    assert manager.get_history("session") == []
    assert manager.get_harness_turn(turn_id) is None


def test_undo_keeps_event_but_nulls_deleted_message_reference(tmp_path):
    manager, journal, turn_id, step_id = _started_step(tmp_path)
    manager.add_message("session", "user", "question")
    journal.commit_messages("session", turn_id, step_id, [("assistant", "answer")], [
        JournalEventDraft(HarnessEventKind.ASSISTANT_COMMITTED, step_id=step_id, message_index=0),
    ])
    manager.undo_last_interaction("session")
    row = manager.conn.execute(
        "SELECT message_id FROM harness_events WHERE kind='assistant_committed'"
    ).fetchone()
    assert row == (None,)
```

- [ ] **Step 2: Run tests to verify RED**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_harness_message_commit.py
```

Expected: missing atomic commit interface.

- [ ] **Step 3: Refactor message insertion behind one locked helper**

Add a private `_insert_message_batch_locked(cursor, session_name, serialized)` returning inserted IDs in order. Use one `INSERT` per row so each `cursor.lastrowid` is captured. Keep `add_messages()` public return behavior unchanged; it calls the helper and discards IDs.

Implement `add_messages_with_harness_events` so it:

1. validates every `message_index` before opening the transaction;
2. inserts the whole message batch;
3. resolves each draft's `message_index` to a real ID;
4. allocates monotonically increasing Turn sequences;
5. inserts all committed events;
6. updates session timestamp;
7. commits once.

Any exception calls `rollback()` and re-raises. Do not catch and convert the error to a warning.

- [ ] **Step 4: Define clear, delete, rename and undo behavior**

- Session rename requires no Harness row rewrite because Journal uses `session_id`.
- `clear_session_messages` deletes Harness turns for the session before deleting messages, then clears DeepSeek generations in the existing transaction.
- `delete_session` relies on foreign-key cascade for Harness rows and retains the current explicit DeepSeek cleanup.
- `undo_last_interaction` deletes messages; `ON DELETE SET NULL` preserves Journal metadata without a dangling reference.

Add assertions for all four operations.

- [ ] **Step 5: Run persistence tests**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_harness_message_commit.py tests\test_harness_journal.py tests\test_memory.py tests\test_llm_events.py -k "persistence or completed_tool_turn or cancelled_tool_turn or harness"
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit atomic persistence**

```powershell
git add core/memory.py core/harness/journal.py tests/test_harness_message_commit.py
git commit -m "feat: commit harness messages atomically"
```

---

### Task 5: Capture the Actual Provider Request with RequestEnvelope

**Files:**
- Create: `core/harness/request_envelope.py`
- Create: `tests/test_request_envelope.py`
- Modify: `core/llm.py:_request_stream`
- Modify: `core/llm.py:_chat_with_model_generator`
- Test: `tests/test_provider_payload_golden.py`

**Interfaces:**
- Produces: `PreparedStreamRequest(protocol: str, kwargs: dict)`.
- Produces: `prepare_stream_request(protocol: str, chat_kwargs: dict, timeout: int) -> PreparedStreamRequest`.
- Produces: `RequestEnvelope.capture(prepared: PreparedStreamRequest, *, cache_scope: str, request_reason: str, attempt: int, deepseek_generation_id: int | None, deepseek_message_count: int | None) -> RequestEnvelope`.
- Consumes: Task 2 `RequestFingerprint`.

- [ ] **Step 1: Write failing envelope tests**

Create `tests/test_request_envelope.py` with exact assertions:

```python
def test_envelope_excludes_api_key_and_raw_content():
    prepared = PreparedStreamRequest("openai", {
        "model": "m",
        "api_key": "sk-secret-value",
        "messages": [{"role": "user", "content": "private body"}],
        "tools": [],
        "timeout": 90,
    })
    envelope = RequestEnvelope.capture(
        prepared,
        cache_scope="main",
        request_reason="冷启动",
        attempt=1,
        deepseek_generation_id=None,
        deepseek_message_count=None,
    )
    assert "secret" not in repr(envelope)
    assert "private body" not in repr(envelope)
    assert len(envelope.fingerprint.messages_sha256) == 64


def test_envelope_hash_changes_when_key_order_changes():
    first = [{"role": "assistant", "content": "x", "reasoning_content": ""}]
    second = [{"content": "x", "role": "assistant", "reasoning_content": ""}]
    assert capture_messages_sha(first) != capture_messages_sha(second)
```

Add a Responses test asserting the Envelope hashes the translated `input` and Responses `tools`, not the pre-translation chat fields.

- [ ] **Step 2: Run tests to verify RED**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_request_envelope.py
```

Expected: missing request envelope module.

- [ ] **Step 3: Extract one request-preparation function**

Move only the deterministic kwargs construction from `_request_stream` into `prepare_stream_request`. For chat protocols, copy `chat_kwargs` in insertion order and append `timeout`. For Responses, build the current `response_kwargs` in its current key order. `_request_stream` must accept `PreparedStreamRequest` and call the same Provider function with `**prepared.kwargs`.

Do not deepcopy or canonicalize the dict before sending it.

- [ ] **Step 4: Implement RequestEnvelope hashing**

Use insertion-order JSON with `sort_keys=False`, compact separators and `allow_nan=False`. Hash messages/input, system messages, tools and options separately. Options exclude `api_key`, `messages`, `input` and `tools`; no secret-derived hash is persisted.

Store counts and byte lengths, protocol, cache scope, request reason, one-based API attempt, DeepSeek generation ID and DeepSeek message count. When `extra_body.prompt_cache_key` or `metadata.user_id` exists, store only its SHA-256 in `cache_lane_sha256`; do not retain the raw value. The dataclass contains no raw messages, tools or options.

- [ ] **Step 5: Insert Envelope capture before network submission**

In the retry loop:

1. create `PreparedStreamRequest` after final messages, tools, options and timeout are fixed;
2. capture `RequestEnvelope` from that exact object;
3. pass the same `PreparedStreamRequest` object to `_request_stream`;
4. keep the Envelope local until Task 6 connects it to StepRuntime.

No Envelope field may be written into `kwargs`.

- [ ] **Step 6: Run byte gates**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_request_envelope.py tests\test_provider_payload_golden.py tests\test_prompt_byte_stability.py tests\test_deepseek_prompt_ledger.py tests\test_llm.py
```

Expected: every golden byte assertion passes without fixture changes.

- [ ] **Step 7: Commit request capture**

```powershell
git add core/harness/request_envelope.py core/llm.py tests/test_request_envelope.py
git commit -m "feat: capture immutable provider request envelopes"
```

---

### Task 6: Integrate TurnRuntime and StepRuntime

**Files:**
- Create: `core/harness/runtime.py`
- Create: `tests/test_harness_runtime.py`
- Modify: `core/llm.py:chat_with_model_events`
- Modify: `core/llm.py:_chat_with_model_generator`
- Modify: `core/turn_events.py`
- Test: `tests/test_cancellation.py`
- Test: `tests/test_agent_turn_e2e.py`

**Interfaces:**
- Produces: `TurnRuntime.start(turn_id: str, session_name: str | None, *, persist: bool, manager: MemoryManager | None, cancel_event: threading.Event | None, clock_ms: Callable[[], int]) -> TurnRuntime`.
- Produces: `TurnRuntime.begin_step() -> StepRuntime`.
- Produces: `StepRuntime.capture_request(envelope)`, `mark_streaming()`, `mark_assistant_ready()`, `mark_tools_running()`, `mark_tools_committed()`, `complete()`, `fail(code)`, `interrupt()`.
- Consumes: `HarnessJournal`, `RequestEnvelope`, Task 2 transitions.

- [ ] **Step 1: Write failing runtime lifecycle tests**

Create `tests/test_harness_runtime.py` covering:

```python
def test_persistent_turn_records_two_model_steps_for_one_tool_round(tmp_path, mocker):
    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit("session")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    mocker.patch("core.llm.completion", side_effect=[
        _tool_stream("call-1", "read_file", {"file_path": str(tmp_path / "a.py")}),
        _text_stream("done"),
    ])
    (tmp_path / "a.py").write_text("body", encoding="utf-8")
    list(chat_with_model_events(
        "fake-model", "key", [{"role": "user", "content": "read"}],
        persist=True, parent_session="session", turn_id="turn-runtime",
    ))
    turn = manager.get_harness_turn("turn-runtime")
    steps = manager.list_harness_steps("turn-runtime")
    assert turn["status"] == "completed"
    assert [(item["ordinal"], item["status"]) for item in steps] == [
        (1, "completed"), (2, "completed"),
    ]


def test_nonpersistent_turn_uses_no_database_journal(mocker):
    manager = MemoryManager(":memory:")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    mocker.patch("core.llm.completion", return_value=_text_stream("answer"))
    events = list(chat_with_model_events(
        "fake-model", "key", [{"role": "user", "content": "q"}],
        persist=False, turn_id="turn-ephemeral",
    ))
    assert [event.kind for event in events] == ["assistant_delta"] * 6 + ["turn_finished"]
    assert manager.get_harness_turn("turn-ephemeral") is None


def test_user_cancel_sets_cancelled_not_interrupted(mocker):
    manager, cancel_event = _persistent_runtime_fixture(mocker)
    mocker.patch("core.llm.completion", return_value=_cancelling_stream(cancel_event))
    with pytest.raises(KeyboardInterrupt):
        list(_persistent_events(cancel_event=cancel_event))
    assert manager.get_harness_turn("turn-runtime")["status"] == "cancelled"
    assert manager.list_harness_steps("turn-runtime")[-1]["status"] == "cancelled"


def test_provider_error_sets_failed_and_keeps_turn_error_event(mocker):
    manager, _cancel_event = _persistent_runtime_fixture(mocker)
    mocker.patch("core.llm.completion", side_effect=RuntimeError("provider failed"))
    mocker.patch("core.llm.time.sleep", return_value=None)
    events = list(_persistent_events())
    assert [event.kind for event in events].count("turn_error") == 1
    assert manager.get_harness_turn("turn-runtime")["status"] == "failed"
```

Define `_tool_stream`, `_text_stream`, `_cancelling_stream`, `_persistent_runtime_fixture`, and `_persistent_events` in the test file using the existing `SimpleNamespace` chunk builders from `tests/test_agent_turn_e2e.py`. Keep their IDs, session and model values fixed as shown above.

- [ ] **Step 2: Run runtime tests to verify RED**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_harness_runtime.py
```

Expected: missing runtime module or no Journal rows.

- [ ] **Step 3: Implement persistent and ephemeral runtime modes**

`TurnRuntime.start` requires a manager and session when `persist=True`; missing storage raises `RuntimeError`. It constructs `HarnessJournal` with Task 3's process-wide `PROCESS_OWNER_ID`. When `persist=False`, Runtime maintains states in memory and performs no Journal call. This is an explicit mode, not a storage failure fallback.

Generate Step IDs as `f"{turn_id}:step:{ordinal}"`. `begin_step` increments ordinal exactly once per outer model/tool loop, not per API retry.

Call `HarnessJournal.heartbeat()` from existing `raise_if_cancelled` checkpoints; the 2-second throttle prevents write amplification.

- [ ] **Step 4: Wire Runtime into the existing public adapter**

`chat_with_model_events` creates the Runtime using the already resolved turn ID and captured `parent_session`. Pass it to `_chat_with_model_generator` through a private keyword-only parameter named `harness_runtime`; do not change the public function signature.

State mapping:

- before preparation: Turn `created -> preparing`;
- before outer loop: Turn `preparing -> running`;
- each outer iteration: create Step and set `requesting`;
- after response stream opens: `streaming`;
- after assistant message assembly: `assistant_ready`;
- before tools: `tools_running`;
- after ordered tool messages commit: `tools_committed -> completed`;
- final assistant: Step completed, then Turn completed;
- KeyboardInterrupt: current Step and Turn cancelled;
- invariant/provider/persistence error: current Step and Turn failed.

Capture the Task 5 Envelope on the current Step before `_request_stream` starts. API retries update the same Step request fingerprint and retry counter; they do not create new Steps.

- [ ] **Step 5: Replace arbitrary TurnEvent payload fallback**

Change `TurnEventFactory.emit` so `deepcopy` failure raises `TypeError` with event kind. Keep the public `TurnEvent.payload` mapping shape and sequence behavior. Add a regression test replacing the current lock-payload fallback test with an explicit `pytest.raises(TypeError)` assertion.

- [ ] **Step 6: Make persistence failures fail the Turn**

Replace the current `flush_persistence` behavior that retains/skips on missing manager or catches `add_messages` errors. Persistent mode must call Runtime/Journal atomic commit and propagate the exception. Cancellation salvage still constructs legal tool results before committing.

- [ ] **Step 7: Run runtime, event, cancellation and byte tests**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_harness_runtime.py tests\test_turn_events.py tests\test_cancellation.py tests\test_agent_turn_e2e.py tests\test_llm_events.py tests\test_llm_events_fixes.py tests\test_provider_payload_golden.py tests\test_deepseek_prompt_ledger.py
```

Expected: existing public event lists and Provider bytes remain equal; new Journal assertions pass.

- [ ] **Step 8: Commit runtime integration**

```powershell
git add core/harness/runtime.py core/llm.py core/turn_events.py tests/test_harness_runtime.py tests/test_turn_events.py
git commit -m "feat: run model turns through harness runtime"
```

---

### Task 7: Normalize Built-in, Plugin and MCP Tool Contracts

**Files:**
- Create: `core/harness/tool_contracts.py`
- Create: `tests/test_tool_contracts.py`
- Modify: `core/tool_registry.py`
- Modify: `core/mcp_client.py`
- Test: `tests/test_mcp_client.py`

**Interfaces:**
- Produces: `ResolvedTool(spec: ToolSpec, source: Literal["registry", "mcp"], execute: Callable)`.
- Produces: `resolve_tool(name, registry, mcp_manager) -> ResolvedTool`.
- Produces: `ToolRegistry.execute_strict(name, **arguments) -> ToolResult`.
- Consumes: existing ToolSpec registrations and MCP schemas.

- [ ] **Step 1: Write failing strict contract tests**

Create `tests/test_tool_contracts.py`:

```python
import pytest


def test_registry_tool_preserves_plugin_metadata():
    spec = ToolSpec(
        name="plugin_tool", description="plugin", input_schema={"type": "object"},
        execute=lambda: ToolResult("ok"), requires_permission=True, parallel_safe=True,
    )
    registry = ToolRegistry([spec])
    resolved = resolve_tool("plugin_tool", registry, FakeMcpManager([]))
    assert resolved.spec is spec
    assert resolved.source == "registry"


def test_mcp_tool_gets_explicit_nonparallel_contract():
    schema = {"type": "function", "function": {
        "name": "remote", "description": "remote", "parameters": {"type": "object"},
    }}
    resolved = resolve_tool("remote", ToolRegistry(), FakeMcpManager([schema]))
    assert resolved.source == "mcp"
    assert resolved.spec.read_only is False
    assert resolved.spec.requires_permission is False
    assert resolved.spec.parallel_safe is False


def test_unknown_tool_fails_contract_resolution():
    with pytest.raises(KeyError, match="unknown tool"):
        resolve_tool("missing", ToolRegistry(), FakeMcpManager([]))


def test_execute_strict_does_not_convert_executor_exception():
    def fail():
        raise ValueError("boom")

    registry = ToolRegistry([ToolSpec(
        name="fail", description="fail", input_schema={"type": "object"}, execute=fail,
    )])
    with pytest.raises(ValueError, match="boom"):
        registry.execute_strict("fail")
```

Define `FakeMcpManager.get_schema(name)` to return the matching schema and `call_tool_sync(name, arguments)` to return a fixed string. It performs no connection or mutation.

- [ ] **Step 2: Run contract tests to verify RED**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_tool_contracts.py
```

Expected: missing resolver and strict registry method.

- [ ] **Step 3: Add strict registry execution without breaking compatibility**

Refactor schema-based argument filtering into `_filtered_arguments(spec, arguments)`. Existing `execute()` continues returning `ToolResult(is_error=True)` for unknown tools and executor exceptions. New `execute_strict()` uses the same filtering but raises unknown-tool and executor exceptions.

The Harness pipeline must use `execute_strict`; external legacy callers may continue using `execute`.

- [ ] **Step 4: Implement deterministic MCP lookup**

Add `MCPManager.get_schema(tool_name) -> dict | None` using the current schema list under its existing lock. Do not connect, reconnect or mutate schemas in this method.

`resolve_tool` checks ToolRegistry first, then MCP. MCP adapters use explicit current-compatible metadata: `read_only=False`, `mutates_files=False`, `requires_permission=False`, `cancellable=True`, `parallel_safe=False`. This codifies current behavior without a function-name permission guess.

- [ ] **Step 5: Run registry and MCP regression tests**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_tool_contracts.py tests\test_mcp_client.py tests\test_tools.py tests\test_prompt_byte_stability.py
```

Expected: all tests pass; API schemas and ordering are unchanged.

- [ ] **Step 6: Commit tool contracts**

```powershell
git add core/harness/tool_contracts.py core/tool_registry.py core/mcp_client.py tests/test_tool_contracts.py
git commit -m "feat: normalize harness tool contracts"
```

---

### Task 8: Extract the Single-Tool Execution Pipeline and Adapters

**Files:**
- Create: `core/harness/tool_adapters.py`
- Create: `core/harness/tool_pipeline.py`
- Create: `tests/test_tool_pipeline.py`
- Modify: `core/llm.py:_execute_single_tool_call`
- Test: `tests/test_llm_events_fixes.py`
- Test: `tests/test_agent_turn_e2e.py`

**Interfaces:**
- Produces: `ToolExecutionContext` frozen dataclass.
- Produces: `DecodedToolCall(call_id: str, name: str, arguments: dict, raw_arguments: str)` and `decode_tool_call(tool_call: dict) -> DecodedToolCall`.
- Produces: `ToolPipeline.execute_one(tool_call, context) -> dict`.
- Produces: `ToolAdapterSet.execute(resolved_tool, arguments, context) -> ToolResult`.
- Consumes: Task 7 `resolve_tool`, current permission checker, sandbox guard, plan manager and event callback.

- [ ] **Step 1: Add single-tool parity tests before moving code**

In `tests/test_tool_pipeline.py`, parameterize exact model-visible results for:

- invalid JSON arguments;
- unknown tool;
- read_file;
- denied write_file;
- approved write_file with Diff and Undo;
- edit_file missing target;
- execute_command;
- plan mode denial;
- pending-plan root-tool denial;
- run_subagent disabled;
- AskUser cancellation;
- manage_task;
- plugin ToolSpec;
- MCP tool.

Use the exact tool message shape assertion:

```python
assert result == {
    "role": "tool",
    "tool_call_id": "call-1",
    "name": "read_file",
    "content": expected_content,
}
```

For file mutations, also assert file bytes, undo snapshot and `diff_ready` event payload.

- [ ] **Step 2: Run parity tests on the old implementation**

Point the tests temporarily at `core.llm._execute_single_tool_call` and run:

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_tool_pipeline.py tests\test_agent_turn_e2e.py
```

Expected: characterization tests pass before extraction.

- [ ] **Step 3: Define the execution context**

`ToolExecutionContext` contains explicit fields only:

```python
@dataclass(frozen=True)
class ToolExecutionContext:
    event_callback: Callable | None
    parent_session: str | None
    parent_turn_id: str | None
    cancel_event: threading.Event | None
    allow_subagents: bool
    permission_owner: str | None
    permission_source: str
    permission_checker: Callable
    registry: ToolRegistry
    mcp_manager: object
```

Do not read current session or current Turn implicitly inside the pipeline.

- [ ] **Step 4: Implement fixed adapters**

Move the current branch bodies into named adapter methods:

```text
execute_read_file
execute_write_file
execute_edit_file
execute_run_subagent
execute_ask_user
execute_manage_task
execute_registry_tool
execute_mcp_tool
```

Plan tools, list/search/fetch/weather/command and other ordinary registered tools use `execute_registry_tool`; IMOM path guarding remains an explicit pre-execution policy keyed by its existing names. Preserve current Diff, Undo, result truncation and Chinese text byte-for-byte.

Move `_show_file_diff`, `_get_imom_active_publish_root` and `_sandbox_outside_guard` with the adapters. Keep compatibility wrappers in `core.llm` for tests and external imports.

- [ ] **Step 5: Implement the fixed pipeline order**

`ToolPipeline.execute_one` must perform:

1. `_tool_call_arguments` using the current direct/control-character/truncated JSON sequence;
2. cancellation check;
3. plan constraints;
4. strict contract resolution;
5. permission and plan-mode decision from ToolSpec metadata;
6. sandbox boundary decision;
7. adapter execution;
8. ToolResult error normalization;
9. existing `_truncate_tool_result_for_context`;
10. exact tool message construction.

Do not add argument coercion, permission name fallback or catch-all success conversion. Expected tool execution exceptions become the same explicit `TOOL_ERROR_PREFIX` result as the characterization fixture; invariant and persistence exceptions propagate.

- [ ] **Step 6: Replace the old function with a compatibility wrapper**

`core.llm._execute_single_tool_call` constructs `ToolExecutionContext` from its existing parameters and module-level dependencies, then calls `ToolPipeline.execute_one`. This preserves callers and monkeypatch points while deleting the old `if/elif` body.

- [ ] **Step 7: Run tool and cache gates**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_tool_pipeline.py tests\test_llm_events.py tests\test_llm_events_fixes.py tests\test_agent_turn_e2e.py tests\test_tools.py tests\test_prompt_byte_stability.py tests\test_provider_payload_golden.py
```

Expected: tool messages, UI events and Provider payloads are unchanged.

- [ ] **Step 8: Commit single-tool extraction**

```powershell
git add core/harness/tool_adapters.py core/harness/tool_pipeline.py core/llm.py tests/test_tool_pipeline.py
git commit -m "refactor: extract harness tool execution pipeline"
```

---

### Task 9: Extract Tool Batching, Repeat Guard and Ordered Commit

**Files:**
- Modify: `core/harness/tool_pipeline.py`
- Modify: `core/llm.py:_chat_with_model_generator`
- Modify: `tests/test_tool_pipeline.py`
- Test: `tests/test_llm_events.py`
- Test: `tests/test_agent_turn_e2e.py`

**Interfaces:**
- Produces: `ToolBatchExecutor.execute(tool_calls: list[dict], context: ToolExecutionContext, on_event: Callable[[str, dict], None] | None) -> tuple[ToolExecutionRecord, ...]`.
- Produces: `ToolExecutionRecord(call_ordinal, message, duration_ms)`.
- Consumes: Task 8 `ToolPipeline.execute_one` and current `_RepeatCallGuard` semantics.

- [ ] **Step 1: Write failing batch-order tests**

Add tests asserting:

```python
def test_parallel_completion_order_never_changes_model_result_order():
    first_release = threading.Event()
    second_finished = threading.Event()

    def execute(call, context):
        if call["id"] == "first":
            second_finished.wait(timeout=1)
            first_release.set()
        else:
            second_finished.set()
        return _tool_message(call["id"])

    records = _parallel_executor(execute).execute(
        [_call("first", "read_file"), _call("second", "read_file")],
        _context(), None,
    )
    assert [record.call_ordinal for record in records] == [0, 1]
    assert [record.message["tool_call_id"] for record in records] == ["first", "second"]


def test_only_adjacent_parallel_safe_tools_share_a_batch():
    calls = [_call("r1", "read_file"), _call("w1", "write_file"), _call("r2", "read_file")]
    batches = group_tool_batches(calls, _registry_with_read_and_write())
    assert [[call["id"] for call in batch.calls] for batch in batches] == [
        ["r1"], ["w1"], ["r2"],
    ]


def test_repeat_guard_blocks_third_successful_identical_signature():
    guard = RepeatCallGuard()
    call = _call("one", "read_file", '{"file_path":"a.py"}')
    assert guard.should_execute("read_file", call["function"]["arguments"])
    guard.record("read_file", call["function"]["arguments"], ok=True)
    assert guard.should_execute("read_file", call["function"]["arguments"])
    guard.record("read_file", call["function"]["arguments"], ok=True)
    assert guard.should_execute("read_file", call["function"]["arguments"]) is False


def test_cancel_stops_polling_without_waiting_for_daemon_tool():
    cancel_event = threading.Event()
    blocker = threading.Event()

    def blocked_execute(call, context):
        cancel_event.set()
        blocker.wait(timeout=1)
        return _tool_message(call["id"])

    with pytest.raises(KeyboardInterrupt):
        _parallel_executor(blocked_execute).execute(
            [_call("blocked", "read_file"), _call("also-blocked", "read_file")],
            _context(cancel_event=cancel_event), None,
        )
```

Define `_call`, `_tool_message`, `_context`, `_parallel_executor`, and `_registry_with_read_and_write` as deterministic local test helpers. The executor uses injected callables and Events; no wall-clock sleep above 0.1 seconds is allowed.

- [ ] **Step 2: Run batch tests to verify RED**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_tool_pipeline.py -k "parallel or adjacent or repeat or cancel"
```

Expected: missing batch executor.

- [ ] **Step 3: Move batching and repeat guard into ToolBatchExecutor**

Move `_RepeatCallGuard`, `_repeat_call_blocked_result`, batch grouping, daemon submission, duration capture and cancellation polling from `core.llm` into `core/harness/tool_pipeline.py`.

The executor decodes each call once, applies the shared RepeatCallGuard to the decoded name and canonical arguments, emits `tool_requested` for the whole batch, then `tool_started` for the whole batch, executes, and emits `tool_finished` with actual durations. It returns records sorted by `call_ordinal`, regardless of completion order. This makes the effective order decode -> repeat guard -> plan constraints -> contract resolution -> permission -> execution.

Parallelism remains capped at 4 by the existing daemon executor. Do not use a context-managed ThreadPoolExecutor because cancellation must not wait for blocked tools.

- [ ] **Step 4: Integrate ordered Journal and message commit**

For each result, build a `JournalEventDraft` with tool name, call ordinal, argument SHA-256, result SHA-256, duration and result status. Raw arguments and results stay in memory only. Submit tool messages and `TOOL_RESULT_COMMITTED` drafts through Task 4's atomic interface in model order.

The Step transitions `tools_running -> tools_committed` only after the atomic commit succeeds.

- [ ] **Step 5: Delete old batch code from `core.llm`**

Keep the existing two `yield ""` points surrounding batch execution so GUI/TUI event draining remains unchanged. Replace the old loops with one ToolBatchExecutor call and append its ordered messages.

- [ ] **Step 6: Run concurrency, persistence and event tests**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_tool_pipeline.py tests\test_llm_events.py tests\test_harness_message_commit.py tests\test_harness_runtime.py tests\test_agent_turn_e2e.py tests\test_cancellation.py
```

Expected: all tests pass, including exact tool_finished order and cancelled persistence.

- [ ] **Step 7: Commit batch extraction**

```powershell
git add core/harness/tool_pipeline.py core/llm.py tests/test_tool_pipeline.py
git commit -m "refactor: centralize harness tool batching"
```

---

### Task 10: Extract Provider Stream Assembly

**Files:**
- Create: `core/harness/stream_assembler.py`
- Create: `tests/test_stream_assembler.py`
- Modify: `core/llm.py:_chat_with_model_generator`
- Test: `tests/test_llm_events.py`
- Test: `tests/test_llm_events_fixes.py`

**Interfaces:**
- Produces: `StreamUpdate(public_text: str, reasoning_text: str)`.
- Produces: `AssembledResponse(content, reasoning_content, tool_calls, thinking_blocks, finish_reason, usage)`.
- Produces: `StreamAssembler.consume(chunk) -> StreamUpdate` and `finish() -> AssembledResponse`.
- Consumes: current provider-normalized chat chunks from `_request_stream`.

- [ ] **Step 1: Write failing assembler unit tests**

Create `tests/test_stream_assembler.py` with exact cases for:

- ordinary content chunks;
- native reasoning chunks;
- split tool-call ID/name/arguments;
- missing tool-call IDs receiving current `call_{index}` fallback;
- signed Anthropic thinking blocks;
- incomplete thinking blocks being omitted;
- usage retaining only the final usage chunk;
- finish reason `length`;
- DeepSeek thinking tool call preserving empty-string `reasoning_content`;
- non-reasoning Provider stripping unsupported reasoning fields.

Representative assertion:

```python
response = assembler.finish()
assert response.tool_calls == [{
    "id": "call_0",
    "type": "function",
    "function": {"name": "read_file", "arguments": "{\"file_path\":\"a.py\"}"},
}]
```

- [ ] **Step 2: Run tests to verify RED**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_stream_assembler.py
```

Expected: missing assembler module.

- [ ] **Step 3: Implement the assembler by moving current state variables**

Move `accumulated_content`, `reasoning_content`, `tool_calls_dict`, native thinking state, provider thinking blocks, finish reason and stream usage into `StreamAssembler` instance fields.

`consume` performs the current per-chunk merge and returns only newly public/reasoning text. `finish` performs current tool-call sorting and Provider field normalization exactly once. It raises a protocol error when DeepSeek thinking requires a string `reasoning_content` and the invariant is not satisfied.

- [ ] **Step 4: Replace inline stream state in `core.llm`**

The existing `_stream_chunks_with_idle_notice`, cancellation checks, cache usage persistence and yield timing remain in `core.llm`. Only chunk accumulation and final response construction move. Feed each chunk to the assembler, yield `StreamUpdate.public_text`, and use `AssembledResponse` for assistant/tool processing.

- [ ] **Step 5: Run reasoning, tool and byte gates**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_stream_assembler.py tests\test_llm_events.py tests\test_llm_events_fixes.py tests\test_agent_turn_e2e.py tests\test_provider_payload_golden.py tests\test_deepseek_prompt_ledger.py tests\test_llm.py
```

Expected: all thinking/reasoning/tool/UI assertions and Provider bytes pass unchanged.

- [ ] **Step 6: Commit stream extraction**

```powershell
git add core/harness/stream_assembler.py core/llm.py tests/test_stream_assembler.py
git commit -m "refactor: extract provider stream assembly"
```

---

### Task 11: Remove Dead Internal Paths and Complete Verification

**Files:**
- Modify: `core/llm.py`
- Modify: `core/harness/__init__.py`
- Modify: `AGENTS.md`
- Modify: `docs/superpowers/specs/2026-08-14-static-harness-kernel-design.md` only if implementation discovered a factual mismatch; behavioral scope must not change.
- Test: full `tests/` suite.

**Interfaces:**
- Produces: one public-compatible `core.llm` path backed by the fixed Harness.
- Consumes: Tasks 1-10.

- [ ] **Step 1: Prove old internal paths have no callers**

Run:

```powershell
rg -n "_execute_single_tool_call|_RepeatCallGuard|tool_calls_dict|accumulated_content|flush_persistence" core tests
```

Expected: `_execute_single_tool_call` remains only as the documented compatibility wrapper and its tests; the other moved implementation names exist only in their Harness modules or are absent from `core/llm.py`.

- [ ] **Step 2: Delete dead branches and unused imports**

Remove only code proven unused by Step 1. Do not remove public aliases used by tests or external modules. Run `git diff --check` immediately after cleanup.

- [ ] **Step 3: Update project architecture instructions**

Add a “固定 Harness 内核” section to `AGENTS.md` documenting:

- `core/harness/` component ownership;
- `messages` as the only content fact source;
- Journal's no-body/no-arguments/no-results rule;
- Step equals one real model request, retries stay inside the Step;
- interrupted work is never replayed;
- Provider request byte-golden tests are mandatory before cache-sensitive edits;
- DeepSeek/Kimi route-specific tool order and reasoning invariants.

- [ ] **Step 4: Run syntax checks**

```powershell
.\venv\Scripts\python.exe -m py_compile main.py core\llm.py core\memory.py core\memory_compressor.py core\themes.py core\harness\models.py core\harness\journal.py core\harness\runtime.py core\harness\request_envelope.py core\harness\stream_assembler.py core\harness\tool_contracts.py core\harness\tool_adapters.py core\harness\tool_pipeline.py
```

Expected: exit code 0.

- [ ] **Step 5: Run cache-critical regression suite**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_provider_payload_golden.py tests\test_prompt_byte_stability.py tests\test_deepseek_prompt_ledger.py tests\test_llm.py tests\test_cache_keepalive.py tests\test_cache_request_diagnostics.py tests\test_cache_diagnostics_storage.py tests\test_token_accounting.py
```

Expected: all tests pass; no golden fixture changes after Task 1.

- [ ] **Step 6: Run Harness and UI regression suite**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_harness_models.py tests\test_harness_journal.py tests\test_harness_message_commit.py tests\test_request_envelope.py tests\test_harness_runtime.py tests\test_tool_contracts.py tests\test_tool_pipeline.py tests\test_stream_assembler.py tests\test_llm_events.py tests\test_llm_events_fixes.py tests\test_agent_turn_e2e.py tests\test_cancellation.py tests\test_main_ui_events.py tests\test_gui_tool_events.py
```

Expected: all tests pass.

- [ ] **Step 7: Run the full test suite**

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

Expected: exit code 0. Existing `.pytest_tmp/` permission warnings may be reported, but no test failure may be ignored.

- [ ] **Step 8: Perform optional real cache verification only with user-provided credentials**

Do not run paid API calls without explicit user approval. When approved and the environment variable is already set, run:

```powershell
if (-not $env:YCODE_DEEPSEEK_BENCH_API_KEY) { throw 'YCODE_DEEPSEEK_BENCH_API_KEY 未设置' }
if (-not $env:YCODE_KIMI_BENCH_API_KEY) { throw 'YCODE_KIMI_BENCH_API_KEY 未设置' }
.\venv\Scripts\python.exe cache_benchmark.py --model openai/deepseek-v4-pro --api-base https://api.deepseek.com --api-key-env YCODE_DEEPSEEK_BENCH_API_KEY --rounds 3 --minimum-hot-rate 0
.\venv\Scripts\python.exe cache_benchmark.py --model openai/kimi-for-coding --api-base https://api.kimi.com/coding/v1 --api-key-env YCODE_KIMI_BENCH_API_KEY --rounds 3 --minimum-hot-rate 0
```

Expected: both commands report `passed=True`, zero catastrophic misses after the cold round, and hot rates no lower than the pre-implementation run under the same model/account/time window. If real metrics are unavailable, delivery may claim byte-equivalent cache behavior but must not claim a measured hit-rate improvement.

- [ ] **Step 9: Commit cleanup and documentation**

```powershell
git add core/llm.py core/harness/__init__.py AGENTS.md docs/superpowers/specs/2026-08-14-static-harness-kernel-design.md
git commit -m "docs: document fixed harness invariants"
```

- [ ] **Step 10: Inspect final branch state**

```powershell
git status --short --branch
git log --oneline --decorate -12
```

Expected: branch is `codex/harness-kernel-upgrade`; only the user's pre-existing `.pytest_tmp/` remains untracked; Tasks 1-11 each have a focused commit.

## Completion Gate

Implementation is complete only when all conditions hold:

1. Task 1 golden payload fixtures were committed before the first production edit and never rewritten to accommodate the refactor.
2. All persistent Turn and Step rows reach a terminal state or become interrupted through the owner lease rule.
3. Message-referencing Journal events and their messages commit atomically.
4. No Journal row contains raw user text, assistant text, reasoning, tool arguments, tool results, credentials or Kimi cache lane values.
5. Plugin tools still enter through the existing ToolRegistry and require no plugin changes.
6. DeepSeek Prompt Ledger tests and Kimi cache-affinity/tool-order tests pass unchanged.
7. Provider payload golden bytes, GUI/TUI events and model-visible tool messages are unchanged.
8. No interrupted model request or side-effect tool is automatically replayed.
9. Full syntax and pytest verification pass.
10. Any claimed cache improvement is backed by separately authorized real API measurements; otherwise the claim is limited to deterministic non-regression.
