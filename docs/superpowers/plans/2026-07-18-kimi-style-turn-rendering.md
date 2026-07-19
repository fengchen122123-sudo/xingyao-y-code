# Kimi-Style Turn Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Y-CODE 每轮对话渲染为 Kimi Code 风格的灰色思考、星瑶中文工具轨迹和规整终端 Markdown 正文，同时保持启动首屏和输入区域不变。

**Architecture:** 在模型事件入口使用有状态解析器把思考与公开回答拆成互斥事件；工具展示和 Markdown 展示分别放入独立模块；实时输出与历史回放共同复用这些纯展示函数。prompt_toolkit 仍只负责输入区，所有对话历史继续通过 `run_in_terminal()` 写入 Windows Terminal 原生滚动历史。

**Tech Stack:** Python 3.10+、prompt_toolkit 3.0.52、Rich Markdown/Table、LiteLLM、SQLite、pytest、pytest-mock。

---

## File Map

- Create `core/reasoning_stream.py`: 跨流分片解析 `<think>` / `<thinking>`，输出思考或公开回答片段。
- Create `core/tool_presenter.py`: 集中维护工具中文文案、目标参数和结果摘要。
- Create `core/terminal_markdown.py`: 使用 Rich 结构化渲染 Markdown、代码块和表格为 ANSI。
- Create `core/history_renderer.py`: 将数据库消息重放为与实时输出一致的 ANSI 历史。
- Modify `core/llm.py`: 产生 `thinking_delta`，保存独立思考字段，构造稳定工具结果元数据。
- Modify `core/turn_renderer.py`: 分别累计思考、工具轨迹和公开回答，不再解析混合标签。
- Modify `core/memory.py`: 规范化结构化助理消息供模型和历史回放使用。
- Modify `main.py`: 调用 Markdown 渲染器和历史渲染器，保持 `run_in_terminal()` 输出边界。
- Modify `system_prompt.md`: 允许终端支持的标准 Markdown，不要求伪造思考。
- Modify `%USERPROFILE%/.ycode/system_prompt.md`: 同步活动提示词。
- Modify `setup.py`: 版本升级到 `1.0.9`。
- Create `tests/test_reasoning_stream.py`: 流式思考解析测试。
- Create `tests/test_tool_presenter.py`: 工具中文文案与源码不泄漏测试。
- Create `tests/test_terminal_markdown.py`: 标题、列表、代码和表格渲染测试。
- Create `tests/test_history_renderer.py`: 结构化消息和旧消息回放测试。
- Modify `tests/test_llm_events.py`: 事件分流和持久化测试。
- Modify `tests/test_turn_renderer.py`: 灰色思考、工具轨迹和最终正文缓冲测试。
- Modify `tests/test_main_ui_events.py`: Markdown 输出和 `run_in_terminal()` 集成测试。

### Task 1: Parse Reasoning and Public Text at the Stream Boundary

**Files:**
- Create: `core/reasoning_stream.py`
- Create: `tests/test_reasoning_stream.py`

- [ ] **Step 1: Write failing parser tests**

```python
from core.reasoning_stream import ReasoningStreamParser, split_reasoning_text


def test_parser_separates_tags_split_across_chunks():
    parser = ReasoningStreamParser()
    parts = []
    parts.extend(parser.feed("开场 <thi"))
    parts.extend(parser.feed("nk>内部分析"))
    parts.extend(parser.feed("</thi"))
    parts.extend(parser.feed("nk>最终结论"))
    parts.extend(parser.finish())

    assert [(part.kind, part.text) for part in parts] == [
        ("assistant", "开场 "),
        ("thinking", "内部分析"),
        ("assistant", "最终结论"),
    ]


def test_unclosed_thinking_stays_reasoning_at_end_of_stream():
    reasoning, answer = split_reasoning_text("<thinking>仍在分析")
    assert reasoning == "仍在分析"
    assert answer == ""


def test_plain_text_is_public_answer():
    reasoning, answer = split_reasoning_text("直接回答")
    assert reasoning == ""
    assert answer == "直接回答"
```

- [ ] **Step 2: Run tests and confirm the module is missing**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_reasoning_stream.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'core.reasoning_stream'`.

- [ ] **Step 3: Implement the state parser**

Create `core/reasoning_stream.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


_OPEN_TAGS = ("<thinking>", "<think>")
_CLOSE_TAGS = ("</thinking>", "</think>")


@dataclass(frozen=True)
class StreamPart:
    kind: Literal["thinking", "assistant"]
    text: str


def _find_first(text: str, tags: tuple[str, ...]):
    matches = [(text.find(tag), tag) for tag in tags if text.find(tag) >= 0]
    return min(matches, default=None, key=lambda item: item[0])


def _safe_prefix_length(text: str, tags: tuple[str, ...]) -> int:
    max_length = max(len(tag) for tag in tags)
    start = max(0, len(text) - max_length + 1)
    for index in range(start, len(text)):
        suffix = text[index:]
        if any(tag.startswith(suffix) for tag in tags):
            return index
    return len(text)


class ReasoningStreamParser:
    def __init__(self):
        self._pending = ""
        self._kind: Literal["thinking", "assistant"] = "assistant"

    def feed(self, text: str) -> list[StreamPart]:
        self._pending += text
        output: list[StreamPart] = []
        while self._pending:
            tags = _CLOSE_TAGS if self._kind == "thinking" else _OPEN_TAGS
            match = _find_first(self._pending, tags)
            if match is not None:
                index, tag = match
                if index:
                    output.append(StreamPart(self._kind, self._pending[:index]))
                self._pending = self._pending[index + len(tag):]
                self._kind = "assistant" if self._kind == "thinking" else "thinking"
                continue
            safe_length = _safe_prefix_length(self._pending, tags)
            if safe_length:
                output.append(StreamPart(self._kind, self._pending[:safe_length]))
                self._pending = self._pending[safe_length:]
            break
        return output

    def finish(self) -> list[StreamPart]:
        if not self._pending:
            return []
        part = StreamPart(self._kind, self._pending)
        self._pending = ""
        return [part]


def split_reasoning_text(text: str) -> tuple[str, str]:
    parser = ReasoningStreamParser()
    parts = parser.feed(text) + parser.finish()
    reasoning = "".join(part.text for part in parts if part.kind == "thinking")
    answer = "".join(part.text for part in parts if part.kind == "assistant")
    return reasoning, answer
```

- [ ] **Step 4: Run parser tests**

Run: `venv\Scripts\python -m pytest -q tests\test_reasoning_stream.py`

Expected: `3 passed`.

- [ ] **Step 5: Commit the parser**

```powershell
git add core/reasoning_stream.py tests/test_reasoning_stream.py
git commit -m "feat: split reasoning from public output"
```

### Task 2: Emit `thinking_delta` and Persist Structured Assistant Messages

**Files:**
- Modify: `core/llm.py` in `chat_with_model_events()` and the assistant persistence block in `_chat_with_model_generator()`
- Modify: `core/memory.py` in `get_history_for_llm()`
- Modify: `tests/test_llm_events.py`
- Modify: `tests/test_memory.py`

- [ ] **Step 1: Add failing event and persistence tests**

Add to `tests/test_llm_events.py`:

```python
def test_event_stream_emits_reasoning_only_as_thinking_delta(mocker):
    mocker.patch("core.llm.completion", return_value=iter([
        _chunk(reasoning="分析过程"),
        _chunk("最终回答"),
    ]))

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "分析"}],
        persist=False,
        turn_id="turn-separated-reasoning",
    ))

    thinking = "".join(
        event.payload["text"] for event in events if event.kind == "thinking_delta"
    )
    answer = "".join(
        event.payload["text"] for event in events if event.kind == "assistant_delta"
    )
    assert thinking == "分析过程"
    assert answer == "最终回答"
    assert "分析过程" not in answer
    assert "<think>" not in answer


def test_literal_split_think_tags_become_reasoning_events(mocker):
    mocker.patch("core.llm.completion", return_value=iter([
        _chunk("<thi"),
        _chunk("nk>分析</think>结论"),
    ]))
    events = list(chat_with_model_events(
        "fake-model", "fake-key", [{"role": "user", "content": "检查"}],
        persist=False, turn_id="turn-tagged-reasoning",
    ))
    assert "".join(e.payload["text"] for e in events if e.kind == "thinking_delta") == "分析"
    assert "".join(e.payload["text"] for e in events if e.kind == "assistant_delta") == "结论"
```

Add to `tests/test_memory.py`:

```python
def test_structured_assistant_history_returns_public_content_to_llm(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory.db"))
    manager.add_message("session", "assistant", {
        "role": "assistant",
        "content": "最终回答",
        "reasoning_content": "分析过程",
    })
    history = manager.get_history_for_llm("session")
    assert history == [{"role": "assistant", "content": "最终回答"}]
```

- [ ] **Step 2: Run the new tests and verify separation fails**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_llm_events.py tests\test_memory.py
```

Expected: the new event test finds no `thinking_delta`, and the memory test returns nested structured content.

- [ ] **Step 3: Parse generator chunks into exclusive events**

In `core/llm.py`, import `ReasoningStreamParser` and replace the chunk forwarding inside `chat_with_model_events()` with this flow:

```python
    parser = ReasoningStreamParser()
    thinking_open = False

    def public_pending_events():
        nonlocal pending_events
        with pending_lock:
            ready = pending_events[:]
            del pending_events[:]
        return [
            event for event in ready
            if event.kind not in {"thinking_started", "thinking_finished"}
        ]

    try:
        for chunk in generator:
            yield from public_pending_events()
            for part in parser.feed(chunk):
                if part.kind == "thinking":
                    if not thinking_open:
                        yield factory.emit("thinking_started", {})
                        thinking_open = True
                    if part.text:
                        yield factory.emit("thinking_delta", {"text": part.text})
                else:
                    if thinking_open:
                        yield factory.emit("thinking_finished", {})
                        thinking_open = False
                    if part.text:
                        yield factory.emit("assistant_delta", {"text": part.text})

        yield from public_pending_events()
        for part in parser.finish():
            if part.kind == "thinking":
                if not thinking_open:
                    yield factory.emit("thinking_started", {})
                    thinking_open = True
                if part.text:
                    yield factory.emit("thinking_delta", {"text": part.text})
            elif part.text:
                if thinking_open:
                    yield factory.emit("thinking_finished", {})
                    thinking_open = False
                yield factory.emit("assistant_delta", {"text": part.text})
        if thinking_open:
            yield factory.emit("thinking_finished", {})
        yield factory.emit("turn_finished", {})
```

- [ ] **Step 4: Persist public content and reasoning separately**

In `_chat_with_model_generator()`, compute the complete fields before checking the final answer and before writing the database record:

```python
            reasoning_content, public_content = split_reasoning_text(accumulated_content)
            persisted_message = {
                "role": "assistant",
                "content": public_content or None,
                "reasoning_content": reasoning_content or None,
            }
            if tool_calls_list:
                persisted_message["tool_calls"] = tool_calls_list

            needs_final_summary = (
                not tool_calls_list
                and tool_activity_seen
                and not _is_meaningful_assistant_content(public_content)
            )
```

Keep `message_to_append` unchanged for the in-memory provider tool chain, but pass `persisted_message` to `manager.add_message()`.

In `core/memory.py`, normalize structured records in `get_history_for_llm()`:

```python
            try:
                parsed = json.loads(raw_content)
            except (ValueError, TypeError):
                history.append({"role": role, "content": raw_content})
                continue

            if isinstance(parsed, dict):
                normalized = {"role": role}
                normalized.update(parsed)
                normalized["role"] = role
                normalized.pop("reasoning_content", None)
                history.append(normalized)
            else:
                history.append({"role": role, "content": parsed})
```

- [ ] **Step 5: Run event and memory tests**

Run: `venv\Scripts\python -m pytest -q tests\test_llm_events.py tests\test_memory.py`

Expected: all tests pass.

- [ ] **Step 6: Commit event separation**

```powershell
git add core/llm.py core/memory.py tests/test_llm_events.py tests/test_memory.py
git commit -m "feat: emit structured thinking events"
```

### Task 3: Centralize Star-Yao Tool Presentation

**Files:**
- Create: `core/tool_presenter.py`
- Create: `tests/test_tool_presenter.py`
- Modify: `core/llm.py` in `_tool_event_payload()`, `_tool_output_preview()`, and tool completion event construction
- Modify: `core/turn_renderer.py` tool rendering methods
- Modify: `tests/test_turn_renderer.py`

- [ ] **Step 1: Write failing tool presentation tests**

```python
from core.tool_presenter import summarize_tool_finished, summarize_tool_started


def test_read_file_uses_star_yao_copy_without_source_content():
    started = summarize_tool_started("read_file", {"file_path": "core/llm.py"})
    finished = summarize_tool_finished(
        "read_file",
        {"file_path": "core/llm.py"},
        {"content": "line one\nline two\nline three"},
        ok=True,
    )
    assert started == "星瑶正在查看文件：core/llm.py"
    assert finished == "星瑶已查看文件：core/llm.py（3 行）"
    assert "line one" not in finished


def test_write_edit_and_command_copy_is_stable():
    assert summarize_tool_started("write_file", {"file_path": "a.py"}) == "星瑶正在写入文件：a.py"
    assert summarize_tool_started("edit_file", {"file_path": "a.py"}) == "星瑶正在修改文件：a.py"
    assert summarize_tool_started("execute_command", {"command": "pytest -q"}) == "星瑶正在执行命令：pytest -q"


def test_failed_tool_is_explicit():
    text = summarize_tool_finished(
        "execute_command", {"command": "pytest -q"}, {"content": "exit 1"}, ok=False
    )
    assert text == "命令执行失败：pytest -q"
```

- [ ] **Step 2: Run tests and confirm the presenter is missing**

Run: `venv\Scripts\python -m pytest -q tests\test_tool_presenter.py`

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement centralized mappings**

Create `core/tool_presenter.py`:

```python
from __future__ import annotations


_TOOL_COPY = {
    "read_file": ("查看文件", "已查看文件", "file_path"),
    "write_file": ("写入文件", "已写入文件", "file_path"),
    "edit_file": ("修改文件", "已修改文件", "file_path"),
    "execute_command": ("执行命令", "命令执行完成", "command"),
    "list_directory": ("查看目录", "已查看目录", "dir_path"),
    "glob_files": ("查找文件", "文件查找完成", "pattern"),
    "search_code": ("搜索代码", "代码搜索完成", "query"),
    "search_news": ("搜索资料", "资料搜索完成", "query"),
}


def _target(name: str, arguments: dict) -> str:
    key = _TOOL_COPY.get(name, ("使用工具", "工具执行完成", ""))[2]
    return str(arguments.get(key, "")).strip()


def summarize_tool_started(name: str, arguments: dict) -> str:
    action = _TOOL_COPY.get(name, (f"使用工具 {name}", "工具执行完成", ""))[0]
    target = _target(name, arguments)
    return f"星瑶正在{action}：{target}" if target else f"星瑶正在{action}"


def summarize_tool_finished(name: str, arguments: dict, result: dict, ok: bool) -> str:
    target = _target(name, arguments)
    if not ok:
        if name == "execute_command":
            return f"命令执行失败：{target}"
        return f"星瑶执行失败：{target}" if target else "星瑶执行工具失败"
    if name == "read_file":
        content = str(result.get("content", "") or "")
        line_count = len(content.splitlines()) if content else 0
        return f"星瑶已查看文件：{target}（{line_count} 行）"
    success = _TOOL_COPY.get(name, ("使用工具", "工具执行完成", ""))[1]
    if name == "execute_command" or not target:
        return success
    return f"星瑶{success}：{target}"
```

- [ ] **Step 4: Attach summaries to tool events and render only summaries**

In `core/llm.py`, add `arguments` and summary text to requested/finished event payloads. In `core/turn_renderer.py`, replace raw labels/previews with:

```python
    def _tool_requested(self, data):
        summary = str(data.get("summary") or data.get("name") or "工具")
        self._append_line(f"{_ANSI_CYAN}● {_ANSI_RESET}{summary}")

    def _tool_finished(self, data):
        ok = bool(data.get("ok", True))
        color = _ANSI_GREEN if ok else _ANSI_RED
        marker = "✓" if ok else "✗"
        summary = str(data.get("summary") or "工具执行完成")
        self._append_line(f"{color}{marker}{_ANSI_RESET} {summary}")
        self._completed_tool_count += 1
        self._completed_tools.append((str(data.get("name", "tool")), summary))
```

Delete terminal rendering of arbitrary `output_preview` lines. The full tool result remains available to the model and database, not to the terminal history.

- [ ] **Step 5: Run tool and renderer tests**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_tool_presenter.py tests\test_turn_renderer.py
```

Expected: all tests pass and the read-file test proves source content is absent.

- [ ] **Step 6: Commit tool presentation**

```powershell
git add core/tool_presenter.py core/llm.py core/turn_renderer.py tests/test_tool_presenter.py tests/test_turn_renderer.py
git commit -m "feat: render Chinese tool activity summaries"
```

### Task 4: Render Final Markdown and Tables with Rich

**Files:**
- Create: `core/terminal_markdown.py`
- Create: `tests/test_terminal_markdown.py`

- [ ] **Step 1: Write failing Markdown rendering tests**

```python
from rich.text import Text

import pytest

from core.terminal_markdown import MarkdownRenderError, render_markdown_to_ansi


def plain(ansi: str) -> str:
    return Text.from_ansi(ansi).plain


def test_markdown_removes_source_markers_and_keeps_structure():
    rendered = plain(render_markdown_to_ansi(
        "# 检查结果\n\n- 第一项\n- `core/llm.py`", width=60
    ))
    assert "检查结果" in rendered
    assert "第一项" in rendered
    assert "core/llm.py" in rendered
    assert "# 检查结果" not in rendered
    assert "`core/llm.py`" not in rendered


def test_table_fits_requested_terminal_width():
    markdown = "| 文件 | 职责 |\n| --- | --- |\n| core/llm.py | 模型调用和工具编排 |"
    rendered = plain(render_markdown_to_ansi(markdown, width=48))
    lines = rendered.splitlines()
    assert "core/llm.py" in rendered
    assert "模型调用和工具编排" in rendered
    assert max(len(line) for line in lines) <= 48


def test_invalid_markdown_type_fails_early():
    try:
        render_markdown_to_ansi(None, width=60)
    except TypeError as error:
        assert str(error) == "markdown_text must be a string"
    else:
        raise AssertionError("TypeError was not raised")


def test_markdown_parser_failure_is_explicit(monkeypatch):
    class BrokenMarkdown:
        def __init__(self, *args, **kwargs):
            raise ValueError("invalid document")

    monkeypatch.setattr("core.terminal_markdown.Markdown", BrokenMarkdown)
    with pytest.raises(MarkdownRenderError, match="Markdown 渲染失败"):
        render_markdown_to_ansi("# 标题", width=60)
```

- [ ] **Step 2: Run tests and confirm the renderer is missing**

Run: `venv\Scripts\python -m pytest -q tests\test_terminal_markdown.py`

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement Rich Markdown rendering**

Create `core/terminal_markdown.py`:

```python
from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.markdown import Markdown
from rich.theme import Theme


_MARKDOWN_THEME = Theme({
    "markdown.paragraph": "white",
    "markdown.h1": "bold white",
    "markdown.h2": "bold white",
    "markdown.h3": "bold white",
    "markdown.code": "cyan",
    "markdown.code_block": "white on #111111",
    "markdown.link": "cyan",
    "markdown.block_quote": "grey62",
    "markdown.item.bullet": "white",
    "table.header": "bold white",
    "table.border": "grey62",
})


class MarkdownRenderError(RuntimeError):
    pass


def render_markdown_to_ansi(markdown_text: str, width: int) -> str:
    if not isinstance(markdown_text, str):
        raise TypeError("markdown_text must be a string")
    if width < 20:
        raise ValueError("terminal width must be at least 20 columns")
    output = StringIO()
    console = Console(
        file=output,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
        theme=_MARKDOWN_THEME,
    )
    try:
        renderable = Markdown(markdown_text, code_theme="monokai", justify="left")
        console.print(renderable)
    except Exception as error:
        raise MarkdownRenderError(f"Markdown 渲染失败：{error}") from error
    return output.getvalue()
```

- [ ] **Step 4: Run Markdown tests at wide and narrow widths**

Run: `venv\Scripts\python -m pytest -q tests\test_terminal_markdown.py`

Expected: `4 passed`.

- [ ] **Step 5: Commit Markdown rendering**

```powershell
git add core/terminal_markdown.py tests/test_terminal_markdown.py
git commit -m "feat: render structured terminal markdown"
```

### Task 5: Render Gray Thinking, Tool Trails, and Final Markdown in Real Time

**Files:**
- Modify: `core/turn_renderer.py`
- Modify: `main.py` in `_render_ui_event()` and `_print_ui_fragment()`
- Modify: `tests/test_turn_renderer.py`
- Modify: `tests/test_main_ui_events.py`

- [ ] **Step 1: Add failing renderer and UI tests**

Add to `tests/test_turn_renderer.py`:

```python
def test_renderer_emits_reasoning_in_dim_gray_and_buffers_answer():
    renderer = TurnRenderer()
    thinking = "".join(renderer.consume("thinking_delta", {"text": "正在分析"}))
    answer_delta = renderer.consume("assistant_delta", {"text": "# 最终结论"})
    assert "\x1b[90m" in thinking
    assert "正在分析" in thinking
    assert answer_delta == []
    assert renderer.assistant_text() == "# 最终结论"
```

Add to `tests/test_main_ui_events.py`:

```python
def test_turn_finished_renders_buffered_answer_as_markdown(monkeypatch):
    factory = TurnEventFactory("turn-markdown")
    printed = []
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", printed.append)
    monkeypatch.setattr(main, "_terminal_width", lambda: 60)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-markdown", None)

    main._render_ui_event(factory.emit("assistant_delta", {"text": "# 结论"}))
    main._render_ui_event(factory.emit("turn_finished"))

    rendered = "".join(printed)
    assert "结论" in rendered
    assert "# 结论" not in Text.from_ansi(rendered).plain
```

- [ ] **Step 2: Run the tests and verify Markdown is still raw**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_turn_renderer.py tests\test_main_ui_events.py
```

Expected: reasoning is not emitted and the final answer still includes raw `#` syntax.

- [ ] **Step 3: Simplify TurnRenderer to consume exclusive events**

Remove mixed-tag parsing from `TurnRenderer`. Implement event handling as:

```python
        if kind == "thinking_delta":
            text = str(data.get("text", ""))
            if text:
                self._thinking_parts.append(text)
                self._emitted.append(f"{_ANSI_DIM}{text}{_ANSI_RESET}")
        elif kind == "assistant_delta":
            text = str(data.get("text", ""))
            self._assistant_text_parts.append(text)
            if self._is_meaningful_text(text):
                self._has_meaningful_assistant_text = True
```

Initialize `self._thinking_parts = []`. Keep tool/error/fallback behavior, but do not add assistant Markdown into `_records` because final rendering belongs to `core/terminal_markdown.py`.

- [ ] **Step 4: Render final Markdown in main through the existing terminal boundary**

In `main.py`:

```python
def _terminal_width():
    return max(20, shutil.get_terminal_size((100, 24)).columns)
```

At `turn_finished`, render the answer once:

```python
        for fragment in renderer.finish_incremental(include_assistant_text=False):
            _print_ui_fragment(fragment)
        answer = renderer.assistant_text()
        if answer:
            try:
                rendered = render_markdown_to_ansi(answer, _terminal_width())
            except MarkdownRenderError as error:
                _print_ui_fragment(f"\x1b[31m✗ {error}\x1b[0m")
            else:
                _print_ui_fragment(rendered)
```

Keep `_print_ui_fragment()` on `run_in_terminal()` so prompt_toolkit cannot erase output. Thinking and tool events continue through the same function.

- [ ] **Step 5: Run renderer and UI tests**

Run: `venv\Scripts\python -m pytest -q tests\test_turn_renderer.py tests\test_main_ui_events.py`

Expected: all tests pass.

- [ ] **Step 6: Commit live rendering**

```powershell
git add core/turn_renderer.py main.py tests/test_turn_renderer.py tests/test_main_ui_events.py
git commit -m "feat: render Kimi-style agent turns"
```

### Task 6: Replay Structured Thinking, Tools, and Markdown from Session History

**Files:**
- Create: `core/history_renderer.py`
- Create: `tests/test_history_renderer.py`
- Modify: `main.py` in `show_session_history()`

- [ ] **Step 1: Write failing history rendering tests**

```python
from rich.text import Text

from core.history_renderer import render_session_history


def plain(text: str) -> str:
    return Text.from_ansi(text).plain


def test_structured_history_replays_reasoning_and_markdown():
    history = [
        {"role": "user", "content": "检查项目"},
        {
            "role": "assistant",
            "content": "# 检查结果\n\n- 正常",
            "reasoning_content": "先查看项目结构",
        },
    ]
    rendered = plain(render_session_history(history, width=60))
    assert "检查项目" in rendered
    assert "先查看项目结构" in rendered
    assert "检查结果" in rendered
    assert "# 检查结果" not in rendered


def test_legacy_tagged_history_remains_compatible():
    history = [{
        "role": "assistant",
        "content": "<think>旧思考</think>旧回答",
    }]
    rendered = plain(render_session_history(history, width=60))
    assert "旧思考" in rendered
    assert "旧回答" in rendered
    assert "<think>" not in rendered
```

- [ ] **Step 2: Run tests and confirm history renderer is missing**

Run: `venv\Scripts\python -m pytest -q tests\test_history_renderer.py`

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement history rendering with shared presenters**

Create `core/history_renderer.py` with this public interface:

```python
from __future__ import annotations

import json

from core.reasoning_stream import split_reasoning_text
from core.terminal_markdown import render_markdown_to_ansi
from core.tool_presenter import summarize_tool_finished, summarize_tool_started


_DIM = "\x1b[90m"
_CYAN = "\x1b[36m"
_GREEN = "\x1b[32m"
_RESET = "\x1b[0m"


def _arguments(tool_call: dict) -> dict:
    raw = tool_call.get("function", {}).get("arguments", "{}")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def render_session_history(history: list[dict], width: int) -> str:
    output: list[str] = []
    pending_tools: dict[str, tuple[str, dict]] = {}
    for message in history:
        role = message.get("role")
        if role == "user":
            output.append(f"\x1b[36m✨ You >\x1b[0m {message.get('content', '')}\n")
            continue
        if role == "assistant":
            content = str(message.get("content") or "")
            reasoning = str(message.get("reasoning_content") or "")
            if not reasoning and "<think" in content:
                reasoning, content = split_reasoning_text(content)
            if reasoning:
                output.append(f"{_DIM}{reasoning}{_RESET}\n")
            for call in message.get("tool_calls") or []:
                name = call.get("function", {}).get("name", "tool")
                arguments = _arguments(call)
                pending_tools[str(call.get("id", ""))] = (name, arguments)
                output.append(f"{_CYAN}●{_RESET} {summarize_tool_started(name, arguments)}\n")
            if content.strip():
                output.append("\x1b[35m🤖 星瑶 >\x1b[0m\n")
                output.append(render_markdown_to_ansi(content, width))
            continue
        if role == "tool":
            tool_id = str(message.get("tool_call_id", ""))
            name, arguments = pending_tools.get(
                tool_id, (str(message.get("name") or "tool"), {})
            )
            result = {"content": message.get("content", "")}
            summary = summarize_tool_finished(name, arguments, result, ok=True)
            output.append(f"{_GREEN}✓{_RESET} {summary}\n")
    return "".join(output)
```

- [ ] **Step 4: Replace ad hoc history printing in main**

Replace the body of `show_session_history()` after loading history with:

```python
    rendered = render_session_history(history, _terminal_width())
    if not rendered:
        print_formatted_text(HTML(f"<ansiyellow>会话 {safe_session} 当前没有任何对话记录。</ansiyellow>"))
        return
    _print_ui_fragment(rendered)
```

Do not filter out `tool` messages and do not remove `<think>` with a one-shot regular expression.

- [ ] **Step 5: Run history and UI tests**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_history_renderer.py tests\test_main_ui_events.py tests\test_memory.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit history replay**

```powershell
git add core/history_renderer.py main.py tests/test_history_renderer.py tests/test_main_ui_events.py
git commit -m "feat: replay styled turn history"
```

### Task 7: Update Prompt Contract, Version, and End-to-End Verification

**Files:**
- Modify: `system_prompt.md`
- Modify: `%USERPROFILE%/.ycode/system_prompt.md`
- Modify: `main.py` version card
- Modify: `setup.py`
- Modify: `tests/test_context_builder.py`
- Modify: `tests/test_agent_turn_e2e.py`

- [ ] **Step 1: Add failing prompt and end-to-end assertions**

Add to `tests/test_context_builder.py`:

```python
def test_default_prompt_allows_terminal_markdown_without_forcing_reasoning():
    prompt = Path("system_prompt.md").read_text(encoding="utf-8")
    assert "支持标准 Markdown" in prompt
    assert "简单表格" in prompt
    assert "不要求模型伪造思考内容" in prompt
```

Extend `tests/test_agent_turn_e2e.py` so the fake provider returns native reasoning, a read-file tool call, and a Markdown table final answer. Assert:

```python
    assert any(event.kind == "thinking_delta" for event in events)
    assert "星瑶正在查看文件" in output
    assert "源代码正文" not in output
    rendered_answer = render_markdown_to_ansi("| 文件 | 状态 |\n| --- | --- |\n| a.py | 正常 |", 60)
    assert "a.py" in Text.from_ansi(rendered_answer).plain
```

- [ ] **Step 2: Run tests and verify the old prompt fails**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_context_builder.py tests\test_agent_turn_e2e.py
```

Expected: prompt assertions fail because the old prompt still discourages Markdown.

- [ ] **Step 3: Update both prompt files with the same terminal contract**

Replace the terminal-output section in both prompt files with:

```text
【终端输出协议】
1. Y-CODE 已支持标准 Markdown。最终回答可使用简洁标题、列表、代码块、引用和简单表格。
2. 不使用 HTML、嵌套表格或依赖浏览器布局的语法。
3. 工具执行完成后必须给出简短中文结论，说明结果、问题或下一步建议。
4. 不重复终端工具轨迹已经展示的长内容，不在读取文件时复述源码全文。
5. 不要求模型伪造思考内容；仅由模型原生推理通道提供可见思考。
```

Keep the existing safety, permission, verification, and tool-use rules unchanged.

- [ ] **Step 4: Upgrade version to v1.0.9**

In `main.py`, set the startup card to `v1.0.9`. In `setup.py`, set `version='1.0.9'`.

- [ ] **Step 5: Run focused and full automated verification**

Run:

```powershell
venv\Scripts\python -m pytest -q tests\test_context_builder.py tests\test_agent_turn_e2e.py
venv\Scripts\python -m pytest -q
venv\Scripts\python -m py_compile main.py core\llm.py core\memory.py core\reasoning_stream.py core\tool_presenter.py core\terminal_markdown.py core\history_renderer.py core\turn_renderer.py
```

Expected: all pytest tests pass and `py_compile` exits with code 0 and no output.

- [ ] **Step 6: Run a real Windows PTY acceptance turn**

Run:

```powershell
venv\Scripts\python main.py
```

Send: `检查一下这个项目是做什么的，并用表格列出核心文件`

Verify all of the following in the actual terminal:

- Startup card layout is unchanged and version is `v1.0.9`.
- Reasoning appears in gray before tool trails.
- Read-file lines say `星瑶正在查看文件：...` and never print source content.
- Final answer is white and contains aligned headings, bullets, inline code, and a table.
- Mouse wheel scrolls the Windows Terminal history.
- Input frame and status bar recover after completion.

- [ ] **Step 7: Commit prompt and release metadata**

```powershell
git add system_prompt.md main.py setup.py tests/test_context_builder.py tests/test_agent_turn_e2e.py
git commit -m "feat: ship Kimi-style terminal rendering v1.0.9"
```

Do not stage `%USERPROFILE%/.ycode/system_prompt.md`; it is runtime configuration outside the repository.

## Final Verification Checklist

- [ ] `thinking_delta` is the only event carrying visible reasoning.
- [ ] `assistant_delta` never contains reasoning tags or reasoning text.
- [ ] Tool summaries use Star-Yao Chinese copy and never leak read-file contents.
- [ ] Rich handles headings, lists, inline code, code blocks, quotes, and tables.
- [ ] Real-time output and restored history use the same presenters.
- [ ] Startup layout remains unchanged except for `v1.0.9`.
- [ ] Repository and active runtime prompts share the same Markdown contract.
- [ ] Full pytest, py_compile, and real Windows PTY acceptance all pass.
