"""Stable display buffering for streamed model reasoning."""

from __future__ import annotations


_SENTENCE_ENDINGS = frozenset("。！？.!?")


def _last_sentence_boundary(text: str) -> int:
    boundary = 0
    for index, char in enumerate(text):
        if char in _SENTENCE_ENDINGS or char in "\r\n":
            boundary = index + 1
    return boundary


def _stable_split(text: str, limit: int) -> int:
    whitespace = max(text.rfind(" ", 0, limit + 1), text.rfind("\t", 0, limit + 1))
    return whitespace if whitespace > 0 else limit


class ThinkingStreamBuffer:
    """Convert token-sized deltas into complete terminal display blocks."""

    def __init__(self, max_visible_chars: int = 120) -> None:
        if max_visible_chars < 1:
            raise ValueError("max_visible_chars must be positive")
        self._pending = ""
        self._max_visible_chars = max_visible_chars

    def feed(self, text: str) -> list[str]:
        self._pending += str(text or "")
        blocks: list[str] = []

        while self._pending:
            boundary = _last_sentence_boundary(self._pending)
            if boundary:
                block = self._take(boundary).rstrip("\r\n")
            elif len(self._pending) >= self._max_visible_chars:
                block = self._take(
                    _stable_split(self._pending, self._max_visible_chars)
                ).rstrip()
            else:
                break

            if block:
                blocks.append(block)

        return blocks

    def finish(self) -> list[str]:
        remaining = self._pending.strip("\r\n")
        self._pending = ""
        return [remaining] if remaining else []

    def _take(self, length: int) -> str:
        block = self._pending[:length]
        self._pending = self._pending[length:].lstrip(" \t\r\n")
        return block
