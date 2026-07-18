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
    matches = []
    for tag in tags:
        index = text.find(tag)
        if index >= 0:
            matches.append((index, tag))
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
