"""Deterministic, terminal-only rendering for ordered turn events."""

from __future__ import annotations

import re
from typing import Any, Iterable

from core.thinking_stream import ThinkingStreamBuffer
from core.tool_progress import ActiveToolStatus, ToolProgressAggregator
from core.ui_markers import (
    ANSI_RESET,
    ANSI_YELLOW,
    plan_color,
    plan_mark,
)


_OPEN_TAGS = ("<thinking>", "<think>")
_CLOSE_TAGS = ("</thinking>", "</think>")
_CODE_BLOCK_FENCES = ("```", "~~~")
_DIFF_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_DIFF_MARKER_RE = re.compile(r"^[\d\s]*([+\-])")


def _count_diff_changes(diff_text: str) -> tuple[int, int]:
    """粗略统计 diff 展示文本中的新增/删除行数，用于简短模式的单行摘要。

    diff_text 既可能是原始 unified diff，也可能是渲染成 Rich 表格/面板后的
    文本（core.llm._show_file_diff 生成，行首会带行号列与面板边框）。两种
    形式在剥离 ANSI 转义码、去掉行号列/边框后，内容列仍然以原始的 +/-/空格
    diff 前缀开头，因此可以用同一套启发式识别。
    """
    added = 0
    removed = 0
    for raw_line in diff_text.split("\n"):
        line = _DIFF_ANSI_RE.sub("", raw_line).strip().strip("│").strip()
        if line.startswith("+++") or line.startswith("---"):
            continue
        match = _DIFF_MARKER_RE.match(line)
        if not match:
            continue
        if match.group(1) == "+":
            added += 1
        else:
            removed += 1
    return added, removed


def _find_tag(text: str, tags: Iterable[str]) -> tuple[int, str] | None:
    found: list[tuple[int, str]] = []
    for tag in tags:
        index = text.find(tag)
        if index >= 0:
            found.append((index, tag))
    if not found:
        return None
    return min(found, key=lambda item: (item[0], -len(item[1])))


def _safe_prefix_length(text: str, tags: Iterable[str]) -> int:
    """Return the part that cannot be the beginning of a split tag."""
    tag_list = tuple(tags)
    max_tag_length = max((len(tag) for tag in tag_list), default=0)
    start = max(0, len(text) - max_tag_length + 1)
    for index in range(len(text), start - 1, -1):
        suffix = text[index:]
        if suffix and any(tag.startswith(suffix) for tag in tag_list):
            return index
    return len(text)


def sanitize_protocol_text(text: str) -> str:
    """清理输出文本中的内部协议标记、Advisor 调试头及裸露的 XML <tool_call> 标签。"""
    if not text or not isinstance(text, str):
        return text
    text = re.sub(r'<tool_call[^>]*>.*?</tool_call>', '', text, flags=re.DOTALL)
    text = re.sub(r'</?tool_call[^>]*>', '', text)
    text = re.sub(r'\[Advisor consultation[^\]]*\]', '', text)
    text = re.sub(r'\[Advisor review\]', '', text)
    text = re.sub(r'\[End of advisor consultation[^\]]*\]', '', text)
    # 圆括号形式的 advisor 内部说明（如 "(advisor returned no content)"）也会泄漏到正文，
    # 同样需要清理
    text = re.sub(r'\(\s*advisor[^\)]*\)', '', text, flags=re.IGNORECASE)
    return text


class TurnRenderer:
    """Accumulate assistant text and tool status lines without doing I/O."""

    def __init__(self) -> None:
        self._records: list[str] = []
        self._pending_text = ""
        self._in_thinking = False
        self._errors: set[str] = set()
        self._emitted: list[str] = []
        self._finished = False
        self._tool_progress = ToolProgressAggregator()
        self._saw_tool_event = False
        self._has_meaningful_assistant_text = False
        self._thinking_stream = ThinkingStreamBuffer()
        self._assistant_text_parts: list[str] = []
        self._suppress_assistant_emit = False
        self._in_code_block = False
        self._code_fence = ""

    def consume(self, kind: str, payload: dict[str, Any] | None = None) -> list[str]:
        if self._finished:
            return []
        self._emitted = []
        data = payload or {}

        if kind == "thinking_delta":
            text = str(data.get("text", ""))
            # 内部推理内容只消费不渲染
            for _ in self._thinking_stream.feed(text):
                pass
        elif kind == "thinking_finished":
            self._flush_thinking()
        elif kind == "thinking_started":
            self._append_tool_lines(self._tool_progress.flush())
        elif kind == "assistant_delta":
            self._append_tool_lines(self._tool_progress.flush())
            self._consume_text(str(data.get("text", "")))
        elif kind in {"tool_started", "tool_requested"}:
            self._saw_tool_event = True
            self._tool_progress.start(data)
        elif kind == "tool_finished":
            self._saw_tool_event = True
            self._append_tool_lines(self._tool_progress.finish(data))
        elif kind == "error":
            self._error(str(data.get("message", "Unknown error")))
        elif kind == "diff_ready":
            self._saw_tool_event = True
            diff = str(data.get("text") or data.get("diff") or "")
            if diff:
                if isinstance(data.get("added"), int) and isinstance(data.get("removed"), int):
                    added, removed = int(data["added"]), int(data["removed"])
                else:
                    added, removed = _count_diff_changes(diff)
                self._append_tool_lines(
                    self._tool_progress.record_diff(
                        str(data.get("tool_call_id") or data.get("id") or ""),
                        str(data.get("file_path") or "").strip(),
                        added,
                        removed,
                    )
                )
        elif kind == "plan_updated":
            if data.get("live_only"):
                return []
            for item in data.get("items", []):
                status = str(item.get("status", "pending"))
                subject = str(item.get("subject", "")).strip()
                if subject:
                    mark = plan_mark(status)
                    color = plan_color(status)
                    self._append_line(f"  {color}{mark}{ANSI_RESET} {subject}")

        return list(self._emitted)

    def active_tool_status(self) -> ActiveToolStatus | None:
        return self._tool_progress.active_status()

    def flush_tool_progress(self) -> list[str]:
        self._emitted = []
        self._append_tool_lines(self._tool_progress.flush())
        return list(self._emitted)

    def _append_tool_lines(self, lines: list[str]) -> None:
        for line in lines:
            self._append_line(line)

    def _consume_text(self, chunk: str) -> None:
        self._pending_text += chunk
        while self._pending_text:
            if self._in_thinking:
                closing = _find_tag(self._pending_text, _CLOSE_TAGS)
                if closing is None:
                    self._pending_text = self._pending_text[
                        _safe_prefix_length(self._pending_text, _CLOSE_TAGS):
                    ]
                    return
                index, tag = closing
                self._pending_text = self._pending_text[index + len(tag):]
                self._in_thinking = False
                continue

            if self._in_code_block:
                fence = self._code_fence
                closing = self._pending_text.find(fence)
                if closing == -1:
                    safe_length = _safe_prefix_length(
                        self._pending_text, (fence,)
                    )
                    if safe_length:
                        self._append_text(self._pending_text[:safe_length])
                        self._pending_text = self._pending_text[safe_length:]
                    return
                self._append_text(
                    self._pending_text[:closing + len(fence)]
                )
                self._pending_text = self._pending_text[closing + len(fence):]
                self._in_code_block = False
                self._code_fence = ""
                continue

            think_opening = _find_tag(self._pending_text, _OPEN_TAGS)
            code_opening = _find_tag(self._pending_text, _CODE_BLOCK_FENCES)

            # 代码块开始早于 think 开始：先处理代码块，跳过其中的 think 标签
            if code_opening is not None and (
                think_opening is None or code_opening[0] < think_opening[0]
            ):
                index, fence = code_opening
                if index:
                    # 递归处理代码块之前的普通文本（其中可能含 think 标签）
                    suffix = self._pending_text[index:]
                    self._pending_text = self._pending_text[:index]
                    self._consume_text("")
                    if self._pending_text:
                        # 递归调用后仍保留了被保守持留的尾部（例如一个尚未完整的
                        # <think> 标签前缀）。此时 fence 已经不再位于位置 0，不能
                        # 再假设 self._pending_text[:len(fence)] 就是围栏本身——
                        # 原样拼回并直接返回，等待下一块数据到达后重新判断；
                        # 回合结束时 finish_incremental 会把剩余内容当纯文本冲刷。
                        self._pending_text += suffix
                        return
                    self._pending_text = suffix
                self._append_text(self._pending_text[:len(fence)])
                self._pending_text = self._pending_text[len(fence):]
                self._in_code_block = True
                self._code_fence = fence
                continue

            if think_opening is None:
                safe_length = _safe_prefix_length(self._pending_text, _OPEN_TAGS)
                # 末尾的 ` 或 ~ 可能是 fenced code block 的开始，保守保留，
                # 避免 ```/~~~ 被拆散后无法识别为代码块。
                while safe_length > 0 and self._pending_text[safe_length - 1] in "`~":
                    safe_length -= 1
                if safe_length:
                    self._append_text(self._pending_text[:safe_length])
                    self._pending_text = self._pending_text[safe_length:]
                return

            index, tag = think_opening
            if index:
                self._append_text(self._pending_text[:index])
            self._pending_text = self._pending_text[index + len(tag):]
            self._in_thinking = True

    def _append_text(self, text: str) -> None:
        clean_text = sanitize_protocol_text(text)
        if not clean_text:
            return
        self._assistant_text_parts.append(clean_text)
        if not self._suppress_assistant_emit and clean_text:
            self._emitted.append(clean_text)
        if not self._is_meaningful_text(clean_text):
            return
        self._has_meaningful_assistant_text = True
        if self._records and not self._records[-1].endswith("\n"):
            self._records[-1] += clean_text
        else:
            self._records.append(clean_text)

    @staticmethod
    def _is_meaningful_text(text: str) -> bool:
        return bool(re.sub(r"[\s`*_#>~\-\[\](){}|.,!?，。！？:：;；/\\]+", "", text))

    def _append_line(self, text: str) -> None:
        self._finish_current_line()
        line = text.rstrip("\n") + "\n"
        self._records.append(line)
        self._emitted.append(line)

    def _finish_current_line(self) -> None:
        if self._records and not self._records[-1].endswith("\n"):
            self._records.append("\n")
            self._emitted.append("\n")

    def _flush_thinking(self) -> None:
        # 内部推理内容只消费不渲染
        for _ in self._thinking_stream.finish():
            pass

    def _error(self, message: str) -> None:
        if not message or message in self._errors:
            return
        self._errors.add(message)
        self._append_line(f"{ANSI_YELLOW}  ! 星瑶暂未完成当前步骤{ANSI_RESET}")

    def finish(self) -> str:
        if not self._finished:
            self._flush_pending()
            self._finished = True
        return "".join(self._records).rstrip("\n")

    def finish_incremental(self, include_assistant_text: bool = True) -> list[str]:
        """Flush only output that was waiting for a complete stream boundary."""
        if self._finished:
            return []
        self._emitted = []
        self._suppress_assistant_emit = not include_assistant_text
        try:
            self._flush_pending()
        finally:
            self._suppress_assistant_emit = False
        return list(self._emitted)

    def assistant_text(self) -> str:
        """Return the complete visible assistant reply after think tags are removed."""
        if not self._has_meaningful_assistant_text:
            return ""
        return "".join(self._assistant_text_parts)

    def _flush_pending(self) -> None:
        self._append_tool_lines(self._tool_progress.flush())
        self._flush_thinking()
        if not self._in_thinking and self._pending_text:
            self._append_text(self._pending_text)
        self._pending_text = ""
        if self._has_meaningful_assistant_text:
            return
        if self._saw_tool_event:
            return
        self._append_line("  \u661f\u7476\u672c\u8f6e\u6682\u672a\u8fd4\u56de\u5185\u5bb9\u3002")
        return
