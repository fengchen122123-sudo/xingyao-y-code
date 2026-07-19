# Kimi-Style Subagent Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make foreground subagents concurrent, bounded, cancellable, permission-safe, and able to return same-turn results, while streaming reasoning in stable sentence-sized terminal blocks.

**Architecture:** `SubagentManager` owns immutable task snapshots and synchronized task transitions; the LLM loop supplies parent session/turn context and executes adjacent `parallel_safe` tools in a bounded pool. A single `PermissionBroker` serializes TUI confirmations, while `ThinkingStreamBuffer` separates model token cadence from terminal write cadence. Child agents receive a filtered tool schema and never consume root-agent background notifications.

**Tech Stack:** Python 3.12, dataclasses, `threading`, `concurrent.futures`, prompt_toolkit, LiteLLM, pytest.

---

## File Map

- Create `core/permission_broker.py`: synchronized permission request queue, single active prompt, owner/turn cancellation, and UI-safe activation.
- Create `core/thinking_stream.py`: deterministic sentence/newline/120-character reasoning buffer.
- Modify `core/tool_protocol.py`: add `parallel_safe` capability metadata.
- Modify `core/tool_registry.py`: mark read-only tools and `run_subagent` parallel-safe; expose the foreground/background/timeout schema.
- Modify `core/tools.py`: replace the notification queue with owned subagent task records and foreground/background execution semantics.
- Modify `core/llm.py`: pass execution ownership, filter child schemas, scope background notifications, serialize permissions, and execute adjacent parallel-safe tools with a four-worker bound.
- Modify `core/tool_presenter.py`: distinguish foreground completion, background start, timeout, cancellation, and failure.
- Modify `core/turn_renderer.py`: emit complete gray reasoning blocks before `thinking_finished`.
- Modify `main.py`: connect prompt resolution and Ctrl+C to the broker/manager, pass the root session/turn, and change only the startup version text.
- Modify `system_prompt.md`: document foreground/background and multi-subagent scheduling rules.
- Modify `C:/Users/***/.ycode/system_prompt.md`: apply the same scheduling contract to the active user prompt.
- Modify `setup.py`: bump the package version to `1.0.11`.
- Create `tests/test_permission_broker.py`: permission ordering, resolution, and cancellation tests.
- Create `tests/test_thinking_stream.py`: sentence, newline, long text, and finish-flush tests.
- Modify `tests/test_tool_protocol.py`: `parallel_safe` metadata coverage.
- Modify `tests/test_tool_registry.py`: registry capability and subagent schema coverage.
- Modify `tests/test_tools.py`: foreground/background lifecycle, timeout, cancellation, empty result, exception, and notification ownership coverage.
- Modify `tests/test_llm_events.py`: parallel subagent ordering/overlap, same-turn result flow, child isolation, and notification consumption coverage.
- Modify `tests/test_main_ui_events.py`: broker-backed prompt resolution, turn cancellation, and early reasoning rendering coverage.
- Modify `tests/test_turn_renderer.py`: stable incremental reasoning block coverage.
- Modify `tests/test_context_builder.py`: scheduling prompt contract coverage.

### Task 1: Stream Reasoning in Stable Blocks

**Files:**
- Create: `core/thinking_stream.py`
- Create: `tests/test_thinking_stream.py`
- Modify: `core/turn_renderer.py`
- Modify: `tests/test_turn_renderer.py`
- Modify: `tests/test_main_ui_events.py`

- [ ] **Step 1: Write failing buffer tests**

```python
from core.thinking_stream import ThinkingStreamBuffer


def test_complete_sentence_flushes_before_finish():
    buffer = ThinkingStreamBuffer(max_visible_chars=120)
    assert buffer.feed("先分析。后续") == ["先分析。"]
    assert buffer.finish() == ["后续"]


def test_newline_flushes_a_complete_block():
    buffer = ThinkingStreamBuffer()
    assert buffer.feed("第一行\n第二行") == ["第一行"]
    assert buffer.finish() == ["第二行"]


def test_long_unpunctuated_text_flushes_at_visible_limit():
    buffer = ThinkingStreamBuffer(max_visible_chars=12)
    assert buffer.feed("abcdefghijklmnop") == ["abcdefghijkl"]
    assert buffer.finish() == ["mnop"]
```

- [ ] **Step 2: Verify the tests fail for the missing module**

Run: `venv\Scripts\python -m pytest -q tests\test_thinking_stream.py`

Expected: collection fails with `ModuleNotFoundError: No module named 'core.thinking_stream'`.

- [ ] **Step 3: Implement the deterministic buffer**

```python
class ThinkingStreamBuffer:
    def __init__(self, max_visible_chars: int = 120) -> None:
        if max_visible_chars < 1:
            raise ValueError("max_visible_chars must be positive")
        self._pending = ""
        self._max_visible_chars = max_visible_chars

    def feed(self, text: str) -> list[str]:
        self._pending += text
        blocks: list[str] = []
        while self._pending:
            boundary = _last_sentence_boundary(self._pending)
            if boundary:
                blocks.append(self._take(boundary).rstrip("\r\n"))
                continue
            if len(self._pending) < self._max_visible_chars:
                break
            split_at = _stable_split(self._pending, self._max_visible_chars)
            blocks.append(self._take(split_at).rstrip())
        return [block for block in blocks if block]

    def finish(self) -> list[str]:
        remaining = self._pending.strip("\r\n")
        self._pending = ""
        return [remaining] if remaining else []
```

Implement `_last_sentence_boundary()` for `。！？.!?\n` and `_stable_split()` so an available whitespace boundary at or before the limit is preferred; otherwise split exactly at the limit.

- [ ] **Step 4: Verify the buffer tests pass**

Run: `venv\Scripts\python -m pytest -q tests\test_thinking_stream.py`

Expected: `3 passed`.

- [ ] **Step 5: Write failing renderer and UI tests for early complete-sentence output**

```python
def test_renderer_streams_complete_reasoning_sentences_before_finish():
    renderer = TurnRenderer()
    first = renderer.consume("thinking_delta", {"text": "正在分析。还有"})
    assert "正在分析。" in "".join(first)
    assert "还有" not in "".join(first)
    assert "还有" in "".join(renderer.consume("thinking_finished"))


def test_ui_prints_complete_reasoning_sentence_before_thinking_finished(monkeypatch):
    factory = TurnEventFactory("turn-thinking-stream")
    fragments = []
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", fragments.append)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-thinking-stream", None)
    main._render_ui_event(factory.emit("thinking_delta", {"text": "第一步完成。第二步"}))
    assert "第一步完成。" in Text.from_ansi("".join(fragments)).plain
```

- [ ] **Step 6: Verify the new renderer behavior fails**

Run: `venv\Scripts\python -m pytest -q tests\test_turn_renderer.py tests\test_main_ui_events.py`

Expected: the new tests fail because `TurnRenderer` still buffers all reasoning until `thinking_finished`.

- [ ] **Step 7: Integrate `ThinkingStreamBuffer` into `TurnRenderer`**

Replace `_thinking_parts` with one `ThinkingStreamBuffer`. For each returned block, append exactly `f"\x1b[90m{block}\x1b[0m\n"`; on `thinking_finished`, call `finish()` and append remaining blocks with the same format. Do not change answer buffering.

- [ ] **Step 8: Run focused rendering tests**

Run: `venv\Scripts\python -m pytest -q tests\test_thinking_stream.py tests\test_turn_renderer.py tests\test_main_ui_events.py`

Expected: all selected tests pass.

- [ ] **Step 9: Commit the reasoning stream unit**

```bash
git add core/thinking_stream.py core/turn_renderer.py tests/test_thinking_stream.py tests/test_turn_renderer.py tests/test_main_ui_events.py
git commit -m "feat: stream reasoning in stable blocks"
```

### Task 2: Declare Parallel-Safe Tool Capabilities

**Files:**
- Modify: `core/tool_protocol.py`
- Modify: `core/tool_registry.py`
- Modify: `tests/test_tool_protocol.py`
- Modify: `tests/test_tool_registry.py`

- [ ] **Step 1: Write failing capability and schema tests**

```python
def test_tool_spec_defaults_to_serial_execution():
    spec = ToolSpec(name="x", description="x", input_schema={}, execute=lambda: None)
    assert spec.parallel_safe is False


def test_registry_marks_reads_and_subagents_parallel_safe():
    registry = build_default_registry()
    assert registry.get("read_file").parallel_safe is True
    assert registry.get("run_subagent").parallel_safe is True
    assert registry.get("run_subagent").requires_permission is False
    assert registry.get("write_file").parallel_safe is False


def test_subagent_schema_defaults_to_foreground_with_bounded_timeout():
    schema = build_default_registry().get("run_subagent").api_schema()
    properties = schema["function"]["parameters"]["properties"]
    assert properties["run_in_background"]["default"] is False
    assert properties["timeout_seconds"]["default"] == 120
    assert properties["timeout_seconds"]["minimum"] == 30
    assert properties["timeout_seconds"]["maximum"] == 3600
```

- [ ] **Step 2: Verify the metadata tests fail**

Run: `venv\Scripts\python -m pytest -q tests\test_tool_protocol.py tests\test_tool_registry.py`

Expected: failures report missing `parallel_safe` and missing subagent schema fields.

- [ ] **Step 3: Add the metadata and schema**

Add `parallel_safe: bool = False` to `ToolSpec`. Set it to `True` on `read_file`, `list_directory`, `search_code`, `search_news`, `glob_files`, and `run_subagent`. Set `run_subagent.requires_permission=False`; permissions still apply independently to commands and file mutations performed inside a child. Change the subagent schema to:

```python
_schema({
    "task_description": {"type": "string"},
    "run_in_background": {"type": "boolean", "default": False},
    "timeout_seconds": {
        "type": "integer", "default": 120, "minimum": 30, "maximum": 3600,
    },
}, ["task_description"])
```

- [ ] **Step 4: Verify capability tests pass**

Run: `venv\Scripts\python -m pytest -q tests\test_tool_protocol.py tests\test_tool_registry.py`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the capability metadata**

```bash
git add core/tool_protocol.py core/tool_registry.py tests/test_tool_protocol.py tests/test_tool_registry.py
git commit -m "feat: declare parallel-safe tools"
```

### Task 3: Replace Global Notifications with Owned Subagent Tasks

**Files:**
- Modify: `core/tools.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Write failing foreground/background lifecycle tests**

Use an injected `runner(record)` callable so lifecycle tests execute real manager code without network mocks:

```python
def test_foreground_subagent_waits_and_returns_public_result():
    manager = SubagentManager(runner=lambda record: "天气：晴")
    result = manager.run("查询天气", "s1", "t1", timeout_seconds=30)
    assert result == "天气：晴"
    assert manager.snapshot()[0].status == "completed"


def test_background_subagent_returns_task_id_immediately():
    release = threading.Event()
    manager = SubagentManager(runner=lambda record: release.wait(1) or "完成")
    result = manager.run("独立任务", "s1", "t1", run_in_background=True)
    assert "subagent-" in result
    assert manager.snapshot()[0].status in {"pending", "running"}
    release.set()


def test_background_notifications_are_scoped_to_parent_session():
    manager = SubagentManager(runner=lambda record: "结果")
    manager.run("任务", "s1", "t1", run_in_background=True)
    assert manager.wait_for_idle(1)
    assert manager.drain_notifications("other") == []
    assert len(manager.drain_notifications("s1")) == 1
    assert manager.drain_notifications("s1") == []


def test_completed_task_records_are_bounded():
    manager = SubagentManager(runner=lambda record: "完成", max_records=2)
    for index in range(3):
        manager.run(f"任务{index}", "s1", f"t{index}")
    assert len(manager.snapshot()) == 2
```

- [ ] **Step 2: Verify lifecycle tests fail against the current manager**

Run: `venv\Scripts\python -m pytest -q tests\test_tools.py -k "subagent"`

Expected: tests fail because the current manager has only `spawn()`, always returns immediately, and owns one global notification queue.

- [ ] **Step 3: Add synchronized task records and terminal transitions**

```python
@dataclass
class SubagentTask:
    task_id: str
    parent_session: str
    parent_turn_id: str
    description: str
    run_in_background: bool
    status: str = "pending"
    started_at: float | None = None
    finished_at: float | None = None
    result: str = ""
    error: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    done_event: threading.Event = field(default_factory=threading.Event, repr=False)
    notification_consumed: bool = False
```

`SubagentManager.run()` validates `30 <= timeout_seconds <= 3600`, creates a record under a lock, starts one daemon worker, and either returns the background ID immediately or waits for the record's terminal state. `snapshot()` returns copied records; callers never mutate the manager dictionary. Keep at most `max_records` records by pruning the oldest terminal, already-notified tasks; never prune a running task or an unconsumed background result.

- [ ] **Step 4: Write failing timeout, parent cancellation, empty-result, and exception tests**

```python
class FakeMonotonic:
    def __init__(self, values):
        self._values = iter(values)

    def __call__(self):
        return next(self._values)


def test_foreground_timeout_sets_terminal_state():
    clock = FakeMonotonic([0.0, 31.0])
    manager = SubagentManager(
        runner=lambda record: record.cancel_event.wait(1),
        monotonic=clock,
    )
    result = manager.run("慢任务", "s1", "t1", timeout_seconds=30)
    assert "timed_out" in result
    assert manager.snapshot()[0].status == "timed_out"


def test_parent_cancel_marks_foreground_task_cancelled():
    parent_cancel = threading.Event()
    manager = SubagentManager(runner=lambda record: record.cancel_event.wait(1))
    timer = threading.Timer(0.02, parent_cancel.set)
    timer.start()
    result = manager.run("可取消任务", "s1", "t1", parent_cancel_event=parent_cancel)
    assert "cancelled" in result


@pytest.mark.parametrize("runner", [lambda record: "", lambda record: (_ for _ in ()).throw(RuntimeError("boom"))])
def test_empty_or_exceptional_subagent_is_failed(runner):
    manager = SubagentManager(runner=runner)
    result = manager.run("失败任务", "s1", "t1")
    assert result.startswith("🚨")
    assert manager.snapshot()[0].status == "failed"
```

- [ ] **Step 5: Implement explicit timeout/cancel/failure transitions**

The wait loop checks `done_event`, `parent_cancel_event`, and `time.monotonic()` without retrying the child. A timeout or cancellation sets the child `cancel_event`, records one terminal state, and prevents a late worker completion from overwriting it. A completed worker with an empty stripped public result transitions to `failed`.

- [ ] **Step 6: Implement the real child runner with isolation flags**

The default runner calls:

```python
events = chat_with_model_events(
    model_name,
    api_key,
    messages,
    api_base,
    persist=False,
    turn_id=record.task_id,
    cancel_event=record.cancel_event,
    allow_subagents=False,
    consume_background_notifications=False,
    parent_session=record.parent_session,
    parent_turn_id=record.parent_turn_id,
    permission_owner=record.task_id,
    permission_source=f"子代理：{record.description[:40]}",
)
```

Collect only `assistant_delta` text as the public result. Do not print child reasoning or raw tool output.

- [ ] **Step 7: Run the complete tools test file**

Run: `venv\Scripts\python -m pytest -q tests\test_tools.py`

Expected: all tests pass.

- [ ] **Step 8: Commit the owned task lifecycle**

```bash
git add core/tools.py tests/test_tools.py
git commit -m "feat: add owned subagent task lifecycle"
```

### Task 4: Serialize Permission Requests

**Files:**
- Create: `core/permission_broker.py`
- Create: `tests/test_permission_broker.py`
- Modify: `core/llm.py`
- Modify: `main.py`
- Modify: `tests/test_main_ui_events.py`

- [ ] **Step 1: Write failing broker ordering and cancellation tests**

```python
def test_broker_activates_only_one_request_at_a_time():
    activated = []
    broker = PermissionBroker(activate_prompt=activated.append)
    first = broker.enqueue("execute_command", {"command": "one"}, "child-1", "turn-1", "子代理一")
    second = broker.enqueue("execute_command", {"command": "two"}, "child-2", "turn-1", "子代理二")
    assert [request.request_id for request in activated] == [first.request_id]
    broker.resolve(first.request_id, 0)
    assert [request.request_id for request in activated] == [first.request_id, second.request_id]
    assert first.future.result() == 0


def test_cancel_turn_rejects_active_and_queued_requests():
    broker = PermissionBroker(activate_prompt=lambda request: None)
    first = broker.enqueue("write_file", {}, "child-1", "turn-1", "一")
    second = broker.enqueue("edit_file", {}, "child-2", "turn-1", "二")
    assert broker.cancel_turn("turn-1") == 2
    assert first.future.result() == 1
    assert second.future.result() == 1
    assert broker.active_request() is None
```

- [ ] **Step 2: Verify broker tests fail for the missing module**

Run: `venv\Scripts\python -m pytest -q tests\test_permission_broker.py`

Expected: collection fails with `ModuleNotFoundError: No module named 'core.permission_broker'`.

- [ ] **Step 3: Implement the broker**

Create frozen request identity fields plus a `Future`, protect the deque and active request with one lock, set Future results outside the lock, and activate the next queued request only after resolution/cancellation. Reject stale `request_id` values instead of resolving a different request.

- [ ] **Step 4: Verify standalone broker behavior**

Run: `venv\Scripts\python -m pytest -q tests\test_permission_broker.py`

Expected: all broker tests pass.

- [ ] **Step 5: Write failing integration tests for broker-backed prompt resolution**

```python
def test_resolve_confirm_prompt_advances_broker_queue(monkeypatch):
    requests = []
    broker = PermissionBroker(activate_prompt=requests.append)
    monkeypatch.setattr(main, "permission_broker", broker)
    first = broker.enqueue("execute_command", {}, "one", "turn", "主代理")
    second = broker.enqueue("execute_command", {}, "two", "turn", "子代理")
    monkeypatch.setattr(main, "confirm_prompt", main._permission_prompt(first))
    assert main._resolve_confirm_prompt(0) is True
    assert first.future.result() == 0
    assert broker.active_request().request_id == second.request_id
```

- [ ] **Step 6: Route `check_permission_and_ask()` through one broker**

Add `permission_owner`, `parent_turn_id`, and `permission_source` parameters. For an active TUI, enqueue and wait on the returned request Future. The broker's UI activation callback is the only code that assigns `main.confirm_prompt`, and it schedules that assignment via `app.loop.call_soon_threadsafe`. Preserve the raw terminal path under a single lock.

In `main._resolve_confirm_prompt()`, validate the selected index and call `permission_broker.resolve(request_id, index)`. Keep legacy Future resolution only for existing non-broker question prompts such as `ask_user`. Include `permission_source` in `confirm_prompt` and render `请求来源：主代理` or the child task label above the operation so concurrent requests remain distinguishable.

- [ ] **Step 7: Wire Ctrl+C to reject all requests owned by the active turn**

Track `active_turn_id` next to `active_cancel_event`. On Ctrl+C, set the cancel event, call `permission_broker.cancel_turn(active_turn_id)`, and call `subagent_manager.cancel_foreground(current_session, active_turn_id)`. Do not assign or clear `confirm_prompt` from a worker thread.

- [ ] **Step 8: Run permission and UI tests**

Run: `venv\Scripts\python -m pytest -q tests\test_permission_broker.py tests\test_main_ui_events.py tests\test_cancellation.py`

Expected: all selected tests pass.

- [ ] **Step 9: Commit serialized permission handling**

```bash
git add core/permission_broker.py core/llm.py main.py tests/test_permission_broker.py tests/test_main_ui_events.py tests/test_cancellation.py
git commit -m "fix: serialize concurrent permission prompts"
```

### Task 5: Run Foreground Subagents Concurrently and Return Same-Turn Results

**Files:**
- Modify: `core/llm.py`
- Modify: `core/tools.py`
- Modify: `core/tool_presenter.py`
- Modify: `tests/test_llm_events.py`
- Modify: `tests/test_tool_presenter.py`

- [ ] **Step 1: Write a failing overlap/order test**

```python
def test_parallel_subagents_overlap_and_results_keep_call_order(mocker):
    barrier = threading.Barrier(2)
    completed = []

    def fake_run(description, **context):
        barrier.wait(timeout=1)
        completed.append(description)
        return f"结果:{description}"

    mocker.patch("core.llm.run_subagent", side_effect=fake_run)
    mocker.patch("core.llm.completion", side_effect=[
        iter([_chunk(tool_calls=[
            _tool_call(0, "weather", "run_subagent", '{"task_description":"天气"}'),
            _tool_call(1, "time", "run_subagent", '{"task_description":"时间"}'),
        ])]),
        iter([_chunk("汇总：天气和时间")]),
    ])
    messages = [{"role": "user", "content": "并行查询并汇总"}]
    events = list(chat_with_model_events("model", "key", messages, persist=False, turn_id="turn-1", parent_session="s1"))
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["weather", "time"]
    assert completed == ["天气", "时间"] or completed == ["时间", "天气"]
    assert "汇总：天气和时间" in "".join(e.payload.get("text", "") for e in events)
```

- [ ] **Step 2: Verify the overlap test fails**

Run: `venv\Scripts\python -m pytest -q tests\test_llm_events.py -k "parallel_subagents"`

Expected: the barrier times out because `run_subagent` calls are currently serialized and/or return background-start text.

- [ ] **Step 3: Add execution context to one tool call**

Extend `_execute_single_tool_call()` with `parent_session`, `parent_turn_id`, `cancel_event`, `allow_subagents`, `permission_owner`, and `permission_source`. For `run_subagent`, reject the call when `allow_subagents` is false; otherwise call:

```python
run_subagent(
    task_description=arguments["task_description"],
    run_in_background=arguments.get("run_in_background", False),
    timeout_seconds=arguments.get("timeout_seconds", 120),
    parent_session=parent_session,
    parent_turn_id=parent_turn_id,
    parent_cancel_event=cancel_event,
)
```

Pass the same ownership context into permission checks for all mutating tools.

- [ ] **Step 4: Filter child tools and scope notification consumption**

Add these keyword parameters to both `chat_with_model_events()` and `_chat_with_model_generator()`:

```python
allow_subagents: bool = True
consume_background_notifications: bool = True
parent_session: str | None = None
parent_turn_id: str | None = None
permission_owner: str | None = None
permission_source: str = "主代理"
```

Build request schemas per invocation. Remove `run_subagent` when `allow_subagents` is false. Only call `subagent_manager.drain_notifications(parent_session)` when `consume_background_notifications` is true and a parent session exists.

- [ ] **Step 5: Execute adjacent parallel-safe calls in a bounded pool**

Replace the read-only name tuple with `tool_registry.get(name).parallel_safe`. Group only adjacent parallel-safe calls, use `ThreadPoolExecutor(max_workers=min(4, len(batch)))`, pass the full execution context to each call, and extend `messages` with `executor.map()` results so tool messages retain model call order.

- [ ] **Step 6: Add failing child isolation tests**

```python
def test_child_schema_excludes_run_subagent(mocker):
    captured = {}
    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk("完成")])
    mocker.patch("core.llm.completion", side_effect=fake_completion)
    list(chat_with_model_events("m", "k", [], persist=False, allow_subagents=False,
                                consume_background_notifications=False))
    assert "run_subagent" not in {item["function"]["name"] for item in captured["tools"]}


def test_child_does_not_drain_root_background_notifications(mocker):
    drain = mocker.patch("core.llm.subagent_manager.drain_notifications")
    mocker.patch("core.llm.completion", return_value=iter([_chunk("完成")]))
    list(chat_with_model_events("m", "k", [], persist=False,
                                consume_background_notifications=False))
    drain.assert_not_called()
```

- [ ] **Step 7: Verify same-turn orchestration and isolation**

Run: `venv\Scripts\python -m pytest -q tests\test_llm_events.py tests\test_tools.py`

Expected: all selected tests pass.

- [ ] **Step 8: Update subagent activity copy and tests**

`summarize_tool_started()` remains `星瑶正在调度子代理：...`. `summarize_tool_finished()` returns `子代理已完成：...` for completed foreground results, `子代理已在后台启动：...` when the result reports a background task ID, and explicit `子代理超时/已取消/执行失败` text for non-success states.

- [ ] **Step 9: Run presenter tests**

Run: `venv\Scripts\python -m pytest -q tests\test_tool_presenter.py`

Expected: all presenter tests pass.

- [ ] **Step 10: Commit same-turn orchestration**

```bash
git add core/llm.py core/tools.py core/tool_presenter.py tests/test_llm_events.py tests/test_tools.py tests/test_tool_presenter.py
git commit -m "feat: orchestrate foreground subagents in parallel"
```

### Task 6: Apply Prompt Contract, Version, and Root-Turn Wiring

**Files:**
- Modify: `system_prompt.md`
- Modify: `C:/Users/***/.ycode/system_prompt.md`
- Modify: `tests/test_context_builder.py`
- Modify: `main.py`
- Modify: `setup.py`
- Modify: `tests/test_agent_turn_e2e.py`

- [ ] **Step 1: Write failing prompt contract tests**

```python
def test_default_prompt_defines_foreground_subagent_contract():
    prompt = Path("system_prompt.md").read_text(encoding="utf-8")
    assert "一次性发出多个 `run_subagent` 调用" in prompt
    assert "当前回答依赖子 Agent 结果时使用默认前台模式" in prompt
    assert "子 Agent 不能启动其他子 Agent" in prompt
```

- [ ] **Step 2: Verify the prompt test fails**

Run: `venv\Scripts\python -m pytest -q tests\test_context_builder.py`

Expected: the new prompt-contract test fails because the scheduling rules are absent.

- [ ] **Step 3: Add the scheduling contract to both prompt files**

Append this section without changing the existing terminal style instructions:

```text
【子 Agent 调度规则】
1. 互不依赖的多个子任务，应在一个响应中一次性发出多个 `run_subagent` 调用。
2. 当前回答依赖子 Agent 结果时使用默认前台模式，不得设置 run_in_background=true。
3. 只有任务可独立继续且当前决策不依赖其结果时，才使用后台模式。
4. 子 Agent 结果只返回主 Agent；需要用户看到时，由主 Agent 在同一轮汇总。
5. 子 Agent 不能启动其他子 Agent。
```

- [ ] **Step 4: Pass root ownership from `main.py`**

Call `chat_with_model_events()` with `parent_session=task_session`, `parent_turn_id=turn_id`, `permission_owner=turn_id`, and `permission_source="主代理"`. Store `active_turn_id = turn_id` while the turn is active and clear it with `active_cancel_event` in all success, cancellation, and failure paths.

- [ ] **Step 5: Bump only the displayed/package version**

Change `main.py` startup card from `v1.0.10` to `v1.0.11` and `setup.py` from `1.0.10` to `1.0.11`. Do not change logo, welcome card, layout, theme, input box, or status bar structure.

- [ ] **Step 6: Add an end-to-end fake-provider same-turn test**

Use a fake first response containing two `run_subagent` calls, a fake subagent runner returning distinct results, and a fake final response. Assert two tool results are present before the final assistant message, both tool IDs preserve call order, and the turn ends with one public combined answer.

- [ ] **Step 7: Run focused integration tests**

Run: `venv\Scripts\python -m pytest -q tests\test_context_builder.py tests\test_agent_turn_e2e.py tests\test_llm_events.py tests\test_main_ui_events.py`

Expected: all selected tests pass.

- [ ] **Step 8: Commit prompt, version, and root wiring**

```bash
git add system_prompt.md main.py setup.py tests/test_context_builder.py tests/test_agent_turn_e2e.py
git commit -m "feat: complete Kimi-style subagent workflow"
```

The active file `C:/Users/***/.ycode/system_prompt.md` is intentionally not part of the Git commit; verify its contents separately.

### Task 7: Full Verification and Windows PTY Acceptance

**Files:**
- Verify all task files above; no new production behavior in this task.

- [ ] **Step 1: Run the complete automated suite**

Run: `venv\Scripts\python -m pytest -q`

Expected: all tests pass with zero failures.

- [ ] **Step 2: Compile all changed Python modules**

Run: `venv\Scripts\python -m py_compile main.py core\llm.py core\tools.py core\permission_broker.py core\thinking_stream.py core\tool_protocol.py core\tool_registry.py core\tool_presenter.py core\turn_renderer.py`

Expected: exit code `0` and no output.

- [ ] **Step 3: Verify version and prompt text exactly**

Run: `rg -n "v1\.0\.11|version='1\.0\.11'|一次性发出多个.*run_subagent|子 Agent 不能启动其他子 Agent" main.py setup.py system_prompt.md "$HOME/.ycode/system_prompt.md"`

Expected: both version locations and both prompt files match; no startup layout lines changed except the version literal.

- [ ] **Step 4: Inspect the task-only diff**

Run: `git diff -- core/permission_broker.py core/thinking_stream.py core/tool_protocol.py core/tool_registry.py core/tools.py core/llm.py core/tool_presenter.py core/turn_renderer.py main.py setup.py system_prompt.md tests/test_permission_broker.py tests/test_thinking_stream.py tests/test_tool_protocol.py tests/test_tool_registry.py tests/test_tools.py tests/test_llm_events.py tests/test_main_ui_events.py tests/test_turn_renderer.py tests/test_context_builder.py tests/test_agent_turn_e2e.py`

Expected: no unrelated refactor, no startup layout redesign, no user session/database edits, and no changes to session `你好_1`.

- [ ] **Step 5: Run the real Windows PTY acceptance**

Run: `venv\Scripts\python main.py`

Enter exactly:

```text
你分出两个子 agent，一个查询天气，一个查询时间，你来汇总给我
```

Verify: two start lines appear quickly; the two child tasks overlap; permission prompts appear one at a time and do not disappear or deadlock; both results return in the same user turn; the root agent prints one weather/time summary; gray reasoning appears by complete sentences before reasoning ends; Ctrl+C cancels the owned tasks and queued permissions; the input/status UI recovers; and the startup layout is unchanged except `v1.0.11`.

- [ ] **Step 6: Record residual external constraints honestly**

If weather lookup needs a city, the model must ask or state an explicit task assumption. If the configured provider cannot emit native reasoning or is unavailable, report that PTY observation separately; do not fabricate a passing result or add a fallback.
