# Y-code CodeBuddy Tool Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace silent multi-tool turns and repeated “思考完成” separators with a CodeBuddy-style B+C tool progress UI that groups reads/searches, shows commands and failures, and continues after ordinary tool failures.

**Architecture:** Add structured tool display metadata at the existing event source, then introduce a pure `ToolProgressAggregator` that owns active tools, deterministic grouping, diff summaries, and static output formatting. `TurnRenderer` delegates tool events to that state machine, while `main.py` renders its active snapshot in the existing prompt_toolkit activity area and keeps all terminal writes on the UI thread.

**Tech Stack:** Python 3.12, prompt_toolkit, ANSI terminal fragments, existing immutable `TurnEvent` stream, pytest.

## Global Constraints

- Work only on the `dev` branch; do not merge to `master`.
- Preserve unrelated user changes in `AGENTS.md`, frontend files, setup/uninstall files, and `.superpowers/`.
- Do not add a runtime dependency.
- Do not expose model-private reasoning in terminal history.
- Do not introduce automatic retries, silent fallbacks, heuristic generic output parsing, or downgraded algorithms.
- Ordinary tool failures remain `tool` results and do not become `turn_error`; cancellation, API failure, permission-system failure, and state corruption still terminate the turn.
- Background workers may only enqueue events. `print_formatted_text`, `_print_ui_fragment`, and `app.invalidate()` remain on the prompt_toolkit main thread.
- New `tool_finished` fields are optional and backward compatible with GUI consumers.
- Database message content and tool execution order remain unchanged.

## File Structure

- Create `core/tool_progress.py`: immutable display-event normalization, active-tool snapshots, deterministic grouping, diff tracking, ANSI line formatting, and control-character sanitization.
- Create `tests/test_tool_progress.py`: focused state-machine and formatting tests using an injected clock.
- Modify `core/tool_presenter.py`: produce explicit structured display metadata for each known tool.
- Modify `core/llm.py`: attach structured metadata and measured `duration_ms` to `tool_finished` without changing control flow.
- Modify `core/turn_renderer.py`: delegate tool events to `ToolProgressAggregator`, flush groups on boundaries, and remove the obsolete silent recap path.
- Modify `main.py`: render the active snapshot in the existing activity panel and stop writing `thinking_finished` separators to history.
- Modify `tests/test_tool_presenter.py`, `tests/test_llm_events.py`, `tests/test_turn_renderer.py`, `tests/test_main_ui_events.py`, and `tests/test_agent_turn_e2e.py`: red-green coverage at each integration boundary.
- Modify `README.md`: remove the stale `/verbose` command and describe the default B+C progress behavior.

---

### Task 1: Structured Tool Display Metadata

**Files:**
- Modify: `core/tool_presenter.py:1-130`
- Modify: `tests/test_tool_presenter.py:1-140`

**Interfaces:**
- Produces: `build_tool_display(name: str, arguments: dict, result: dict | None = None, ok: bool | None = None) -> dict[str, object]`
- Produces payload keys: `category`, `action`, `target`, `command`, `metrics`, `error_summary`, `is_skill`
- Consumed by: Task 2 `_tool_finished_event_payload()` and Task 3 `ToolDisplayEvent.from_payload()`

- [ ] **Step 1: Write failing metadata tests**

Add these tests to `tests/test_tool_presenter.py`:

```python
from core.tool_presenter import build_tool_display


def test_build_tool_display_returns_explicit_read_metadata():
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


def test_build_tool_display_keeps_raw_command_separate_from_summary():
    command = "venv\\Scripts\\python -m pytest -q"
    display = build_tool_display(
        "execute_command",
        {"command": command},
        {"content": "执行成功，但命令没有返回任何输出内容。"},
        ok=True,
    )

    assert display["category"] == "command"
    assert display["action"] == "运行命令"
    assert display["command"] == command
    assert display["error_summary"] == ""


def test_build_tool_display_exposes_one_stable_failure_summary():
    display = build_tool_display(
        "execute_command",
        {"command": "pytest -q"},
        {"content": "🚨 命令执行失败（退出码 1）。\nModuleNotFoundError: pytest"},
        ok=False,
    )

    assert display["category"] == "command"
    assert display["error_summary"] == (
        "命令执行失败（退出码 1）。 ModuleNotFoundError: pytest"
    )


def test_skill_read_is_not_grouped_with_source_reads():
    display = build_tool_display(
        "read_file",
        {"file_path": r"C:\Users\user\.agents\skills\writing-plans\SKILL.md"},
        {"content": "skill body"},
        ok=True,
    )

    assert display["category"] == "skill"
    assert display["target"] == "writing-plans"
    assert display["is_skill"] is True
```

- [ ] **Step 2: Run the tests and verify the missing interface**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_tool_presenter.py
```

Expected: FAIL during collection because `build_tool_display` does not exist.

- [ ] **Step 3: Add explicit tool categories and metadata construction**

Add to `core/tool_presenter.py`:

```python
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


def build_tool_display(
    name: str,
    arguments: dict,
    result: dict | None = None,
    ok: bool | None = None,
) -> dict[str, Any]:
    safe_arguments = arguments if isinstance(arguments, dict) else {}
    target = tool_target(name, safe_arguments)
    skill_name = ""
    if name == "read_file":
        skill_name = _skill_name_from_path(
            str(safe_arguments.get("file_path", ""))
        )

    metrics: dict[str, int] = {}
    if name == "read_file" and result is not None and ok is True:
        content = str(result.get("content", "") or "")
        metrics["line_count"] = len(content.splitlines()) if content else 0

    error_summary = ""
    if result is not None and ok is False:
        content = str(result.get("content", "") or "").strip()
        error_summary = " ".join(content.lstrip("🚨").split())[:160]

    return {
        "category": "skill" if skill_name else _TOOL_CATEGORIES.get(name, "other"),
        "action": "使用技能" if skill_name else _TOOL_ACTIONS.get(name, f"使用工具 {name}"),
        "target": skill_name or target,
        "command": str(safe_arguments.get("command", "")).strip()
        if name == "execute_command"
        else "",
        "metrics": metrics,
        "error_summary": error_summary,
        "is_skill": bool(skill_name),
    }
```

Keep `summarize_tool_started()` and `summarize_tool_finished()` backward compatible because GUI and existing tests still consume them.

- [ ] **Step 4: Run presenter tests**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_tool_presenter.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```powershell
git add core\tool_presenter.py tests\test_tool_presenter.py
git commit -m "feat: add structured tool display metadata"
```

---

### Task 2: Duration And Display Fields In Tool Events

**Files:**
- Modify: `core/llm.py:165, 1550-1640, 3297-3373`
- Modify: `tests/test_llm_events.py:1-375`

**Interfaces:**
- Consumes: Task 1 `build_tool_display()`
- Produces: `_tool_event_payload(tc: dict) -> dict` with optional structured `display` metadata for `tool_requested` and `tool_started`
- Produces: `_tool_finished_event_payload(tc: dict, result: dict, duration_ms: int) -> dict`
- Produces optional `tool_finished.payload["display"]` and `tool_finished.payload["duration_ms"]`
- Preserves existing `id`, `name`, `preview`, `arguments`, `summary`, `ok`, and `result`

- [ ] **Step 1: Write failing event-payload tests**

Extend the import in `tests/test_llm_events.py` and add:

```python
from core.llm import _tool_event_payload, _tool_finished_event_payload


def test_tool_started_payload_contains_display_metadata():
    payload = _tool_event_payload({
        "id": "cmd-start",
        "function": {
            "name": "execute_command",
            "arguments": '{"command":"pytest -q"}',
        },
    })

    assert payload["display"]["category"] == "command"
    assert payload["display"]["action"] == "运行命令"
    assert payload["display"]["command"] == "pytest -q"


def test_tool_finished_payload_contains_duration_and_display_metadata():
    tc = {
        "id": "read-1",
        "function": {
            "name": "read_file",
            "arguments": '{"file_path":"core/llm.py"}',
        },
    }
    payload = _tool_finished_event_payload(
        tc,
        {
            "role": "tool",
            "tool_call_id": "read-1",
            "name": "read_file",
            "content": "one\ntwo",
        },
        duration_ms=125,
    )

    assert payload["duration_ms"] == 125
    assert payload["display"]["category"] == "read"
    assert payload["display"]["metrics"] == {"line_count": 2}
    assert payload["ok"] is True


def test_failed_payload_stays_a_tool_result_not_a_turn_error():
    tc = {
        "id": "cmd-1",
        "function": {
            "name": "execute_command",
            "arguments": '{"command":"pytest -q"}',
        },
    }
    payload = _tool_finished_event_payload(
        tc,
        {
            "role": "tool",
            "tool_call_id": "cmd-1",
            "name": "execute_command",
            "content": "🚨 命令执行失败（退出码 1）。",
        },
        duration_ms=3200,
    )

    assert payload["ok"] is False
    assert payload["display"]["error_summary"] == "命令执行失败（退出码 1）。"
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_llm_events.py::test_tool_started_payload_contains_display_metadata tests\test_llm_events.py::test_tool_finished_payload_contains_duration_and_display_metadata tests\test_llm_events.py::test_failed_payload_stays_a_tool_result_not_a_turn_error
```

Expected: FAIL because started events have no `display` field and `_tool_finished_event_payload` does not exist.

- [ ] **Step 3: Centralize finished-event construction**

Import Task 1’s interface. In the existing `_tool_event_payload()`, add structured metadata to the returned mapping so the active UI has a summary before execution finishes:

```python
from core.tool_presenter import (
    build_tool_display,
    summarize_tool_finished,
    summarize_tool_started,
)


def _tool_event_payload(tc):
    # Preserve the existing preview-selection body unchanged.
    # Add this field to the existing return mapping:
    return {
        "id": tc.get("id", ""),
        "name": function_name,
        "preview": str(preview)[:120],
        "arguments": arguments,
        "summary": summarize_tool_started(function_name, arguments),
        "display": build_tool_display(function_name, arguments),
    }


# Add this helper next to _tool_output_preview().


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
        "display": build_tool_display(
            function_name,
            arguments,
            result,
            ok=ok,
        ),
    }
```

- [ ] **Step 4: Measure each tool inside its actual execution context**

Inside `_chat_with_model_generator()`, next to `execute_in_context()`, add:

```python
def execute_timed(tool_call):
    started_at = time.monotonic()
    result = execute_in_context(tool_call)
    duration_ms = round((time.monotonic() - started_at) * 1000)
    return result, max(0, duration_ms)
```

For parallel batches, replace `executor.map(execute_in_context, batch)` with `executor.map(execute_timed, batch)`, unzip the returned `(result, duration_ms)` tuples, extend `messages` with only the result objects, and emit `_tool_finished_event_payload(tc, result, duration_ms)` in original tool-call order. For serial batches, call `execute_timed(tc)` and use the same helper. Do not catch `KeyboardInterrupt` or convert internal exceptions here.

- [ ] **Step 5: Run LLM event tests**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_llm_events.py
```

Expected: PASS, including existing parallel-order and failure-detection tests.

- [ ] **Step 6: Commit Task 2**

```powershell
git add core\llm.py tests\test_llm_events.py
git commit -m "feat: enrich tool completion events"
```

---

### Task 3: Pure Tool Progress Aggregator

**Files:**
- Create: `core/tool_progress.py`
- Create: `tests/test_tool_progress.py`

**Interfaces:**
- Consumes: optional event payload fields from Task 2
- Produces: `ToolDisplayEvent.from_payload(payload: dict) -> ToolDisplayEvent`
- Produces: `ActiveToolStatus(label: str, detail: str, count: int, started_at: float)`
- Produces: `ToolProgressAggregator.start(payload)`, `finish(payload) -> list[str]`, `record_diff(file_path, added, removed) -> list[str]`, `flush() -> list[str]`, `active_status() -> ActiveToolStatus | None`
- Consumed by: Task 4 `TurnRenderer` and Task 5 `main.py`

- [ ] **Step 1: Write failing aggregation tests**

Create `tests/test_tool_progress.py`:

```python
from rich.text import Text

from core.tool_progress import ToolProgressAggregator, clean_terminal_text


def plain(lines):
    return Text.from_ansi("".join(lines)).plain


def read_payload(tool_id, path, duration_ms=100):
    return {
        "id": tool_id,
        "name": "read_file",
        "ok": True,
        "duration_ms": duration_ms,
        "display": {
            "category": "read",
            "action": "读取文件",
            "target": path,
            "command": "",
            "metrics": {"line_count": 10},
            "error_summary": "",
            "is_skill": False,
        },
    }


def test_consecutive_reads_flush_as_one_group():
    progress = ToolProgressAggregator(clock=lambda: 10.0)
    for index in range(3):
        payload = read_payload(str(index), f"file-{index}.py")
        progress.start(payload)
        assert progress.finish(payload) == []

    output = plain(progress.flush())
    assert output.count("已读取") == 1
    assert "已读取 3 个文件" in output
    assert "30 行" in output
    assert "0.3s" in output


def test_category_change_flushes_previous_group():
    progress = ToolProgressAggregator(clock=lambda: 10.0)
    read = read_payload("read", "a.py")
    search = {
        "id": "search",
        "name": "search_code",
        "ok": True,
        "duration_ms": 250,
        "display": {
            "category": "search",
            "action": "搜索代码",
            "target": "function_code",
            "command": "",
            "metrics": {},
            "error_summary": "",
            "is_skill": False,
        },
    }

    progress.start(read)
    assert progress.finish(read) == []
    progress.start(search)
    output = plain(progress.finish(search))

    assert "已读取 1 个文件" in output
    assert "已完成 1 次搜索" not in output
    assert "已完成 1 次搜索" in plain(progress.flush())


def test_command_failure_is_visible_and_next_tool_can_start():
    ticks = iter([1.0, 2.0, 3.0])
    progress = ToolProgressAggregator(clock=lambda: next(ticks))
    failed = {
        "id": "cmd-1",
        "name": "execute_command",
        "ok": False,
        "duration_ms": 3200,
        "display": {
            "category": "command",
            "action": "运行命令",
            "target": "pytest -q",
            "command": "pytest -q",
            "metrics": {},
            "error_summary": "ModuleNotFoundError: pytest",
            "is_skill": False,
        },
    }
    retry = {
        **failed,
        "id": "cmd-2",
        "ok": True,
        "duration_ms": 100,
        "display": {**failed["display"], "error_summary": ""},
    }

    progress.start(failed)
    output = plain(progress.finish(failed))
    assert "运行命令失败" in output
    assert "ModuleNotFoundError: pytest" in output
    assert "pytest -q" in output

    progress.start(retry)
    assert progress.active_status().label == "正在运行命令"
    assert "命令执行完成" in plain(progress.finish(retry))


def test_parallel_reads_have_one_active_status():
    progress = ToolProgressAggregator(clock=lambda: 5.0)
    for index in range(4):
        progress.start(read_payload(str(index), f"file-{index}.py"))

    status = progress.active_status()
    assert status is not None
    assert status.label == "正在并行读取 4 个文件"
    assert status.count == 4


def test_missing_event_duration_uses_injected_monotonic_clock():
    ticks = iter([10.0, 10.251])
    progress = ToolProgressAggregator(clock=lambda: next(ticks))
    payload = read_payload("read", "a.py")
    del payload["duration_ms"]

    progress.start(payload)
    progress.finish(payload)

    assert "0.3s" in plain(progress.flush())


def test_diff_line_suppresses_duplicate_mutation_completion():
    progress = ToolProgressAggregator(clock=lambda: 5.0)
    diff = plain(progress.record_diff("core/turn_renderer.py", 28, 14))
    mutation = {
        "id": "edit-1",
        "name": "edit_file",
        "ok": True,
        "duration_ms": 400,
        "display": {
            "category": "mutation",
            "action": "修改文件",
            "target": "core/turn_renderer.py",
            "command": "",
            "metrics": {},
            "error_summary": "",
            "is_skill": False,
        },
    }
    progress.start(mutation)

    assert "已修改 core/turn_renderer.py" in diff
    assert "+28 / -14 行" in diff
    assert progress.finish(mutation) == []


def test_terminal_text_removes_controls_truncates_and_redacts_secrets():
    value = (
        '\x1b[31mAuthorization: Bearer abc.def.secret\x1b[0m\n'
        'api_key="sk-1234567890abcdef"'
    )

    cleaned = clean_terminal_text(value, limit=80)

    assert "abc.def.secret" not in cleaned
    assert "sk-1234567890abcdef" not in cleaned
    assert "Authorization: ***" in cleaned
    assert 'api_key=***' in cleaned
    assert "\x1b" not in cleaned
```

- [ ] **Step 2: Run the new test module and verify failure**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_tool_progress.py
```

Expected: FAIL because `core.tool_progress` does not exist.

- [ ] **Step 3: Create immutable display and active-status types**

Create `core/tool_progress.py` with these public types and deterministic helpers:

```python
from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Callable

from core.ui_markers import ANSI_CYAN, ANSI_GREEN, ANSI_RED, ANSI_RESET


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_NAMED_SECRET_RE = re.compile(
    r'(?i)(["\']?(?:api[_-]?key|authorization|token|password)["\']?\s*[:=]\s*)'
    r'(Bearer\s+[A-Za-z0-9._~+/=-]+|"[^"]*"|\'[^\']*\'|[^\s,;]+)'
)
_BEARER_SECRET_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def redact_sensitive_text(value: object) -> str:
    text = str(value or "")
    text = _NAMED_SECRET_RE.sub(lambda match: f"{match.group(1)}***", text)
    text = _BEARER_SECRET_RE.sub(lambda match: f"{match.group(1)}***", text)
    return _OPENAI_KEY_RE.sub("***", text)


def clean_terminal_text(value: object, limit: int = 160) -> str:
    text = _ANSI_RE.sub("", redact_sensitive_text(value))
    text = _CONTROL_RE.sub("", text.replace("\r", " ").replace("\n", " "))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def format_duration(duration_ms: int) -> str:
    seconds = max(0, int(duration_ms)) / 1000
    return f"{seconds:.1f}s"


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

    @classmethod
    def from_payload(cls, payload: dict) -> "ToolDisplayEvent":
        display = dict(payload.get("display") or {})
        return cls(
            tool_id=str(payload.get("id") or payload.get("name") or "tool"),
            name=str(payload.get("name") or "tool"),
            category=str(display.get("category") or "other"),
            action=clean_terminal_text(display.get("action") or payload.get("name") or "使用工具"),
            target=clean_terminal_text(display.get("target") or payload.get("preview") or ""),
            command=clean_terminal_text(display.get("command") or "", limit=240),
            metrics={
                str(key): int(value)
                for key, value in dict(display.get("metrics") or {}).items()
                if isinstance(value, int) and value >= 0
            },
            error_summary=clean_terminal_text(display.get("error_summary") or ""),
            is_skill=bool(display.get("is_skill")),
            ok=payload.get("ok") if isinstance(payload.get("ok"), bool) else None,
            duration_ms=(
                max(0, int(payload["duration_ms"]))
                if isinstance(payload.get("duration_ms"), (int, float))
                else None
            ),
        )


@dataclass(frozen=True)
class ActiveToolStatus:
    label: str
    detail: str
    count: int
    started_at: float
```

- [ ] **Step 4: Implement grouping, failure, diff, and active-state transitions**

In the same module, implement `ToolProgressAggregator` with an `OrderedDict[str, tuple[ToolDisplayEvent, float]]` for active tools and one pending group. The exact state API is:

```python
@dataclass
class _PendingGroup:
    category: str
    count: int = 0
    duration_ms: int = 0
    metrics: dict[str, int] = field(default_factory=dict)


class ToolProgressAggregator:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._active = OrderedDict()
        self._pending: _PendingGroup | None = None
        self._diff_targets: set[str] = set()

    def start(self, payload: dict) -> None:
        event = ToolDisplayEvent.from_payload(payload)
        self._active[event.tool_id] = (event, self._clock())

    def active_status(self) -> ActiveToolStatus | None:
        if not self._active:
            return None
        active = list(self._active.values())
        events = [item[0] for item in active]
        started_at = min(item[1] for item in active)
        categories = {event.category for event in events}
        if len(events) > 1 and categories == {"read"}:
            return ActiveToolStatus(
                f"正在并行读取 {len(events)} 个文件", "", len(events), started_at
            )
        if len(events) > 1 and categories == {"search"}:
            return ActiveToolStatus(
                f"正在并行搜索 {len(events)} 项", "", len(events), started_at
            )
        event = events[-1]
        detail = event.command or event.target
        return ActiveToolStatus(
            f"正在{event.action}", detail, len(events), started_at
        )

    def finish(self, payload: dict) -> list[str]:
        event = ToolDisplayEvent.from_payload(payload)
        active = self._active.pop(event.tool_id, None)
        if event.duration_ms is None:
            started_at = active[1] if active is not None else self._clock()
            event = replace(
                event,
                duration_ms=max(0, round((self._clock() - started_at) * 1000)),
            )
        if event.ok is False:
            lines = self.flush()
            lines.extend(self._failure_lines(event))
            return lines
        if event.is_skill:
            lines = self.flush()
            lines.append(
                f"{ANSI_CYAN}✦{ANSI_RESET} 使用技能：{event.target}\n"
            )
            return lines
        if event.category in {"read", "search"}:
            lines = []
            if self._pending is not None and self._pending.category != event.category:
                lines.extend(self.flush())
            if self._pending is None:
                self._pending = _PendingGroup(event.category)
            self._pending.count += 1
            self._pending.duration_ms += event.duration_ms or 0
            for key, value in event.metrics.items():
                self._pending.metrics[key] = self._pending.metrics.get(key, 0) + value
            return lines
        lines = self.flush()
        if event.category == "mutation" and event.target in self._diff_targets:
            self._diff_targets.remove(event.target)
            return lines
        lines.extend(self._success_lines(event))
        return lines

    def record_diff(self, file_path: str, added: int, removed: int) -> list[str]:
        target = clean_terminal_text(file_path)
        self._diff_targets.add(target)
        lines = self.flush()
        lines.append(
            f"{ANSI_GREEN}✓{ANSI_RESET} 已修改 {ANSI_CYAN}{target}{ANSI_RESET}"
            f" · {ANSI_GREEN}+{added}{ANSI_RESET} / {ANSI_RED}-{removed}{ANSI_RESET} 行\n"
        )
        return lines

    def flush(self) -> list[str]:
        group = self._pending
        self._pending = None
        if group is None:
            return []
        duration = format_duration(group.duration_ms)
        if group.category == "read":
            count_copy = "个文件"
            metric = group.metrics.get("line_count", 0)
            suffix = f" · {metric} 行" if metric else ""
            text = f"已读取 {group.count} {count_copy}{suffix} · {duration}"
        else:
            text = f"已完成 {group.count} 次搜索 · {duration}"
        return [f"{ANSI_GREEN}✓{ANSI_RESET} {text}\n"]

    def _success_lines(self, event: ToolDisplayEvent) -> list[str]:
        summary = clean_terminal_text(
            f"命令执行完成" if event.category == "command" else f"已{event.action}"
        )
        target = f" {event.target}" if event.target and event.category != "command" else ""
        lines = [
            f"{ANSI_GREEN}✓{ANSI_RESET} {summary}{target} · {format_duration(event.duration_ms or 0)}\n"
        ]
        if event.command:
            lines.append(f"  └─ {event.command}\n")
        return lines

    def _failure_lines(self, event: ToolDisplayEvent) -> list[str]:
        lines = [
            f"{ANSI_RED}✗{ANSI_RESET} {event.action}失败 · {format_duration(event.duration_ms or 0)}\n"
        ]
        if event.command:
            lines.append(f"  ├─ {event.command}\n")
        if event.error_summary:
            lines.append(f"  └─ {event.error_summary}\n")
        elif event.target and not event.command:
            lines.append(f"  └─ {event.target}\n")
        return lines
```

- [ ] **Step 5: Run aggregator tests**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_tool_progress.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```powershell
git add core\tool_progress.py tests\test_tool_progress.py
git commit -m "feat: aggregate terminal tool progress"
```

---

### Task 4: Integrate Progress With TurnRenderer

**Files:**
- Modify: `core/turn_renderer.py:1-410`
- Modify: `tests/test_turn_renderer.py:1-340`

**Interfaces:**
- Consumes: Task 3 `ToolProgressAggregator`
- Produces: `TurnRenderer.active_tool_status() -> ActiveToolStatus | None`
- Produces: `TurnRenderer.flush_tool_progress() -> list[str]` so TUI tool lines stay outside the Markdown answer buffer
- Produces tool fragments during `tool_finished`, `diff_ready`, and boundary events
- Preserves `assistant_text()`, protocol sanitization, plan rendering, and Markdown buffering

- [ ] **Step 1: Replace silent-mode tests with failing B+C expectations**

Update the obsolete concise-mode cases in `tests/test_turn_renderer.py` and add:

```python
def test_renderer_groups_reads_before_assistant_text():
    renderer = TurnRenderer()
    for index in range(3):
        payload = {
            "id": str(index),
            "name": "read_file",
            "ok": True,
            "duration_ms": 100,
            "display": {
                "category": "read",
                "action": "读取文件",
                "target": f"file-{index}.py",
                "command": "",
                "metrics": {"line_count": 10},
                "error_summary": "",
                "is_skill": False,
            },
        }
        renderer.consume("tool_started", payload)
        renderer.consume("tool_finished", payload)

    fragments = "".join(renderer.consume("assistant_delta", {"text": "结论"}))
    output = Text.from_ansi(renderer.finish()).plain

    assert fragments.count("已读取") == 1
    assert "已读取 3 个文件" in output
    assert "共执行" not in output


def test_renderer_flushes_group_before_thinking_boundary():
    renderer = TurnRenderer()
    payload = {
        "id": "read",
        "name": "read_file",
        "ok": True,
        "duration_ms": 100,
        "display": {
            "category": "read",
            "action": "读取文件",
            "target": "a.py",
            "command": "",
            "metrics": {"line_count": 2},
            "error_summary": "",
            "is_skill": False,
        },
    }
    renderer.consume("tool_started", payload)
    renderer.consume("tool_finished", payload)

    boundary = Text.from_ansi(
        "".join(renderer.consume("thinking_started", {}))
    ).plain
    assert "已读取 1 个文件" in boundary


def test_renderer_exposes_active_command_status_and_failure():
    renderer = TurnRenderer()
    payload = {
        "id": "cmd",
        "name": "execute_command",
        "ok": False,
        "duration_ms": 3200,
        "display": {
            "category": "command",
            "action": "运行命令",
            "target": "pytest -q",
            "command": "pytest -q",
            "metrics": {},
            "error_summary": "ModuleNotFoundError: pytest",
            "is_skill": False,
        },
    }

    renderer.consume("tool_started", payload)
    status = renderer.active_tool_status()
    failed = Text.from_ansi(
        "".join(renderer.consume("tool_finished", payload))
    ).plain

    assert status is not None
    assert status.label == "正在运行命令"
    assert status.detail == "pytest -q"
    assert "运行命令失败" in failed
    assert renderer.active_tool_status() is None
```

Delete assertions that require all successful and failed tools to remain silent, the one-time “星瑶正在干活” header, and `concise_recap()` failure suppression.

- [ ] **Step 2: Run renderer tests and verify old behavior fails**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_turn_renderer.py
```

Expected: FAIL because tool events are still silent and `active_tool_status()` does not exist.

- [ ] **Step 3: Delegate tool event handling to the aggregator**

In `TurnRenderer.__init__()` add `self._tool_progress = ToolProgressAggregator()` and remove `_tool_lines`, `_completed_tools`, `_failed_tool_count`, `_concise_header_emitted`, and `_recap_emitted`.

Change `consume()` branches to:

```python
if kind == "thinking_started":
    self._append_tool_lines(self._tool_progress.flush())
elif kind == "thinking_delta":
    text = str(data.get("text", ""))
    for _ in self._thinking_stream.feed(text):
        pass
elif kind == "thinking_finished":
    self._flush_thinking()
elif kind == "assistant_delta":
    self._append_tool_lines(self._tool_progress.flush())
    self._consume_text(str(data.get("text", "")))
elif kind == "tool_started":
    self._tool_progress.start(data)
elif kind == "tool_finished":
    self._append_tool_lines(self._tool_progress.finish(data))
elif kind == "diff_ready":
    diff = str(data.get("text") or data.get("diff") or "")
    if diff:
        file_path = str(data.get("file_path") or "").strip()
        added, removed = _count_diff_changes(diff)
        self._append_tool_lines(
            self._tool_progress.record_diff(file_path, added, removed)
        )
```

Add:

```python
def _append_tool_lines(self, lines: list[str]) -> None:
    if lines:
        self._append_section_gap()
    for line in lines:
        self._append_line(line)

def active_tool_status(self):
    return self._tool_progress.active_status()

def flush_tool_progress(self) -> list[str]:
    self._emitted = []
    self._append_tool_lines(self._tool_progress.flush())
    return list(self._emitted)
```

Before local empty-answer fallback in `_flush_pending()`, flush pending tool groups. Remove `concise_recap()` and all old `_tool_requested()`, `_tool_finished()`, `_tool_label()`, and skill-special-case paths now represented by `category="skill"`.

- [ ] **Step 4: Run renderer and presenter tests**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_tool_progress.py tests\test_turn_renderer.py tests\test_tool_presenter.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```powershell
git add core\turn_renderer.py tests\test_turn_renderer.py
git commit -m "feat: render grouped tool progress"
```

---

### Task 5: Live TUI Activity And Thinking Cleanup

**Files:**
- Modify: `main.py:121-140, 558-580, 658-686, 796-1015, 2538-2547`
- Modify: `tests/test_main_ui_events.py:250-487, 582-625`

**Interfaces:**
- Consumes: Task 4 `TurnRenderer.active_tool_status()`
- Produces: `_format_activity_text(status: str, active_status: ActiveToolStatus | None, elapsed: float, frame: str) -> str`
- Preserves: existing UI event queue, prompt_toolkit thread scheduling, Ctrl+C cancellation, temporary reasoning panel

- [ ] **Step 1: Write failing UI tests for the accepted behavior**

Replace `test_each_thinking_segment_prints_its_own_divider_line` with:

```python
def test_thinking_segments_never_write_completion_dividers(monkeypatch):
    factory = TurnEventFactory("turn-thinking-no-history")
    fragments = []
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", fragments.append)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-thinking-no-history", None)
    main.active_thinking_text = ""
    main.active_thinking_turn_id = None

    for index in range(3):
        main._render_ui_event(factory.emit("thinking_started"))
        main._render_ui_event(
            factory.emit("thinking_delta", {"text": f"分析{index}"})
        )
        main.think_start_time -= 1.0
        main._render_ui_event(factory.emit("thinking_finished"))

    assert "思考完成" not in Text.from_ansi("".join(fragments)).plain
```

Add a pure formatter test:

```python
from core.tool_progress import ActiveToolStatus


def test_activity_text_shows_summary_command_and_interrupt_hint():
    status = ActiveToolStatus(
        label="正在运行测试",
        detail=r"venv\Scripts\python -m pytest -q",
        count=1,
        started_at=10.0,
    )

    text = main._format_activity_text("executing", status, 8.4, "⠹")

    assert text == (
        "  ⠹ 正在运行测试 · 8.4s · Ctrl+C 中断\n"
        "    └─ venv\\Scripts\\python -m pytest -q"
    )
```

Add the event-level continuation test:

```python
def test_tool_failure_continues_with_next_active_tool(monkeypatch):
    factory = TurnEventFactory("turn-failure-continues")
    fragments = []
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", fragments.append)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-failure-continues", None)
    main._active_tool_status = None
    main.task_status = "thinking"

    failed = {
        "id": "cmd-fail",
        "name": "execute_command",
        "duration_ms": 3200,
        "display": {
            "category": "command",
            "action": "运行命令",
            "target": "pytest -q",
            "command": "pytest -q",
            "metrics": {},
            "error_summary": "ModuleNotFoundError: pytest",
            "is_skill": False,
        },
    }
    main._render_ui_event(factory.emit("tool_started", failed))
    assert main._active_tool_status.detail == "pytest -q"

    main._render_ui_event(factory.emit("tool_finished", {**failed, "ok": False}))
    assert "运行命令失败" in Text.from_ansi("".join(fragments)).plain
    assert main.task_status == "executing"

    corrected = {
        **failed,
        "id": "cmd-pass",
        "display": {
            **failed["display"],
            "command": "python -m pytest -q",
            "target": "python -m pytest -q",
            "error_summary": "",
        },
    }
    main._render_ui_event(factory.emit("tool_started", corrected))

    assert main._active_tool_status.detail == "python -m pytest -q"
    assert main.task_status == "executing"


def test_tool_group_stays_outside_markdown_answer_buffer(monkeypatch):
    turn_id = "turn-tool-before-answer"
    factory = TurnEventFactory(turn_id)
    fragments = []
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", fragments.append)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop(turn_id, None)
    main._assistant_stream_buffers.pop(turn_id, None)

    read = {
        "id": "read-1",
        "name": "read_file",
        "duration_ms": 100,
        "display": {
            "category": "read",
            "action": "读取文件",
            "target": "a.py",
            "command": "",
            "metrics": {"line_count": 2},
            "error_summary": "",
            "is_skill": False,
        },
    }
    main._render_ui_event(factory.emit("tool_started", read))
    main._render_ui_event(factory.emit("tool_finished", {**read, "ok": True}))
    main._render_ui_event(factory.emit("assistant_delta", {"text": "结论"}))

    assert "已读取 1 个文件" in Text.from_ansi("".join(fragments)).plain
    assert main._assistant_stream_buffers[turn_id] == "结论"
```

- [ ] **Step 2: Run focused UI tests and verify failure**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_main_ui_events.py -k "thinking_segments or activity_text or failure_continues"
```

Expected: FAIL because thinking dividers are still printed and no structured activity formatter exists.

- [ ] **Step 3: Add one structured active-status global and pure formatter**

Replace `_current_tool_name` with `_active_tool_status = None`. Add:

```python
def _format_activity_text(status, active_status, elapsed, frame):
    if active_status is not None:
        first = (
            f"  {frame} {active_status.label} · {elapsed:.1f}s"
            " · Ctrl+C 中断"
        )
        if active_status.detail:
            return f"{first}\n    └─ {active_status.detail}"
        return first
    return f"  {frame} {_turn_activity_label(status)} · {elapsed:.1f}s"
```

Clear `_active_tool_status` in `_reset_turn_state()`, `turn_finished`, `turn_cancelled`, and `turn_error`.

- [ ] **Step 4: Reorder event consumption without breaking UI-thread ownership**

For `thinking_started`, call `renderer.consume("thinking_started", payload)` and print returned tool-group fragments before returning. Immediately before the common `assistant_delta` consume path, call `renderer.flush_tool_progress()` and send those fragments directly through `_print_ui_fragment`; this prevents ANSI tool history from entering `_assistant_stream_buffers` and Markdown rendering. For `tool_started` and `tool_finished`, let the existing common `renderer.consume()` path run, then assign:

```python
if event.kind in {"tool_started", "tool_finished"}:
    _active_tool_status = renderer.active_tool_status()
    task_status = "executing"
    is_thinking = True
```

On `tool_started`, capture whether `_active_tool_status` was `None` before consuming the event. If this is the first active tool, set `activity_start_time = time.time()` after consumption; additional parallel starts do not reset it. `ActiveToolStatus.started_at` remains internal monotonic state and is not mixed with the wall-clock value used by `get_activity_text()`. Do not invoke terminal I/O from worker callbacks.

Delete the `thinking_finished` block that prints `思考完成 · 用时 ...`; retain temporary-panel state, `active_thinking_turn_id` reset, and `_thinking_panel_linger` behavior.

Change nested `get_activity_text()` to compute the spinner frame and call `_format_activity_text(task_status, _active_tool_status, elapsed, frame)`.

Remove `renderer.concise_recap()` calls from successful, cancelled, and failed turn endings because static tool lines now provide the audit trail.

- [ ] **Step 5: Run UI and renderer tests**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_main_ui_events.py tests\test_turn_renderer.py tests\test_tool_progress.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

```powershell
git add main.py tests\test_main_ui_events.py
git commit -m "feat: show live CodeBuddy-style tool status"
```

---

### Task 6: End-To-End Failure Continuation, Documentation, And Verification

**Files:**
- Modify: `tests/test_agent_turn_e2e.py:1-180`
- Modify: `README.md:180-205`
- Verify: `core/tool_progress.py`, `core/tool_presenter.py`, `core/llm.py`, `core/turn_renderer.py`, `main.py`

**Interfaces:**
- Consumes all Tasks 1-5
- Produces one deterministic acceptance test for grouped reads/searches, visible failure, continued execution, mutation output, and one final answer
- Produces user-facing documentation matching actual commands and default behavior

- [ ] **Step 1: Write the failing end-to-end test**

Add to `tests/test_agent_turn_e2e.py`:

```python
def test_tool_progress_groups_work_and_continues_after_failure(mocker):
    responses = [
        iter([_chunk(tool_calls=[
            _tool_call(0, "read-1", "read_file", {"file_path": "a.py"}),
            _tool_call(1, "read-2", "read_file", {"file_path": "b.py"}),
            _tool_call(2, "read-3", "read_file", {"file_path": "c.py"}),
        ])]),
        iter([_chunk(tool_calls=[
            _tool_call(0, "search-1", "search_code", {"query": "target"}),
            _tool_call(1, "search-2", "glob_files", {"pattern": "*.py"}),
        ])]),
        iter([_chunk(tool_calls=[
            _tool_call(0, "cmd-fail", "execute_command", {"command": "pytest -q"}),
        ])]),
        iter([_chunk(tool_calls=[
            _tool_call(0, "cmd-pass", "execute_command", {"command": "python -m pytest -q"}),
        ])]),
        iter([_chunk(tool_calls=[
            _tool_call(0, "edit-1", "edit_file", {
                "file_path": "a.py",
                "old_string": "old",
                "new_string": "new",
            }),
        ])]),
        iter([_chunk("检查结论：失败已修正，任务继续并完成。")]),
    ]
    mocker.patch("core.llm.completion", side_effect=responses)

    def fake_execute(tc, **kwargs):
        tool_id = tc["id"]
        name = tc["function"]["name"]
        content = {
            "read-1": "one\ntwo",
            "read-2": "one\ntwo",
            "read-3": "one\ntwo",
            "search-1": "a.py:1:target",
            "search-2": "a.py\nb.py",
            "cmd-fail": "🚨 命令执行失败（退出码 1）。\nModuleNotFoundError: pytest",
            "cmd-pass": "12 passed",
            "edit-1": "已修改文件 a.py。",
        }[tool_id]
        return {
            "role": "tool",
            "tool_call_id": tool_id,
            "name": name,
            "content": content,
        }

    mocker.patch("core.llm._execute_single_tool_call", side_effect=fake_execute)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "检查代码"}],
        persist=False,
        turn_id="turn-tool-progress-e2e",
    ))
    renderer = TurnRenderer()
    for event in events:
        renderer.consume(event.kind, dict(event.payload))
    output = renderer.finish()

    assert output.count("已读取") == 1
    assert "已读取 3 个文件" in output
    assert output.count("已完成 2 次搜索") == 1
    assert "运行命令失败" in output
    assert "ModuleNotFoundError: pytest" in output
    assert "命令执行完成" in output
    assert "已修改文件 a.py" in output
    assert "检查结论：失败已修正" in output
    assert [event.kind for event in events][-1] == "turn_finished"
```

- [ ] **Step 2: Run the acceptance test**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_agent_turn_e2e.py::test_tool_progress_groups_work_and_continues_after_failure
```

Expected before final integration: FAIL on missing grouped output or failure continuation display. After Tasks 1-5: PASS.

- [ ] **Step 3: Align README with the actual UI**

In `README.md`, remove the stale `/verbose <on|off>` row. Add a short “工具执行反馈” paragraph after the system-control table stating:

```markdown
工具执行反馈默认采用精简进度模式：连续读取和搜索会合并显示；命令、文件修改和失败操作单独留痕；运行中的命令会显示摘要、截断后的原始命令、耗时和 Ctrl+C 中断提示。单个工具失败会返回给模型继续分析，不会被静默隐藏。
```

- [ ] **Step 4: Run focused regression suites**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_tool_presenter.py tests\test_tool_progress.py tests\test_turn_renderer.py tests\test_main_ui_events.py tests\test_llm_events.py tests\test_agent_turn_e2e.py
```

Expected: PASS.

- [ ] **Step 5: Run the full test suite**

Run:

```powershell
venv\Scripts\python -m pytest -q
```

Expected: PASS with no failures.

- [ ] **Step 6: Run the project compile check**

Run:

```powershell
venv\Scripts\python -m py_compile main.py core\llm.py core\memory.py core\memory_compressor.py core\themes.py core\tool_presenter.py core\tool_progress.py core\turn_renderer.py
```

Expected: exit code 0 and no output.

- [ ] **Step 7: Run one real K3 smoke turn**

Start the source TUI with:

```powershell
venv\Scripts\python main.py
```

Ask it to inspect at least five files and run one harmless command that first fails, then can be corrected. Verify:

- no historical `思考完成` wall;
- live activity always names the current action;
- reads/searches are grouped;
- raw command is safely truncated and displayed under its summary;
- failure is visible and the next tool still runs;
- final answer appears once.

- [ ] **Step 8: Commit Task 6**

```powershell
git add README.md tests\test_agent_turn_e2e.py
git commit -m "test: verify continuous tool progress"
```

---

## Final Review Checklist

- [ ] `git diff --check` reports no whitespace errors.
- [ ] `git status --short` contains no accidental `.superpowers/` staging and no unrelated user files staged.
- [ ] Each task commit touches only the files listed for that task.
- [ ] The final output contains no private reasoning, secrets, raw ANSI control input, or unbounded command/error text.
- [ ] An ordinary tool failure is visible and followed by a later tool event in the acceptance test.
- [ ] The final source run, not the older `D:\app\ycode\ycode.exe`, is used for smoke verification.
