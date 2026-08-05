# Y-code CLI + GUI Tool Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one structured tool-event contract, then deliver CodeBuddy-style B+C progress in the CLI and a compact execution timeline inside each GUI assistant message, with ordinary tool failures remaining visible and non-terminal.

**Architecture:** `core/tool_presenter.py` owns presentation-safe tool metadata and `core/llm.py` owns durations, results, and Diff ownership. After that shared contract passes, the CLI track builds a pure progress aggregator and prompt_toolkit renderer while the GUI track independently forwards events into message-local Vue state and renders a tested timeline; a final integration task verifies both consumers against the same event sequence.

**Tech Stack:** Python 3.12, pytest, prompt_toolkit, Rich/ANSI, Vue 3.5, Pinia 4, TypeScript 6, Vite 8, Vitest, Vue Test Utils, jsdom.

This plan supersedes `docs/superpowers/plans/2026-08-04-codebuddy-tool-progress.md` for execution. Keep the earlier document as design history, but do not execute its overlapping tasks separately.

## Global Constraints

- Work only on `dev`; do not merge to `master`.
- Preserve every pre-existing user change discovered at execution start. At plan creation this includes `core/lsp.py`, `core/mcp_selector.py`, `core/task_manager.py`, `core/task_scheduler.py`, `core/tools.py`, `core/worktree.py`, and `gui_bridge.py`; re-run `git status --short` before each task because the worktree is actively changing.
- Never stage `.superpowers/` or unrelated user files.
- Use UTF-8 for Chinese source and tests.
- Do not add a runtime dependency; Vitest, Vue Test Utils, and jsdom are frontend development dependencies only.
- Do not expose model-private reasoning in CLI history or the GUI execution timeline.
- Do not add automatic retries, silent fallbacks, heuristic parsing of arbitrary tool output, or downgraded algorithms.
- Ordinary tool failures remain `tool` results and do not become `turn_error`; cancellation, API failure, permission-system failure, and state corruption still terminate the turn.
- Background workers only enqueue events. They must not call `print_formatted_text`, mutate prompt_toolkit layout state, or call `app.invalidate()` directly.
- TUI output is raw terminal text; never add Markdown emphasis markers to CLI status lines.
- Database message contents and tool execution order remain unchanged.
- New event fields are optional for backward compatibility; missing legacy durations are omitted, not displayed as `0.0s`.
- Display strings must remove ANSI/control characters, redact known secrets, and enforce explicit length limits before reaching either UI.

## Runtime Setup

The checked-in `venv` currently points to a missing Python installation. Before Task 1, create the ignored `.venv` from the bundled workspace Python and install the declared project dependencies:

```powershell
& 'C:\Users\USERNAME\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -V
```

The final command must report Python 3.12 before continuing. Do not delete or rewrite the existing `venv`; all plan commands use `.venv` explicitly.

## Execution Waves

1. Shared wave, sequential: Tasks 1–2.
2. Parallel wave after Task 2 passes:
   - CLI worker: Tasks 3–5 in order.
   - GUI worker: Tasks 6–9 in order.
3. Integration wave, after both workers pass review: Task 10.

The parallel workers must not edit `core/tool_presenter.py` or `core/llm.py`; contract corrections return to the shared wave owner.

## File Structure

- Modify `core/tool_presenter.py`: safe structured tool display contract.
- Modify `core/llm.py`: start/finish metadata, duration measurement, Diff `tool_call_id`.
- Create `core/tool_progress.py`: CLI-only active state, deterministic aggregation, ANSI formatting.
- Modify `core/turn_renderer.py`: consume CLI tool progress and flush boundaries.
- Modify `main.py`: live CLI activity and thinking-divider cleanup.
- Modify `gui_bridge.py`: de-duplicate start events and forward the shared contract.
- Create `frontend/src/utils/toolTrace.ts`: frontend domain types, legacy normalization, aggregation, Diff counting.
- Create `frontend/src/components/ToolExecutionRow.vue`: one interactive timeline row.
- Create `frontend/src/components/ToolExecutionTrace.vue`: trace header, status, rows, timer, whole-block collapse.
- Modify `frontend/src/components/MessageBubble.vue`: replace old tool cards and standalone Diff cards.
- Modify `frontend/src/stores/appStore.ts`: bind tool results/Diffs/subagents to the current assistant message.
- Modify `frontend/src/types/pywebview.d.ts`: typed bridge-event payloads.
- Modify `frontend/package.json`, `frontend/package-lock.json`, and `frontend/vite.config.ts`: frontend tests.
- Add focused Python and frontend tests named in each task.
- Modify `README.md`: document the shared CLI/GUI behavior and remove stale `/verbose` documentation.

---

### Task 1: Shared Presentation-Safe Tool Metadata

**Files:**
- Modify: `core/tool_presenter.py`
- Modify: `tests/test_tool_presenter.py`

**Interfaces:**
- Produces: `sanitize_tool_display_text(value: object, limit: int = 160) -> str`
- Produces: `build_tool_display(name: str, arguments: dict, result: dict | None = None, ok: bool | None = None) -> dict[str, object]`
- Produces keys: `category`, `action`, `target`, `command`, `metrics`, `error_summary`, `is_skill`
- Consumed by: Task 2 event payloads, Task 3 CLI normalization, Task 6 GUI bridge.

- [ ] **Step 1: Add failing metadata and security tests**

Add to `tests/test_tool_presenter.py`:

```python
from core.tool_presenter import build_tool_display, sanitize_tool_display_text


def test_build_tool_display_returns_read_metadata():
    display = build_tool_display(
        "read_file",
        {"file_path": "core/llm.py"},
        {"content": "one\ntwo\nthree"},
        ok=True,
    )
    assert display == {
        "category": "read",
        "action": "读取文件",
        "target": "core/llm.py",
        "command": "",
        "metrics": {"line_count": 3},
        "error_summary": "",
        "is_skill": False,
    }


def test_build_tool_display_marks_skill_read_separately():
    display = build_tool_display(
        "read_file",
        {"file_path": r"C:\Users\user\.agents\skills\writing-plans\SKILL.md"},
        {"content": "skill"},
        ok=True,
    )
    assert display["category"] == "skill"
    assert display["target"] == "writing-plans"
    assert display["is_skill"] is True


def test_display_text_removes_controls_truncates_and_redacts():
    value = (
        "\x1b[31mAuthorization: Bearer abc.def.secret\x1b[0m\n"
        'api_key="sk-1234567890abcdef"'
    )
    cleaned = sanitize_tool_display_text(value, limit=80)
    assert "abc.def.secret" not in cleaned
    assert "sk-1234567890abcdef" not in cleaned
    assert "Authorization: ***" in cleaned
    assert "api_key=***" in cleaned
    assert "\x1b" not in cleaned


def test_failed_command_keeps_safe_command_and_error_summary():
    display = build_tool_display(
        "execute_command",
        {"command": "pytest -q"},
        {"content": "🚨 命令执行失败（退出码 1）。\nModuleNotFoundError: pytest"},
        ok=False,
    )
    assert display["category"] == "command"
    assert display["command"] == "pytest -q"
    assert display["error_summary"] == (
        "命令执行失败（退出码 1）。 ModuleNotFoundError: pytest"
    )
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_tool_presenter.py
```

Expected: collection fails because the two new functions do not exist.

- [ ] **Step 3: Implement the shared contract**

Add explicit mappings and sanitization to `core/tool_presenter.py`:

```python
import re
from typing import Any


_TOOL_CATEGORIES = {
    "read_file": "read",
    "search_code": "search",
    "glob_files": "search",
    "execute_command": "command",
    "write_file": "mutation",
    "edit_file": "mutation",
}

_TOOL_ACTIONS = {
    "read_file": "读取文件",
    "search_code": "搜索代码",
    "glob_files": "查找文件",
    "execute_command": "运行命令",
    "write_file": "写入文件",
    "edit_file": "修改文件",
    "list_directory": "查看目录",
    "search_news": "搜索资料",
    "fetch_url": "访问网址",
    "get_weather": "查询天气",
    "run_subagent": "调度子代理",
    "manage_task": "管理后台任务",
    "ask_user": "等待用户回答",
    "plan_write": "更新任务计划",
    "plan_update": "更新计划项",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NAMED_SECRET_RE = re.compile(
    r'(?i)(["\']?(?:api[_-]?key|authorization|token|password)["\']?\s*[:=]\s*)'
    r'(Bearer\s+[A-Za-z0-9._~+/=-]+|"[^"]*"|\'[^\']*\'|[^\s,;]+)'
)
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def sanitize_tool_display_text(value: object, limit: int = 160) -> str:
    text = _ANSI_RE.sub("", str(value or ""))
    text = _CONTROL_RE.sub("", text.replace("\r", " ").replace("\n", " "))
    text = _NAMED_SECRET_RE.sub(lambda match: f"{match.group(1)}***", text)
    text = _BEARER_RE.sub(lambda match: f"{match.group(1)}***", text)
    text = _OPENAI_KEY_RE.sub("***", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."
```

Implement `build_tool_display()` with explicit categories. Use `tool_target()` for targets, `_skill_name_from_path()` for skills, `sanitize_tool_display_text(..., 240)` for commands, and `sanitize_tool_display_text(..., 160)` for targets/errors. Count read lines only from a successful structured `result["content"]`; do not derive any other metrics from free-form output.

Keep `summarize_tool_started()` and `summarize_tool_finished()` backward compatible.

- [ ] **Step 4: Run presenter tests and verify GREEN**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Commit the shared metadata**

```powershell
git add core\tool_presenter.py tests\test_tool_presenter.py
git commit -m "feat: add safe structured tool display metadata"
```

---

### Task 2: Shared Duration, Completion, and Diff Ownership Events

**Files:**
- Modify: `core/llm.py`
- Modify: `tests/test_llm_events.py`
- Modify: `tests/test_llm_events_fixes.py`

**Interfaces:**
- Consumes: Task 1 `build_tool_display()`.
- Produces: `_tool_event_payload(tc: dict) -> dict` with `display`.
- Produces: `_tool_finished_event_payload(tc: dict, result: dict, duration_ms: int) -> dict`.
- Produces: `_show_file_diff(..., tool_call_id: str = "")` and `diff_ready.payload["tool_call_id"]`.
- Preserves: existing `id`, `name`, `preview`, `arguments`, `summary`, `ok`, and `result` fields.

- [ ] **Step 1: Add failing event-contract tests**

Add to `tests/test_llm_events.py`:

```python
from core.llm import _tool_event_payload, _tool_finished_event_payload


def test_tool_start_payload_has_structured_display():
    payload = _tool_event_payload({
        "id": "cmd-start",
        "function": {
            "name": "execute_command",
            "arguments": '{"command":"pytest -q"}',
        },
    })
    assert payload["display"]["category"] == "command"
    assert payload["display"]["command"] == "pytest -q"


def test_tool_finish_payload_has_duration_result_and_display():
    tc = {
        "id": "read-1",
        "function": {
            "name": "read_file",
            "arguments": '{"file_path":"core/llm.py"}',
        },
    }
    result = {
        "role": "tool",
        "tool_call_id": "read-1",
        "name": "read_file",
        "content": "one\ntwo",
    }
    payload = _tool_finished_event_payload(tc, result, duration_ms=125)
    assert payload["duration_ms"] == 125
    assert payload["display"]["metrics"] == {"line_count": 2}
    assert payload["ok"] is True
    assert payload["result"] == "one\ntwo"
```

Add a focused test to `tests/test_llm_events_fixes.py` that calls `_show_file_diff()` with an event collector and asserts:

```python
from core.llm import _show_file_diff


def test_diff_ready_contains_exact_tool_call_id():
    events = []
    _show_file_diff(
        "sample.py",
        "old\n",
        "new\n",
        lambda kind, payload: events.append((kind, payload)),
        panel_title="test diff",
        truncated_note="test",
        tool_call_id="edit-7",
    )

    assert events[0][0] == "diff_ready"
    assert events[0][1]["tool_call_id"] == "edit-7"
    assert events[0][1]["file_path"] == "sample.py"
```

- [ ] **Step 2: Run the contract tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_llm_events.py tests\test_llm_events_fixes.py -k "tool_start_payload or tool_finish_payload or diff_ready"
```

Expected: missing `display`, missing helper, or missing `tool_call_id` failures.

- [ ] **Step 3: Centralize event construction**

In `core/llm.py`, import `build_tool_display`. Add `display` to `_tool_event_payload()` and add:

```python
def _tool_finished_event_payload(tc, result, duration_ms):
    function_name = tc["function"]["name"]
    arguments = _tool_call_arguments(tc)
    ok = _tool_result_ok(function_name, result)
    return {
        **_tool_event_payload(tc),
        "ok": ok,
        "summary": _tool_output_preview(function_name, arguments, result),
        "result": _tool_result_text(result),
        "duration_ms": max(0, int(duration_ms)),
        "display": build_tool_display(function_name, arguments, result, ok=ok),
    }
```

Next to `execute_in_context()`, add:

```python
def execute_timed(tool_call):
    started_at = time.monotonic()
    result = execute_in_context(tool_call)
    duration_ms = round((time.monotonic() - started_at) * 1000)
    return result, max(0, duration_ms)
```

Parallel batches must preserve model call order when unzipping `(result, duration_ms)` pairs. Serial batches use the same helper. Do not catch `KeyboardInterrupt` or convert internal exceptions here.

- [ ] **Step 4: Attach exact Diff ownership**

Extend `_show_file_diff()` with `tool_call_id=""`, add it to the emitted payload, and pass `tc["id"]` from both `write_file` and `edit_file` call sites:

```python
_notify_event(event_callback, "diff_ready", {
    "tool_call_id": str(tool_call_id or ""),
    "file_path": file_path,
    "text": diff_output,
    "diff_text": display_text,
})
```

Do not associate Diffs by filename or most-recent mutation.

- [ ] **Step 5: Run shared event tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_tool_presenter.py tests\test_llm_events.py tests\test_llm_events_fixes.py
```

Expected: PASS.

- [ ] **Step 6: Commit the shared event contract**

```powershell
git add core\llm.py tests\test_llm_events.py tests\test_llm_events_fixes.py
git commit -m "feat: add structured timed tool events"
```

After this commit, start the CLI and GUI tracks concurrently.

---

### Task 3: CLI Tool Progress State Machine

**Files:**
- Create: `core/tool_progress.py`
- Create: `tests/test_tool_progress.py`

**Interfaces:**
- Consumes: Task 2 event payloads.
- Produces: immutable `ToolDisplayEvent` and `ActiveToolStatus`.
- Produces: `start(payload: dict) -> None`, `finish(payload: dict) -> list[str]`, `record_diff(tool_call_id: str, file_path: str, added: int, removed: int) -> list[str]`, `flush() -> list[str]`, `active_status() -> ActiveToolStatus | None`.
- Consumed by: Task 4.

- [ ] **Step 1: Write failing state-machine tests**

Create `tests/test_tool_progress.py` with helpers that convert ANSI through `rich.text.Text.from_ansi`, then assert these exact scenarios:

```python
from rich.text import Text

from core.tool_progress import ToolProgressAggregator


def plain(lines):
    return Text.from_ansi("".join(lines)).plain


def tool_payload(tool_id, name, category, action, target, *, ok=True,
                 duration_ms=100, command="", error_summary="", metrics=None):
    payload = {
        "id": tool_id,
        "name": name,
        "ok": ok,
        "display": {
            "category": category,
            "action": action,
            "target": target,
            "command": command,
            "metrics": metrics or {},
            "error_summary": error_summary,
            "is_skill": False,
        },
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    return payload


def read_payload(tool_id, path, duration_ms=100, line_count=10):
    return tool_payload(
        tool_id, "read_file", "read", "读取文件", path,
        duration_ms=duration_ms, metrics={"line_count": line_count},
    )


def search_payload(tool_id, query, duration_ms=100):
    return tool_payload(
        tool_id, "search_code", "search", "搜索代码", query,
        duration_ms=duration_ms,
    )


def command_payload(tool_id, command, ok, duration_ms=100):
    return tool_payload(
        tool_id, "execute_command", "command", "运行命令", command,
        ok=ok, duration_ms=duration_ms, command=command,
        error_summary="" if ok else "ModuleNotFoundError: pytest",
    )


def mutation_payload(tool_id, path):
    return tool_payload(tool_id, "edit_file", "mutation", "修改文件", path)


def test_three_reads_flush_as_one_group():
    progress = ToolProgressAggregator(clock=lambda: 10.0)
    for index in range(3):
        payload = read_payload(str(index), f"file-{index}.py", duration_ms=100)
        progress.start(payload)
        assert progress.finish(payload) == []
    output = plain(progress.flush())
    assert output.count("已读取") == 1
    assert "已读取 3 个文件" in output
    assert "30 行" in output
    assert "0.3s" in output


def test_category_change_flushes_previous_group():
    progress = ToolProgressAggregator(clock=lambda: 10.0)
    read = read_payload("read", "a.py", duration_ms=100)
    search = search_payload("search", "target", duration_ms=250)
    progress.start(read)
    progress.finish(read)
    progress.start(search)
    assert "已读取 1 个文件" in plain(progress.finish(search))
    assert "已完成 1 次搜索" in plain(progress.flush())


def test_command_failure_is_visible_and_next_command_starts():
    progress = ToolProgressAggregator(clock=lambda: 5.0)
    failed = command_payload("bad", "pytest -q", ok=False, duration_ms=3200)
    corrected = command_payload("good", "python -m pytest -q", ok=True, duration_ms=100)
    progress.start(failed)
    failed_output = plain(progress.finish(failed))
    assert "运行命令失败" in failed_output
    assert "pytest -q" in failed_output
    assert "ModuleNotFoundError: pytest" in failed_output
    progress.start(corrected)
    assert progress.active_status().detail == "python -m pytest -q"
    assert "命令执行完成" in plain(progress.finish(corrected))


def test_missing_duration_uses_injected_clock():
    ticks = iter([10.0, 10.251])
    progress = ToolProgressAggregator(clock=lambda: next(ticks))
    payload = read_payload("read", "a.py", duration_ms=None)
    progress.start(payload)
    progress.finish(payload)
    assert "0.3s" in plain(progress.flush())


def test_diff_suppresses_duplicate_mutation_line():
    progress = ToolProgressAggregator(clock=lambda: 5.0)
    mutation = mutation_payload("edit-1", "core/turn_renderer.py")
    progress.start(mutation)
    diff_line = plain(progress.record_diff("edit-1", "core/turn_renderer.py", 28, 14))
    assert "已修改 core/turn_renderer.py" in diff_line
    assert "+28 / -14 行" in diff_line
    assert progress.finish(mutation) == []
```

The test helpers must construct complete Task 2 `display` mappings; no production parser reads arbitrary result text.

- [ ] **Step 2: Run and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_tool_progress.py
```

Expected: `ModuleNotFoundError: core.tool_progress`.

- [ ] **Step 3: Implement immutable normalization and active state**

Create:

```python
@dataclass(frozen=True)
class ToolDisplayEvent:
    tool_id: str
    name: str
    category: str
    action: str
    target: str
    command: str
    metrics: dict[str, int]
    error_summary: str
    is_skill: bool
    ok: bool | None
    duration_ms: int | None


@dataclass(frozen=True)
class ActiveToolStatus:
    label: str
    detail: str
    count: int
    started_at: float
```

`ToolDisplayEvent.from_payload()` accepts optional legacy fields, but unknown events stay category `other`; it must not inspect `result` to infer category or metrics.

- [ ] **Step 4: Implement deterministic aggregation**

Use `OrderedDict[str, tuple[ToolDisplayEvent, float]]` for active tools, one `_PendingGroup`, and a `set[str]` of mutation tool IDs already represented by Diffs. Required transitions:

- `start`: replace the same ID, never duplicate it;
- `finish`: pop active; derive duration from the injected monotonic clock only when the event lacks `duration_ms`;
- successful `read/search`: append to same-category pending group;
- category change/failure/skill/command/mutation: flush pending group first;
- failure: emit command/target plus error summary and remain usable;
- `record_diff(tool_call_id, ...)`: flush group, emit one mutation line, mark that exact tool ID;
- `active_status`: combine parallel homogeneous read/search calls, otherwise show the latest active action;
- `flush`: emit one aggregate line and clear only the pending group.

All output helpers return ANSI strings and never perform terminal I/O.

- [ ] **Step 5: Run and verify GREEN**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 6: Commit CLI state machine**

```powershell
git add core\tool_progress.py tests\test_tool_progress.py
git commit -m "feat: aggregate CLI tool progress"
```

---

### Task 4: CLI TurnRenderer Integration

**Files:**
- Modify: `core/turn_renderer.py`
- Modify: `tests/test_turn_renderer.py`

**Interfaces:**
- Consumes: Task 3 `ToolProgressAggregator`.
- Produces: `TurnRenderer.active_tool_status() -> ActiveToolStatus | None`.
- Produces: `TurnRenderer.flush_tool_progress() -> list[str]`.
- Preserves: assistant text sanitization, Markdown buffering, plans, errors, and `assistant_text()`.

- [ ] **Step 1: Replace obsolete silent-tool assertions with failing B+C tests**

Add tests for:

```python
def test_renderer_groups_reads_before_answer():
    renderer = TurnRenderer()
    for index in range(3):
        payload = read_payload(str(index), f"file-{index}.py")
        renderer.consume("tool_started", payload)
        renderer.consume("tool_finished", payload)
    group = Text.from_ansi("".join(renderer.flush_tool_progress())).plain
    assert group.count("已读取") == 1
    assert "已读取 3 个文件" in group
    answer = "".join(renderer.consume("assistant_delta", {"text": "结论"}))
    assert "结论" in answer


def test_renderer_exposes_failed_then_active_command():
    renderer = TurnRenderer()
    failed = command_payload("bad", "pytest -q", ok=False)
    renderer.consume("tool_started", failed)
    output = Text.from_ansi("".join(renderer.consume("tool_finished", failed))).plain
    assert "运行命令失败" in output
    corrected = command_payload("good", "python -m pytest -q", ok=True)
    renderer.consume("tool_started", corrected)
    assert renderer.active_tool_status().detail == "python -m pytest -q"
```

Delete tests requiring successful/failed tools to remain silent, the one-time “星瑶正在干活” history header, and `concise_recap()`.

- [ ] **Step 2: Run and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_turn_renderer.py
```

Expected: missing progress methods and old silent behavior failures.

- [ ] **Step 3: Delegate tool events to the aggregator**

Instantiate one aggregator in `TurnRenderer.__init__()`. Handle `tool_started`, `tool_finished`, and `diff_ready`; flush on `thinking_started`, `assistant_delta`, and `_flush_pending()`. Add:

```python
def active_tool_status(self):
    return self._tool_progress.active_status()


def flush_tool_progress(self) -> list[str]:
    self._emitted = []
    self._append_tool_lines(self._tool_progress.flush())
    return list(self._emitted)
```

For Diff events, pass `tool_call_id`, `file_path`, and `_count_diff_changes(diff)` to `record_diff()`. Remove the obsolete concise recap fields and methods.

- [ ] **Step 4: Run CLI renderer tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_tool_progress.py tests\test_turn_renderer.py tests\test_tool_presenter.py
```

Expected: PASS.

- [ ] **Step 5: Commit renderer integration**

```powershell
git add core\turn_renderer.py tests\test_turn_renderer.py
git commit -m "feat: render grouped CLI tool progress"
```

---

### Task 5: CLI Live Activity and Thinking Cleanup

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main_ui_events.py`

**Interfaces:**
- Consumes: Task 4 active status and explicit flush.
- Produces: `_format_activity_text(status, active_status, elapsed, frame) -> str`.
- Preserves: UI event queue, Ctrl+C, temporary reasoning panel, prompt_toolkit thread ownership.

- [ ] **Step 1: Write failing UI tests**

Add exact tests asserting:

```python
def test_activity_text_shows_command_and_interrupt_hint():
    status = ActiveToolStatus(
        label="正在运行测试",
        detail=r"venv\Scripts\python -m pytest -q",
        count=1,
        started_at=10.0,
    )
    assert main._format_activity_text("executing", status, 8.4, "⠹") == (
        "  ⠹ 正在运行测试 · 8.4s · Ctrl+C 中断\n"
        "    └─ venv\\Scripts\\python -m pytest -q"
    )
```

Also replace the thinking-divider test with three thinking segments and assert no emitted fragment contains `思考完成`. Add an event-level test where a failed command is printed, a corrected command becomes active, and `task_status` never becomes `idle` between them. Add a buffering test asserting grouped tool ANSI is sent directly to `_print_ui_fragment` and `_assistant_stream_buffers[turn_id]` contains only assistant text.

- [ ] **Step 2: Run and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_main_ui_events.py -k "activity_text or thinking_segments or failure_continues or outside_markdown"
```

- [ ] **Step 3: Implement live structured activity**

Replace `_current_tool_name` with `_active_tool_status`. On the first active tool, set display-only `activity_start_time = time.time()`; additional parallel starts do not reset it. Never mix Task 3’s monotonic `started_at` with wall-clock UI elapsed time.

Before consuming `assistant_delta`, call `renderer.flush_tool_progress()` and print those fragments directly. Remove `thinking_finished` history separators and `concise_recap()` calls. Clear active status only on thinking transitions and terminal turn states.

All changes remain inside `_render_ui_event()` on the prompt_toolkit thread; background callbacks continue to enqueue immutable events.

- [ ] **Step 4: Run and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_main_ui_events.py tests\test_turn_renderer.py tests\test_tool_progress.py
```

- [ ] **Step 5: Commit CLI live UI**

```powershell
git add main.py tests\test_main_ui_events.py
git commit -m "feat: show live CLI tool activity"
```

---

### Task 6: GUI Bridge Contract and Message Ownership Events

**Files:**
- Modify: `gui_bridge.py`
- Create: `tests/test_gui_tool_events.py`

**Interfaces:**
- Consumes: Task 2 `display`, `duration_ms`, `tool_call_id`.
- Produces: GUI events `tool_start`, `tool_finish`, `diff_ready`, and `turn_error` with the approved fields.
- Preserves existing event names and result-length limits.

- [ ] **Step 1: Write failing bridge tests**

Create a bridge via `ApiBridge.__new__(ApiBridge)`, assign a fresh `threading.Event()` to `_cancel_event`, replace `emit_event` with a collector, and monkeypatch `core.llm.chat_with_model_events`. Assert:

```python
import threading

from core.turn_events import TurnEventFactory
from gui_bridge import ApiBridge


def make_events(items):
    factory = TurnEventFactory("turn-gui-tools")
    return [factory.emit(kind, payload) for kind, payload in items]


def start_payload(tool_id):
    return {
        "id": tool_id,
        "name": "execute_command",
        "summary": "运行命令",
        "display": {
            "category": "command",
            "action": "运行命令",
            "target": "pytest -q",
            "command": "pytest -q",
            "metrics": {},
            "error_summary": "",
            "is_skill": False,
        },
    }


def finish_payload(tool_id, ok, duration_ms):
    payload = start_payload(tool_id)
    payload.update({
        "ok": ok,
        "duration_ms": duration_ms,
        "summary": "命令执行失败" if not ok else "命令执行完成",
        "result": "退出码 1" if not ok else "passed",
        "display": {
            **payload["display"],
            "error_summary": "ModuleNotFoundError: pytest" if not ok else "",
        },
    })
    return payload


def consume_bridge_events(monkeypatch, events):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._cancel_event = threading.Event()
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))
    monkeypatch.setattr("core.llm.chat_with_model_events", lambda *args, **kwargs: iter(events))
    bridge._consume_turn_events([], "model", "key", None, "session", "turn-gui-tools")
    return emitted


def test_bridge_deduplicates_start_and_forwards_display(monkeypatch):
    events = make_events([
        ("tool_requested", start_payload("cmd-1")),
        ("tool_started", start_payload("cmd-1")),
        ("tool_finished", finish_payload("cmd-1", ok=False, duration_ms=3200)),
        ("turn_finished", {}),
    ])
    emitted = consume_bridge_events(monkeypatch, events)
    starts = [data for kind, data in emitted if kind == "tool_start"]
    finishes = [data for kind, data in emitted if kind == "tool_finish"]
    assert len(starts) == 1
    assert starts[0]["display"]["category"] == "command"
    assert finishes[0]["duration_ms"] == 3200
    assert finishes[0]["display"]["error_summary"] == "ModuleNotFoundError: pytest"


def test_bridge_forwards_diff_tool_call_id(monkeypatch):
    events = make_events([
        ("diff_ready", {
            "tool_call_id": "edit-1",
            "file_path": "a.py",
            "diff_text": "--- a.py\n+++ a.py\n-old\n+new",
        }),
        ("turn_finished", {}),
    ])
    emitted = consume_bridge_events(monkeypatch, events)
    diff = next(data for kind, data in emitted if kind == "diff_ready")
    assert diff["tool_call_id"] == "edit-1"


def test_bridge_emits_turn_error(monkeypatch):
    events = make_events([
        ("turn_error", {"message": "provider disconnected"}),
    ])
    emitted = consume_bridge_events(monkeypatch, events)
    assert ("turn_error", {"message": "provider disconnected"}) in emitted
```

- [ ] **Step 2: Run and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_gui_tool_events.py
```

- [ ] **Step 3: Implement exact forwarding**

Inside `_consume_turn_events()`, maintain `started_tool_ids: set[str]`. Emit `tool_start` only on the first requested/started event for a non-empty ID. Forward a copied `display` mapping on start and finish, plus `duration_ms` on finish and `tool_call_id` on Diff:

```python
if tool_id not in started_tool_ids:
    started_tool_ids.add(tool_id)
    self.emit_event("tool_start", {
        "id": tool_id,
        "name": name,
        "label": label,
        "display": dict(data.get("display") or {}),
    })
```

If the backend produces an empty tool ID, emit a single `tool_start` with the empty ID so the frontend can render an explicit contract-error row; do not manufacture an ID in the bridge.

Translate model `turn_error` to a GUI `turn_error` event and keep the existing `error` toast event. In `_run_turn_worker`'s outer exception handler, emit both events with the same sanitized message. The existing final `turn_finish` may still unlock the input; Task 9 must not overwrite an earlier `error` or `cancelled` message state.

- [ ] **Step 4: Run bridge regression tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_gui_tool_events.py tests\test_gui_workspace_groups.py tests\test_gui_theme.py
```

- [ ] **Step 5: Commit GUI bridge contract**

```powershell
git add tests\test_gui_tool_events.py
git add -p gui_bridge.py
git diff --cached -- gui_bridge.py tests\test_gui_tool_events.py
git commit -m "feat: forward structured GUI tool events"
```

Stage only Task 6 hunks from `gui_bridge.py`; the task-start user hunk must remain unstaged.

---

### Task 7: Frontend Trace Domain and Test Harness

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `frontend/vite.config.ts`
- Create: `frontend/src/utils/toolTrace.ts`
- Create: `frontend/src/utils/toolTrace.test.ts`

**Interfaces:**
- Produces: `ToolDisplay`, `ToolExecutionResult`, `ToolExecutionCall`, `ToolTraceRow`, `ExecutionState`.
- Produces: `buildToolTraceRows(calls: ToolExecutionCall[], legacyDiffs?: LegacyDiff[]) -> ToolTraceRow[]`.
- Produces: `countDiffChanges(diffText: string) -> { added: number; removed: number }`.
- Consumed by: Tasks 8–9.

- [ ] **Step 1: Install test-only dependencies and add the test script**

Run in `frontend`:

```powershell
npm install --save-dev vitest @vue/test-utils jsdom
```

Set `package.json` script to `"test": "vitest run"`. In `vite.config.ts`, add `test: { environment: 'jsdom' }` while preserving existing server/build settings.

- [ ] **Step 2: Write failing pure-domain tests**

Create tests covering:

```ts
import { describe, expect, it } from 'vitest';
import {
  buildToolTraceRows,
  countDiffChanges,
  type ToolDisplay,
  type ToolExecutionCall,
} from './toolTrace';


function readCall(id: string, path: string, durationMs: number, lines: number): ToolExecutionCall {
  const display: ToolDisplay = {
    category: 'read', action: '读取文件', target: path, command: '',
    metrics: { line_count: lines }, error_summary: '', is_skill: false,
  };
  return {
    id, name: 'read_file', label: `读取文件 ${path}`, display,
    result: { ok: true, summary: '读取完成', result: '', duration_ms: durationMs, display },
  };
}


function legacyReadCall(id: string, path: string): ToolExecutionCall {
  return {
    id, name: 'read_file', label: `读取文件 ${path}`,
    result: { ok: true, summary: '读取完成', result: '' },
  };
}


function commandCall(id: string, command: string, ok: boolean,
                     durationMs: number): ToolExecutionCall {
  const display: ToolDisplay = {
    category: 'command', action: '运行命令', target: command, command,
    metrics: {}, error_summary: ok ? '' : 'ModuleNotFoundError: pytest',
    is_skill: false,
  };
  return {
    id, name: 'execute_command', label: `运行命令 ${command}`, display,
    result: { ok, summary: ok ? '完成' : '失败', result: '', duration_ms: durationMs, display },
  };
}


it('groups adjacent successful reads', () => {
  const rows = buildToolTraceRows([
    readCall('1', 'a.py', 100, 10),
    readCall('2', 'b.py', 200, 20),
    readCall('3', 'c.py', 300, 30),
  ]);
  expect(rows).toHaveLength(1);
  expect(rows[0]).toMatchObject({ kind: 'read-group', count: 3, durationMs: 600 });
  expect(rows[0].metrics.line_count).toBe(60);
});

it('flushes a read group before a failure and keeps later work', () => {
  const rows = buildToolTraceRows([
    readCall('1', 'a.py', 100, 10),
    commandCall('bad', 'pytest -q', false, 3200),
    commandCall('good', 'python -m pytest -q', true, 100),
  ]);
  expect(rows.map(row => row.kind)).toEqual(['read-group', 'command', 'command']);
  expect(rows[1].status).toBe('failed');
  expect(rows[2].status).toBe('completed');
});

it('does not invent a legacy duration', () => {
  const rows = buildToolTraceRows([legacyReadCall('1', 'a.py')]);
  expect(rows[0].durationMs).toBeUndefined();
});

it('counts only standard unified diff changes', () => {
  expect(countDiffChanges('--- a.py\n+++ a.py\n-old\n+new\n context')).toEqual({
    added: 1,
    removed: 1,
  });
});

it('keeps legacy diffs in an explicit final row', () => {
  const rows = buildToolTraceRows([], [{ file_path: 'a.py', diff_text: '+new' }]);
  expect(rows[0].kind).toBe('legacy-diffs');
});
```

- [ ] **Step 3: Run and verify RED**

```powershell
npm test -- src/utils/toolTrace.test.ts
```

Expected: module-not-found failure.

- [ ] **Step 4: Implement explicit types and grouping**

Use discriminated status values:

```ts
export type ExecutionState = 'running' | 'completed' | 'cancelled' | 'error';
export type ToolCallStatus = 'running' | 'completed' | 'failed' | 'cancelled';

export type ToolCategory =
  | 'read' | 'search' | 'command' | 'mutation' | 'skill' | 'other';

export interface ToolDisplay {
  category: ToolCategory;
  action: string;
  target: string;
  command: string;
  metrics: Record<string, number>;
  error_summary: string;
  is_skill: boolean;
}

export interface ToolExecutionResult {
  ok: boolean;
  summary: string;
  result: string;
  duration_ms?: number;
  display?: ToolDisplay;
}

export interface SubagentTaskSnapshot {
  id: string;
  description: string;
  status: string;
  stage: string;
  stage_detail?: string;
  elapsed: number;
}

export interface ToolExecutionCall {
  id: string;
  name: string;
  label: string;
  started_at_ms?: number;
  display?: ToolDisplay;
  result?: ToolExecutionResult;
  diff?: LegacyDiff;
  subagent_tasks?: SubagentTaskSnapshot[];
}

export interface LegacyDiff {
  file_path: string;
  diff_text: string;
}

export interface ToolTraceRow {
  id: string;
  kind: 'read-group' | 'search-group' | 'command' | 'mutation' |
        'skill' | 'tool' | 'legacy-diffs';
  status: ToolCallStatus;
  action: string;
  detail: string;
  command: string;
  errorSummary: string;
  count: number;
  durationMs?: number;
  metrics: Record<string, number>;
  calls: ToolExecutionCall[];
}
```

Group only adjacent completed-success read/search calls. Running parallel homogeneous reads/searches may share one running row; failed or mixed-category calls remain separate. Legacy category fallback is an explicit map by tool name. Never inspect `result.result` to determine category, success, or metrics.

- [ ] **Step 5: Run utility tests and build**

```powershell
npm test -- src/utils/toolTrace.test.ts
npm run build
```

Expected: both commands pass.

- [ ] **Step 6: Commit frontend trace domain**

```powershell
git add frontend\package.json frontend\package-lock.json frontend\vite.config.ts frontend\src\utils\toolTrace.ts frontend\src\utils\toolTrace.test.ts
git commit -m "feat: add GUI tool trace domain"
```

---

### Task 8: Interactive GUI Timeline Row

**Files:**
- Create: `frontend/src/components/ToolExecutionRow.vue`
- Create: `frontend/src/components/ToolExecutionRow.test.ts`

**Interfaces:**
- Consumes: Task 7 `ToolTraceRow`.
- Props: `row: ToolTraceRow`, `elapsedMs?: number`.
- Produces: no store mutations; owns only local result/Diff expansion state.

- [ ] **Step 1: Write failing row-component tests**

Using Vue Test Utils, assert:

```ts
import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';
import ToolExecutionRow from './ToolExecutionRow.vue';
import type { ToolTraceRow } from '../utils/toolTrace';


function failedCommandRow(): ToolTraceRow {
  return {
    id: 'bad', kind: 'command', status: 'failed', action: '运行命令',
    detail: 'pytest -q', command: 'pytest -q',
    errorSummary: 'ModuleNotFoundError: pytest', count: 1, durationMs: 3200,
    metrics: {},
    calls: [{
      id: 'bad', name: 'execute_command', label: '运行命令',
      result: { ok: false, summary: '失败', result: '命令执行失败（退出码 1）' },
    }],
  };
}


function mutationRowWithDiff(): ToolTraceRow {
  return {
    id: 'edit-1', kind: 'mutation', status: 'completed', action: '修改文件',
    detail: 'a.py', command: '', errorSummary: '', count: 1, durationMs: 100,
    metrics: {},
    calls: [{
      id: 'edit-1', name: 'edit_file', label: '修改文件 a.py',
      result: { ok: true, summary: '完成', result: '' },
      diff: { file_path: 'a.py', diff_text: '--- a.py\n+++ a.py\n-old\n+new' },
    }],
  };
}


it('shows a failed command and continues to expose details', async () => {
  const wrapper = mount(ToolExecutionRow, { props: { row: failedCommandRow() } });
  expect(wrapper.text()).toContain('运行命令失败');
  expect(wrapper.text()).toContain('pytest -q');
  expect(wrapper.text()).toContain('ModuleNotFoundError: pytest');
  const button = wrapper.get('button[data-detail-toggle]');
  expect(button.attributes('aria-expanded')).toBe('false');
  await button.trigger('click');
  expect(button.attributes('aria-expanded')).toBe('true');
  expect(wrapper.get('pre').text()).toContain('退出码 1');
});

it('expands a mutation diff in the same row', async () => {
  const wrapper = mount(ToolExecutionRow, { props: { row: mutationRowWithDiff() } });
  expect(wrapper.text()).toContain('+1 / -1 行');
  await wrapper.get('button[data-diff-toggle]').trigger('click');
  expect(wrapper.findAll('.diff-line.add')).toHaveLength(1);
  expect(wrapper.findAll('.diff-line.del')).toHaveLength(1);
});
```

Add tests for a grouped-read file list, missing duration omission, and `run_subagent` task snapshots.

- [ ] **Step 2: Run and verify RED**

```powershell
npm test -- src/components/ToolExecutionRow.test.ts
```

- [ ] **Step 3: Implement the row component**

Use native buttons and text interpolation. Required structure:

```vue
<div class="trace-row" :class="`status-${row.status}`">
  <span class="trace-icon" aria-hidden="true">{{ statusIcon }}</span>
  <div class="trace-main">
    <div class="trace-title">
      <span>{{ title }}</span>
      <span v-if="durationText" class="trace-duration">{{ durationText }}</span>
    </div>
    <div v-if="row.command || row.detail || row.errorSummary" class="trace-detail">
      {{ row.errorSummary || row.command || row.detail }}
    </div>
    <button v-if="hasResult" data-detail-toggle :aria-expanded="resultOpen" @click="resultOpen = !resultOpen">
      {{ resultOpen ? '收起详情' : '展开详情' }}
    </button>
    <pre v-if="resultOpen" class="trace-result">{{ resultText }}</pre>
  </div>
</div>
```

Render Diff lines with Vue interpolation and explicit `add/del/hdr` classes. Never use `v-html` for tool output. Add `@media (prefers-reduced-motion: reduce)` to stop the spinner animation. Limit results to 240px with internal scrolling.

- [ ] **Step 4: Run row tests and build**

```powershell
npm test -- src/components/ToolExecutionRow.test.ts
npm run build
```

- [ ] **Step 5: Commit the row component**

```powershell
git add frontend\src\components\ToolExecutionRow.vue frontend\src\components\ToolExecutionRow.test.ts
git commit -m "feat: add GUI tool timeline rows"
```

---

### Task 9: GUI Trace, Message-Local Store, and MessageBubble Integration

**Files:**
- Create: `frontend/src/components/ToolExecutionTrace.vue`
- Create: `frontend/src/components/ToolExecutionTrace.test.ts`
- Modify: `frontend/src/components/MessageBubble.vue`
- Create: `frontend/src/components/MessageBubble.test.ts`
- Modify: `frontend/src/stores/appStore.ts`
- Create: `frontend/src/stores/appStore.toolEvents.test.ts`
- Modify: `frontend/src/types/pywebview.d.ts`

**Interfaces:**
- Consumes: Tasks 6–8.
- Produces: message-local `tool_calls`, `legacy_diffs`, `execution_state`.
- `ToolExecutionTrace` props: `calls`, `legacyDiffs`, `state`.

- [ ] **Step 1: Write failing store ownership tests**

With a fresh Pinia store, drive exact bridge events:

```ts
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';


const event = (type: string, data: any) => ({ type, data });
const startData = (id: string, command: string) => ({
  id, name: 'execute_command', label: `运行命令 ${command}`,
  display: {
    category: 'command', action: '运行命令', target: command, command,
    metrics: {}, error_summary: '', is_skill: false,
  },
});
const finishData = (id: string, ok: boolean, duration_ms: number) => ({
  ...startData(id, id === 'bad' ? 'pytest -q' : 'python -m pytest -q'),
  ok, duration_ms, summary: ok ? '完成' : '失败', result: ok ? 'passed' : '退出码 1',
});

beforeEach(() => setActivePinia(createPinia()));

it('keeps a failed tool and the corrected active tool in one message', () => {
const store = useAppStore();
store.handleBridgeEvent(event('turn_start', { user_text: '检查代码' }));
store.handleBridgeEvent(event('tool_start', startData('bad', 'pytest -q')));
store.handleBridgeEvent(event('tool_finish', finishData('bad', false, 3200)));
store.handleBridgeEvent(event('tool_start', startData('good', 'python -m pytest -q')));

const message = store.messages.at(-1)!;
expect(message.execution_state).toBe('running');
expect(message.tool_calls).toHaveLength(2);
expect(message.tool_calls![0].result!.ok).toBe(false);
expect(message.tool_calls![1].result).toBeUndefined();
expect(store.running).toBe(true);
});
```

Add tests that `diff_ready.tool_call_id` binds to only the matching call, old history tool messages pair by explicit ID, and switching sessions does not retain previous live results.

- [ ] **Step 2: Write failing trace and MessageBubble tests**

Assert that:

- running trace title is `执行过程 · N 项 · 进行中`;
- terminal states map to `已完成/已取消/异常结束`;
- the trace header button controls `aria-expanded`;
- the timer is one interval per running trace and clears on unmount/terminal state;
- `MessageBubble` order is thinking block, trace, Markdown answer;
- the empty “星瑶正在干活” line disappears after the first tool call;
- no legacy `.tool-card` or standalone `.diff-list` remains.

- [ ] **Step 3: Run all new tests and verify RED**

```powershell
npm test -- src/stores/appStore.toolEvents.test.ts src/components/ToolExecutionTrace.test.ts src/components/MessageBubble.test.ts
```

- [ ] **Step 4: Define typed message-local state**

Import Task 7 types into `appStore.ts`. On `turn_start`, create:

```ts
{
  role: 'assistant',
  content: '',
  reasoning_content: '',
  tool_calls: [],
  legacy_diffs: [],
  execution_state: 'running',
}
```

Use `activeAssistantOrThrow()` to raise an explicit contract error when a turn-scoped event arrives without an assistant message. `tool_start` upserts by ID and records `started_at_ms = Date.now()` only on first insertion; empty IDs receive a UI-only key `contract-error-${tool_calls.length + 1}` and category `other`. `tool_finish` updates the same call, or creates an explicit orphan completion call and logs `console.error`. `diff_ready` binds only by non-empty `tool_call_id`; otherwise append to `legacy_diffs`.

On `turn_finish`, set `execution_state = 'completed'` only when the current state is still `running`; preserve earlier `cancelled` and `error` states.

Move subagent snapshots into the current serial `run_subagent` call and freeze the last snapshot on completion. Remove the migrated live path’s dependency on global `toolResults`; delete the field after all references are gone.

- [ ] **Step 5: Implement `ToolExecutionTrace.vue`**

Use `buildToolTraceRows()` in a computed value. The header count is the number of raw calls registered so far, not a predicted total. Use one interval to update `now` while `state === 'running'`, derive active elapsed time from the earliest running call's `started_at_ms`, and pass it to rows. The header is a native button with `aria-expanded`; default expanded is `true`.

Render one `ToolExecutionRow` per stable row ID. Use `aria-live="polite"` only for action changes; keep elapsed seconds outside the live region.

- [ ] **Step 6: Replace MessageBubble tool/Diff markup**

Position:

```vue
<ThinkBlock />
<ToolExecutionTrace
  v-if="msg.tool_calls?.length || msg.legacy_diffs?.length"
  :calls="msg.tool_calls || []"
  :legacy-diffs="msg.legacy_diffs || []"
  :state="msg.execution_state || 'completed'"
/>
<div v-if="msg.content?.trim()" class="md-body" ... />
```

Keep the existing inline thinking markup rather than creating an unrelated `ThinkBlock` component; the snippet shows ordering only. Remove old tool-card helpers/styles and the independent Diff loop/styles after the new components cover them.

- [ ] **Step 7: Run frontend tests and build**

```powershell
npm test
npm run build
```

Expected: all Vitest tests and TypeScript/Vite build pass.

- [ ] **Step 8: Commit GUI message timeline**

Before staging, compare `git diff -- frontend/src/stores/appStore.ts` with the task-start snapshot. Preserve any user hunks that appeared before or during the task, and stage the file only after both the task changes and those user hunks are still present.

```powershell
git add frontend\src\components\ToolExecutionTrace.vue frontend\src\components\ToolExecutionTrace.test.ts frontend\src\components\MessageBubble.vue frontend\src\components\MessageBubble.test.ts frontend\src\stores\appStore.toolEvents.test.ts frontend\src\types\pywebview.d.ts
git add -p frontend\src\stores\appStore.ts
git diff --cached -- frontend\src\stores\appStore.ts
git commit -m "feat: add GUI tool execution timeline"
```

---

### Task 10: Cross-UI Acceptance, Documentation, and Full Verification

**Files:**
- Modify: `tests/test_agent_turn_e2e.py`
- Modify: `README.md`
- Verify all files from Tasks 1–9.

**Interfaces:**
- Consumes both completed tracks.
- Produces one deterministic event-sequence acceptance test and user-facing documentation.

- [ ] **Step 1: Add the shared end-to-end failure-continuation test**

Mock model rounds in this exact order:

1. three `read_file` calls;
2. `search_code` plus `glob_files`;
3. failed `execute_command("pytest -q")`;
4. successful `execute_command("python -m pytest -q")`;
5. `edit_file("a.py", ...)` with Diff;
6. final text `检查结论：失败已修正，任务继续并完成。`.

Assert the event list ends in `turn_finished`, includes a tool start after the failed finish, and has exactly one assistant final-text sequence. Feed the same events through `TurnRenderer` and assert one read group, one search group, visible failure, corrected command, mutation, and final text.

- [ ] **Step 2: Run the acceptance test**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_agent_turn_e2e.py::test_tool_progress_groups_work_and_continues_after_failure
```

- [ ] **Step 3: Update README**

Remove the stale `/verbose <on|off>` row. Document:

```text
工具执行反馈默认采用精简进度模式：CLI 与 GUI 会合并连续读取和搜索；
命令、文件修改、子代理和失败操作单独留痕；普通工具失败会返回模型继续分析，
不会被静默隐藏。GUI 可展开完整工具结果和对应文件 Diff。
```

- [ ] **Step 4: Run focused Python suites**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_tool_presenter.py tests\test_tool_progress.py tests\test_turn_renderer.py tests\test_main_ui_events.py tests\test_llm_events.py tests\test_llm_events_fixes.py tests\test_gui_tool_events.py tests\test_agent_turn_e2e.py
```

- [ ] **Step 5: Run all frontend tests and build**

```powershell
Set-Location frontend
npm test
npm run build
Set-Location ..
```

- [ ] **Step 6: Run full Python verification**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m py_compile main.py core\llm.py core\memory.py core\memory_compressor.py core\themes.py core\tool_presenter.py core\tool_progress.py core\turn_renderer.py gui_bridge.py
```

- [ ] **Step 7: Run source CLI smoke test**

```powershell
.\.venv\Scripts\python.exe main.py
```

Use the source command, not the older installed `D:\app\ycode\ycode.exe`. Verify no thinking-divider wall, live action text, B+C grouping, visible failure continuation, and one final answer.

- [ ] **Step 8: Run GUI visual smoke test**

Start the frontend development server and GUI using the project’s documented commands. Execute the fixed acceptance sequence and verify:

- one trace per assistant message;
- trace appears between thinking and answer;
- failure stays red while the next row runs;
- Diff opens inside its mutation row;
- dark/light themes and narrow width remain readable;
- Enter/Space toggles details;
- switching sessions does not cross-link results.

- [ ] **Step 9: Inspect repository state and commit integration**

```powershell
git diff --check
git status --short
git add README.md tests\test_agent_turn_e2e.py
git commit -m "test: verify CLI and GUI tool progress"
```

Confirm `.superpowers/` and unrelated user files are unstaged.

## Final Review Checklist

- [ ] Tasks 1–2 passed before either parallel track began.
- [ ] CLI and GUI tracks did not edit shared contract files after Task 2.
- [ ] Every tool display string is sanitized once in the shared presenter.
- [ ] No arbitrary result-text parser infers tool category, success, or metrics.
- [ ] Ordinary failure is followed by a later tool event in Python and frontend tests.
- [ ] New Diffs use exact `tool_call_id`; legacy Diffs are explicitly unbound.
- [ ] Message-local results survive history loading and do not leak across sessions.
- [ ] CLI terminal writes remain on the prompt_toolkit thread.
- [ ] GUI tool output uses text interpolation, not `v-html`.
- [ ] Full pytest, Vitest, frontend build, and `py_compile` pass.
- [ ] Manual source CLI and GUI smoke checks pass.
- [ ] Only task-owned files are staged in each commit.
