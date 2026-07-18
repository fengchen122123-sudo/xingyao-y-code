"""Deterministic, terminal-only rendering for ordered turn events."""

from __future__ import annotations

from typing import Any, Iterable


_OPEN_TAGS = ("<thinking>", "<think>")
_CLOSE_TAGS = ("</thinking>", "</think>")


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

    def consume(self, kind: str, payload: dict[str, Any] | None = None) -> list[str]:
        if self._finished:
            return []
        self._emitted = []
        data = payload or {}

        if kind in ("assistant_delta", "thinking_delta"):
            if kind == "assistant_delta":
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
        if not text:
            return
        if self._records and not self._records[-1].endswith("\n"):
            self._records[-1] += text
        else:
            self._records.append(text)
        self._emitted.append(text)

    def _append_line(self, text: str) -> None:
        if self._records and not self._records[-1].endswith("\n"):
            self._records.append("\n")
            self._emitted.append("\n")
        line = text.rstrip("\n") + "\n"
        self._records.append(line)
        self._emitted.append(line)

    def _tool_label(self, data: dict[str, Any]) -> str:
        name = str(data.get("name") or "tool")
        preview = str(data.get("preview") or "").strip()
        return f"{name}({preview})" if preview else name

    def _tool_requested(self, data: dict[str, Any]) -> None:
        tool_id = str(data.get("id") or self._tool_label(data))
        label = self._tool_label(data)
        self._tool_lines[tool_id] = (label, "")
        self._append_line(f"  ↳ {label} …")

    def _tool_finished(self, data: dict[str, Any]) -> None:
        tool_id = str(data.get("id") or self._tool_label(data))
        label = self._tool_lines.get(tool_id, (self._tool_label(data), ""))[0]
        marker = "✓" if data.get("ok", True) else "✗"
        self._append_line(f"  {marker} {label}")

    def _error(self, message: str) -> None:
        if not message or message in self._errors:
            return
        self._errors.add(message)
        self._append_line(f"  ✗ {message}")

    def finish(self) -> str:
        if not self._finished:
            if not self._in_thinking and self._pending_text:
                self._append_text(self._pending_text)
            self._pending_text = ""
            self._finished = True
        return "".join(self._records).rstrip("\n")
