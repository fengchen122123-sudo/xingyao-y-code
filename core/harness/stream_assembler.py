"""Provider stream chunk assembly for one Harness Step."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from core.reasoning_stream import split_reasoning_text


class StreamProtocolError(ValueError):
    """A provider stream violated an invariant required for safe replay."""


@dataclass(frozen=True)
class StreamUpdate:
    public_text: str
    reasoning_text: str


@dataclass(frozen=True)
class AssembledResponse:
    content: str
    reasoning_content: str | None
    tool_calls: list[dict]
    thinking_blocks: list[dict] | None
    finish_reason: str | None
    usage: Any


def complete_thinking_blocks(delta):
    """Return only complete provider blocks that are safe to round-trip."""
    fields = getattr(delta, "provider_specific_fields", None) or {}
    blocks = fields.get("thinking_blocks") if isinstance(fields, dict) else None
    if not isinstance(blocks, list):
        return None
    complete = [
        deepcopy(block)
        for block in blocks
        if isinstance(block, dict)
        and (
            bool(block.get("signature"))
            or (
                block.get("type") == "redacted_thinking"
                and bool(block.get("data"))
            )
        )
    ]
    return complete or None


class StreamAssembler:
    """Accumulate normalized chat chunks without owning transport lifecycle."""

    def __init__(
        self,
        *,
        keep_reasoning_fields: bool = True,
        keep_thinking_blocks: bool = False,
        require_tool_call_reasoning: bool = False,
    ):
        self._keep_reasoning_fields = bool(keep_reasoning_fields)
        self._keep_thinking_blocks = bool(keep_thinking_blocks)
        self._require_tool_call_reasoning = bool(require_tool_call_reasoning)
        self._accumulated_content = ""
        self._tool_calls = {}
        self._in_native_think = False
        self._thinking_blocks = None
        self._finish_reason = None
        self._usage = None
        self._finish_public_text = ""
        self._raw_reasoning_content = ""
        self._finished = False

    @property
    def finish_public_text(self) -> str:
        return self._finish_public_text

    @property
    def raw_reasoning_content(self) -> str:
        return self._raw_reasoning_content

    def observe_usage(self, usage) -> None:
        """Replace stream usage with a later provider-reported value."""
        if self._finished:
            raise RuntimeError("stream assembler is already finished")
        if usage is not None:
            self._usage = usage

    def consume(self, chunk) -> StreamUpdate:
        if self._finished:
            raise RuntimeError("stream assembler is already finished")

        usage = getattr(chunk, "usage", None)
        if usage:
            self._usage = usage

        choices = getattr(chunk, "choices", None)
        if not choices:
            return StreamUpdate("", "")

        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason:
            self._finish_reason = finish_reason

        delta = choice.delta
        complete_blocks = complete_thinking_blocks(delta)
        if complete_blocks is not None:
            self._thinking_blocks = complete_blocks

        emitted = []
        reasoning = getattr(delta, "reasoning_content", "") or ""
        if not isinstance(reasoning, str):
            raise StreamProtocolError("provider reasoning_content must be a string")
        if reasoning:
            if not self._in_native_think:
                emitted.append("<think>")
                self._accumulated_content += "<think>"
                self._in_native_think = True
            emitted.append(reasoning)
            self._accumulated_content += reasoning

        content = getattr(delta, "content", "") or ""
        if not isinstance(content, str):
            raise StreamProtocolError("provider content must be a string")
        if content:
            if self._in_native_think:
                emitted.append("</think>")
                self._accumulated_content += "</think>"
                self._in_native_think = False
            emitted.append(content)
            self._accumulated_content += content

        for tool_call in getattr(delta, "tool_calls", None) or ():
            index = tool_call.index
            if index not in self._tool_calls:
                self._tool_calls[index] = {
                    "id": None,
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
            assembled = self._tool_calls[index]
            call_id = getattr(tool_call, "id", None)
            existing_id = assembled.get("id")
            if call_id:
                if not existing_id or str(existing_id).startswith("call_"):
                    assembled["id"] = str(call_id)
            elif not existing_id:
                assembled["id"] = f"call_{index}"

            function = tool_call.function
            name = getattr(function, "name", None)
            if name and not assembled["function"]["name"]:
                assembled["function"]["name"] = name
            arguments = getattr(function, "arguments", None)
            if arguments:
                assembled["function"]["arguments"] += arguments

        return StreamUpdate("".join(emitted), reasoning)

    def finish(self) -> AssembledResponse:
        if self._finished:
            raise RuntimeError("stream assembler is already finished")
        self._finished = True

        if self._in_native_think:
            self._finish_public_text = "</think>"
            self._accumulated_content += self._finish_public_text
            self._in_native_think = False

        reasoning_content, public_content = split_reasoning_text(
            self._accumulated_content
        )
        self._raw_reasoning_content = reasoning_content
        tool_calls = [
            self._tool_calls[index]
            for index in sorted(self._tool_calls)
        ]

        replay_reasoning = reasoning_content or None
        if not self._keep_reasoning_fields:
            replay_reasoning = None
        elif tool_calls and self._require_tool_call_reasoning:
            replay_reasoning = reasoning_content

        if (
            tool_calls
            and self._require_tool_call_reasoning
            and not isinstance(replay_reasoning, str)
        ):
            raise StreamProtocolError(
                "DeepSeek thinking tool call requires string reasoning_content"
            )

        thinking_blocks = (
            self._thinking_blocks if self._keep_thinking_blocks else None
        )
        return AssembledResponse(
            content=public_content,
            reasoning_content=replay_reasoning,
            tool_calls=tool_calls,
            thinking_blocks=thinking_blocks,
            finish_reason=self._finish_reason,
            usage=self._usage,
        )
