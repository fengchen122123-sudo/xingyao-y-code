"""Deterministic, terminal-only rendering for ordered turn events."""

from __future__ import annotations

import re
from typing import Any, Iterable

from core.thinking_stream import ThinkingStreamBuffer


_OPEN_TAGS = ("<thinking>", "<think>")
_CLOSE_TAGS = ("</thinking>", "</think>")
_ANSI_RESET = "\x1b[0m"
_ANSI_CYAN = "\x1b[36m"
_ANSI_GREEN = "\x1b[32m"
_ANSI_RED = "\x1b[31m"
_ANSI_YELLOW = "\x1b[33m"
_ANSI_DIM = "\x1b[90m"


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
    return text


class TurnRenderer:
    """Accumulate assistant text and tool status lines without doing I/O."""

    def __init__(self) -> None:
        self._records: list[str] = []
        self._pending_text = ""
        self._in_thinking = False
        self._tool_lines: dict[str, tuple[str, str]] = {}
        self._errors: set[str] = set()
        self._emitted: list[str] = []
        self._finished = False
        self._completed_tool_count = 0
        self._completed_tools: list[tuple[str, str]] = []
        self._has_meaningful_assistant_text = False
        self._thinking_stream = ThinkingStreamBuffer()
        self._assistant_text_parts: list[str] = []
        self._suppress_assistant_emit = False

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
        elif kind == "assistant_delta":
            self._consume_text(str(data.get("text", "")))
        elif kind == "tool_requested":
            self._tool_requested(data)
        elif kind == "tool_finished":
            self._tool_finished(data)
        elif kind == "error":
            self._error(str(data.get("message", "Unknown error")))
        elif kind == "diff_ready":
            diff = str(data.get("text") or data.get("diff") or "")
            if diff:
                self._append_line(diff)
        elif kind == "plan_updated":
            if data.get("live_only"):
                return []
            status_markers = {
                "completed": "✓",
                "in_progress": "■",
                "failed": "✗",
                "cancelled": "-",
                "pending": "□",
            }
            for item in data.get("items", []):
                status = str(item.get("status", "pending"))
                subject = str(item.get("subject", "")).strip()
                if subject:
                    self._append_line(f"  {status_markers.get(status, '□')} {subject}")

        return list(self._emitted)

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

            opening = _find_tag(self._pending_text, _OPEN_TAGS)
            if opening is None:
                safe_length = _safe_prefix_length(self._pending_text, _OPEN_TAGS)
                if safe_length:
                    self._append_text(self._pending_text[:safe_length])
                    self._pending_text = self._pending_text[safe_length:]
                return

            index, tag = opening
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

    def _tool_label(self, data: dict[str, Any]) -> str:
        summary = str(data.get("summary") or "").strip()
        if summary:
            return summary
        name = str(data.get("name") or "工具")
        preview = str(data.get("preview") or "").strip()
        return f"{name}({preview})" if preview else name

    def _styled_tool_summary(self, data: dict[str, Any]) -> str:
        summary = self._tool_label(data)
        target = str(data.get("preview") or "").strip()
        if not target or target not in summary:
            return summary
        before, _, after = summary.partition(target)
        return f"{before}{_ANSI_CYAN}{target}{_ANSI_RESET}{after}"

    def _tool_requested(self, data: dict[str, Any]) -> None:
        tool_id = str(data.get("id") or self._tool_label(data))
        label = self._tool_label(data)
        self._tool_lines[tool_id] = (label, "")
        self._append_line(f"{_ANSI_GREEN}●{_ANSI_RESET} {self._styled_tool_summary(data)}")

    def _tool_finished(self, data: dict[str, Any]) -> None:
        tool_id = str(data.get("id") or self._tool_label(data))
        requested_label = self._tool_lines.get(tool_id, ("", ""))[0]
        label = self._tool_label(data) if data.get("summary") else requested_label
        if not label:
            label = self._tool_label(data)
        ok = bool(data.get("ok", True))
        marker = "✓" if ok else "!"
        color = _ANSI_GREEN if ok else _ANSI_YELLOW
        styled_summary = self._styled_tool_summary(data) if data.get("summary") else label
        self._append_line(f"{color}{marker}{_ANSI_RESET} {styled_summary}")
        self._completed_tool_count += 1
        self._completed_tools.append((str(data.get("name", "tool")), label))

    def _error(self, message: str) -> None:
        if not message or message in self._errors:
            return
        self._errors.add(message)
        self._append_line(f"{_ANSI_YELLOW}  ! 星瑶暂未完成当前步骤{_ANSI_RESET}")

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
        self._flush_thinking()
        if not self._in_thinking and self._pending_text:
            self._append_text(self._pending_text)
        self._pending_text = ""
        if self._completed_tool_count and not self._has_meaningful_assistant_text:
            read_count = sum(name == "read_file" for name, _ in self._completed_tools)
            if read_count:
                summary = f"检查完成：已读取 {read_count} 个文件。模型未返回最终结论。"
            else:
                summary = f"检查完成：已执行 {self._completed_tool_count} 项工具操作。模型未返回最终结论。"
            self._append_line(f"  {summary}")

