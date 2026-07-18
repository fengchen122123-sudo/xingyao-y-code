# Y-code Agent Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有启动界面、主题和输入体验的前提下，建立可测试的 Agent turn 生命周期、统一事件流、稳定终端渲染、工具协议、分层系统提示词、结构化计划和可靠中断机制。

**Architecture:** 保留 `main.py` 作为 CLI 外壳，将模型/工具输出转换为带 turn 与 sequence 的 `TurnEvent`，由主 TUI 线程的 `TurnRenderer` 唯一渲染。现有 SQLite、模型管理、技能和 MCP 继续作为基础设施；新协议通过适配器接入，避免一次性重写所有业务。

**Tech Stack:** Python 3.12、prompt_toolkit、LiteLLM、Rich ANSI/HTML、SQLite、pytest。

---

### Task 1: Repair the test and dependency contract

**Files:**
- Modify: `core/tools.py:1-4`
- Modify: `core/memory.py` (after `MemoryManager` definition)
- Modify: `setup.py` dependency list
- Modify: `requirements.txt`
- Test: `tests/test_tools.py`
- Test: `tests/test_memory.py`

- [ ] **Step 1: Write failing tests for the import contract**

Add a test that imports the declared DuckDuckGo package path and a compatibility test that verifies the public memory manager name used by existing callers resolves to the active implementation:

```python
def test_memory_manager_compatibility_name():
    from core.memory import MemoryManager, SessionManager
    assert SessionManager is MemoryManager

def test_declared_search_dependency_exports_ddgs():
    from duckduckgo_search import DDGS
    assert DDGS is not None
```

- [ ] **Step 2: Run the focused tests and verify the expected collection failure**

Run: `pytest tests/test_tools.py tests/test_memory.py -q`

Expected: collection fails because `core.tools` imports `ddgs` while the declared package is `duckduckgo-search`, and `SessionManager` is not exported.

- [ ] **Step 3: Implement the minimal import and compatibility contract**

Use `from duckduckgo_search import DDGS` in `core/tools.py`, add `SessionManager = MemoryManager` after the class definition, and align both dependency manifests to the same package name and version floor.

- [ ] **Step 4: Run the focused tests and the syntax check**

Run: `pytest tests/test_tools.py tests/test_memory.py -q`

Expected: focused tests pass. Run: `venv\Scripts\python -m py_compile main.py core/llm.py core/memory.py core/tools.py core/themes.py`.

- [ ] **Step 5: Commit the isolated baseline repair**

```text
git add core/tools.py core/memory.py setup.py requirements.txt tests/test_tools.py tests/test_memory.py
git commit -m "fix: align runtime dependencies and memory compatibility"
```

### Task 2: Add the event and tool result contracts

**Files:**
- Create: `core/turn_events.py`
- Create: `core/tool_protocol.py`
- Create: `tests/test_turn_events.py`
- Create: `tests/test_tool_protocol.py`

- [ ] **Step 1: Write failing event contract tests**

Test that event sequences are monotonic per turn, event payloads are copied snapshots, and tool results distinguish normal output, errors, changed files, and diffs:

```python
def test_event_factory_assigns_sequence_per_turn():
    from core.turn_events import TurnEventFactory
    factory = TurnEventFactory("turn-1")
    first = factory.emit("assistant_delta", {"text": "a"})
    second = factory.emit("tool_started", {"name": "read_file"})
    assert (first.turn_id, first.sequence) == ("turn-1", 1)
    assert second.sequence == 2

def test_event_payload_is_a_snapshot():
    from core.turn_events import TurnEventFactory
    payload = {"text": "before"}
    event = TurnEventFactory("turn-1").emit("assistant_delta", payload)
    payload["text"] = "after"
    assert event.payload["text"] == "before"

def test_tool_result_keeps_diff_and_error_metadata():
    from core.tool_protocol import ToolResult
    result = ToolResult(content="failed", is_error=True, changed_files=["x.py"], diff="@@")
    assert result.is_error is True
    assert result.changed_files == ["x.py"]
    assert result.diff == "@@"
```

- [ ] **Step 2: Run the tests and verify they fail because the contracts do not exist**

Run: `pytest tests/test_turn_events.py tests/test_tool_protocol.py -q`

Expected: import failures for `core.turn_events` and `core.tool_protocol`.

- [ ] **Step 3: Implement the minimal immutable event and result types**

Define `TurnEvent` as a frozen dataclass with `turn_id`, `sequence`, `kind`, and `payload`; define `TurnEventFactory` with one sequence counter; define `ToolSpec` metadata and `ToolResult` with a `to_message_content()` method that returns only model-facing content.

- [ ] **Step 4: Run the contract tests and the full existing test collection**

Run: `pytest tests/test_turn_events.py tests/test_tool_protocol.py -q` and then `pytest -q`.

Expected: new tests pass; any remaining failures are recorded by file and not hidden.

- [ ] **Step 5: Commit the contracts**

```text
git add core/turn_events.py core/tool_protocol.py tests/test_turn_events.py tests/test_tool_protocol.py
git commit -m "feat: add turn event and tool result contracts"
```

### Task 3: Build the pure terminal turn renderer

**Files:**
- Create: `core/turn_renderer.py`
- Create: `tests/test_turn_renderer.py`
- Modify: `main.py` output integration points around `print_line_segments` and `_background_worker`

- [ ] **Step 1: Write failing renderer tests**

Cover split thinking tags, ordered tool status output, collapsed read-only tools, errors, and finalization:

```python
def test_renderer_does_not_leak_split_thinking_tags():
    from core.turn_renderer import TurnRenderer
    renderer = TurnRenderer()
    renderer.consume("assistant_delta", {"text": "A <thi"})
    renderer.consume("assistant_delta", {"text": "nk>private</think>B"})
    output = renderer.finish()
    assert "<think>" not in output
    assert "<" not in output
    assert "A" in output and "B" in output

def test_renderer_keeps_tool_results_in_requested_order():
    from core.turn_renderer import TurnRenderer
    renderer = TurnRenderer()
    renderer.consume("tool_requested", {"id": "1", "name": "read_file", "preview": "a.py"})
    renderer.consume("tool_requested", {"id": "2", "name": "read_file", "preview": "b.py"})
    renderer.consume("tool_finished", {"id": "1", "name": "read_file", "ok": True})
    renderer.consume("tool_finished", {"id": "2", "name": "read_file", "ok": True})
    output = renderer.finish()
    assert output.index("a.py") < output.index("b.py")
```

- [ ] **Step 2: Run the renderer tests and verify the expected failures**

Run: `pytest tests/test_turn_renderer.py -q`

Expected: import failure or failed assertions because no renderer contract exists.

- [ ] **Step 3: Implement a pure renderer state machine**

Implement `consume(kind, payload) -> list[str]` and `finish() -> str` without terminal I/O. Buffer incomplete `<think>`/`<thinking>` tags, discard private thinking text, preserve normal text, render tool statuses with stable prefixes, and render errors once. The renderer must never call `print`, Rich Console, or prompt_toolkit.

- [ ] **Step 4: Run renderer tests and refactor only after green**

Run: `pytest tests/test_turn_renderer.py -q` and require PASS before changing integration code.

- [ ] **Step 5: Commit the pure renderer**

```text
git add core/turn_renderer.py tests/test_turn_renderer.py
git commit -m "feat: add deterministic turn renderer"
```

### Task 4: Convert the LLM/tool loop to ordered events

**Files:**
- Modify: `core/llm.py` functions `_execute_single_tool_call`, `_chat_with_model_generator`, and `chat_with_model`
- Modify: `core/tools.py` tool execution adapters
- Modify: `main.py` `_background_worker` and UI callback path
- Create: `tests/test_llm_events.py`

- [ ] **Step 1: Write a failing event-stream test using a fake completion stream**

The fake stream must produce assistant text, two consecutive read-only tool calls, and a final assistant response. Assert that the event generator emits request/start/finish events in model order and that no event handler prints directly.

```python
def test_event_stream_orders_parallel_read_tools(mocker):
    from core.llm import chat_with_model_events
    events = list(chat_with_model_events("fake", "key", [{"role": "user", "content": "read"}], completion_fn=fake_completion))
    kinds = [event.kind for event in events]
    assert kinds.index("tool_requested") < kinds.index("tool_finished") < kinds.index("turn_finished")
    assert [e.payload["id"] for e in events if e.kind == "tool_finished"] == ["call-1", "call-2"]
```

- [ ] **Step 2: Run the test and verify it fails on the missing event API**

Run: `pytest tests/test_llm_events.py::test_event_stream_orders_parallel_read_tools -q`

Expected: import failure for `chat_with_model_events` or an event-order assertion failure.

- [ ] **Step 3: Add an event generator while preserving the legacy text adapter**

Implement `chat_with_model_events(...)` as the canonical generator. Refactor the current generator so tool execution returns `ToolResult` and emits events instead of calling `safe_print`, while retaining `chat_with_model(..., stream=True)` as a compatibility wrapper that yields assistant text only. Keep read-only execution parallel but collect futures and emit results in original tool-call order.

- [ ] **Step 4: Route events to the main TUI thread**

Add a thread-safe UI event queue in `main.py`. The worker enqueues events and schedules a main-loop drain with `app_instance.loop.call_soon_threadsafe`; the drain passes events to `TurnRenderer` and calls the existing `print_formatted_text` only on the UI thread. Existing startup and status-bar construction remain untouched.

- [ ] **Step 5: Remove direct tool-loop output and verify integration tests**

Search for direct output in the model/tool path with `rg -n "print\\(|print_formatted_text|Console\\(" core/llm.py core/tools.py`. The remaining calls must be permission/UI adapters explicitly scheduled on the main loop, not arbitrary tool execution. Run: `pytest tests/test_llm_events.py tests/test_turn_renderer.py -q`.

- [ ] **Step 6: Commit the event-stream integration**

```text
git add core/llm.py core/tools.py main.py tests/test_llm_events.py
git commit -m "feat: route agent turns through ordered UI events"
```

### Task 5: Centralize ToolSpec, permissions, and Diff results

**Files:**
- Modify: `core/tool_protocol.py`
- Create: `core/tool_registry.py`
- Modify: `core/llm.py` tool schema construction and execution dispatch
- Modify: `core/tools.py` adapters
- Create: `tests/test_tool_registry.py`

- [ ] **Step 1: Write failing registry and permission metadata tests**

Assert that read tools are marked read-only, mutating tools require permission, schemas contain the existing function names, and unknown tools produce an error result instead of an exception escaping the turn.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `pytest tests/test_tool_registry.py -q`

Expected: import or assertion failures because registry metadata is not centralized.

- [ ] **Step 3: Implement the registry as adapters over existing functions**

Register `read_file`, `write_file`, `edit_file`, `list_directory`, `search_code`, `search_news`, `execute_command`, `manage_task`, `run_subagent`, and `glob_files`. Generate the existing API tool schema from registry metadata. Keep undo and Diff generation in the adapter result, not in the renderer.

- [ ] **Step 4: Move permission decisions to ToolSpec capabilities**

Preserve `confirm`, `plan`, and `full`. Make `check_permission_and_ask` accept a ToolSpec and return a structured decision; retain current-session allowlists and existing confirmation UI behavior while routing its display events through the UI queue.

- [ ] **Step 5: Run all tool tests and commit**

Run: `pytest tests/test_tool_registry.py tests/test_tools.py -q`.

```text
git add core/tool_protocol.py core/tool_registry.py core/llm.py core/tools.py tests/test_tool_registry.py
git commit -m "feat: centralize tool capabilities and permissions"
```

### Task 6: Layer the system prompt and runtime context

**Files:**
- Modify: `system_prompt.md`
- Create: `core/context_builder.py`
- Create: `tests/test_context_builder.py`
- Modify: `main.py` runtime prompt assembly
- Modify: `core/llm.py` memory/system-message injection

- [ ] **Step 1: Write failing prompt contract tests**

Assert that the stable prompt does not require private chain-of-thought tags or infinite retries, and that runtime context includes model, permission mode, working directory, active plan, skills, and memory without duplicating system messages.

- [ ] **Step 2: Run the tests and verify the expected failures**

Run: `pytest tests/test_context_builder.py -q`

Expected: failures because no ContextBuilder contract exists and the old prompt still contains the removed directives.

- [ ] **Step 3: Replace the stable prompt with the approved layered rules**

Keep the Star-Yao identity and terminal style, replace forced `<think>` output, unlimited retry, unconditional command use, and Markdown-only plan instructions with bounded workflow, verification, permission, and failure rules.

- [ ] **Step 4: Implement `ContextBuilder`**

Define `RuntimeContext` and `build_system_prompt(base_prompt, runtime, memory, skills, plan)`; strip old system messages from history, inject exactly one assembled system message, and keep dynamic time/model/path data outside the static prompt file.

- [ ] **Step 5: Run prompt tests and verify syntax**

Run: `pytest tests/test_context_builder.py -q` and `venv\Scripts\python -m py_compile main.py core/llm.py core/context_builder.py core/tool_registry.py core/turn_events.py core/turn_renderer.py`.

- [ ] **Step 6: Commit the prompt/context layer**

```text
git add system_prompt.md core/context_builder.py main.py core/llm.py tests/test_context_builder.py
git commit -m "feat: separate stable prompt and runtime context"
```

### Task 7: Add structured plans and reliable cancellation

**Files:**
- Create: `core/plan.py`
- Create: `tests/test_plan.py`
- Modify: `core/tool_registry.py` to register plan and user-question tools
- Modify: `core/llm.py` to pass a turn cancellation event and emit completion/cancel events
- Modify: `main.py` Ctrl+C and turn cleanup
- Create: `core/turn_controller.py`
- Create: `tests/test_cancellation.py`

- [ ] **Step 1: Write failing plan and cancellation tests**

Test plan transitions and rollback, and verify cancellation removes only the current turn additions while preserving earlier messages:

```python
def test_plan_update_rejects_unknown_status():
    from core.plan import PlanManager
    plan = PlanManager()
    plan.replace([{"id": "1", "subject": "test", "status": "pending"}])
    assert plan.update("1", status="invalid") is False

def test_cancel_turn_restores_start_length():
    from core.turn_controller import TurnController
    controller = TurnController([{"role": "user", "content": "old"}])
    controller.begin_turn()
    controller.append({"role": "assistant", "content": "partial"})
    controller.cancel()
    assert controller.messages == [{"role": "user", "content": "old"}]
```

- [ ] **Step 2: Run the tests and verify they fail for missing contracts**

Run: `pytest tests/test_plan.py tests/test_cancellation.py -q`.

Expected: import failures for `core.plan` and `core.turn_controller`.

- [ ] **Step 3: Implement `PlanManager` and plan event rendering**

Support `replace`, `update`, `snapshot`, `in_progress`, and `clear`; accept only the five defined statuses; emit `plan_updated` events through the existing event factory. Add plan tools through the registry without changing startup layout.

- [ ] **Step 4: Implement `TurnController` cancellation boundaries**

Record the message length at turn start, use a `threading.Event` for cancellation, close the active response stream when possible, stop further tool execution, remove current-turn messages, and emit exactly one `turn_cancelled` event.

- [ ] **Step 5: Add AskUser using the existing confirmation channel**

Generalize the current confirmation Future to a typed question payload. The worker waits for a response while the main UI owns the prompt; cancellation resolves the Future as declined and never leaves `confirm_prompt` populated.

- [ ] **Step 6: Run plan, cancellation, event, renderer, and tool tests**

Run: `pytest tests/test_plan.py tests/test_cancellation.py tests/test_llm_events.py tests/test_turn_renderer.py tests/test_tool_registry.py -q`.

- [ ] **Step 7: Commit lifecycle behavior**

```text
git add core/plan.py core/turn_controller.py core/tool_registry.py core/llm.py main.py tests/test_plan.py tests/test_cancellation.py
git commit -m "feat: add structured plans and cancellable turns"
```

### Task 8: Full verification and regression audit

**Files:**
- Modify only files required by failing tests found in this task
- Test: `tests/` and `core/`

- [ ] **Step 1: Run the complete test suite**

Run: `pytest -q`.

Expected: all tests pass with no collection errors.

- [ ] **Step 2: Run the project compile check**

Run: `venv\Scripts\python -m py_compile main.py core/llm.py core/memory.py core/memory_compressor.py core/themes.py core/context_builder.py core/turn_events.py core/turn_renderer.py core/tool_protocol.py core/tool_registry.py core/plan.py core/turn_controller.py`.

- [ ] **Step 3: Audit immutable UI surfaces**

Compare the startup and input construction in `main.py` against the pre-implementation committed source. Verify `print_logo`, theme selection, model selector, session selector, input key bindings, and status-bar layout were not changed except for event consumption hooks.

- [ ] **Step 4: Audit thread-safe output**

Run: `rg -n "print_formatted_text|Console\\(|Application\\.invalidate|\\bprint\\(" core main.py`. For every remaining background path, verify it schedules a main-loop callback or returns data to the event renderer.

- [ ] **Step 5: Perform a fake-provider end-to-end turn test**

Use a deterministic fake completion stream to exercise assistant text, split thinking tags, parallel reads, permission-denied write, successful edit with Diff, and cancellation. Assert event order, rendered output, message rollback, and no duplicate output.

- [ ] **Step 6: Commit only verified final fixes and report residual risks**

Run `git diff --check`, `git status --short`, and record any pre-existing dirty files separately from task changes. Do not claim completion until the acceptance criteria in the design specification have direct test or audit evidence.
