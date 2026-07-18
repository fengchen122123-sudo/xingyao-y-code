from __future__ import annotations

import json

from core.reasoning_stream import split_reasoning_text
from core.terminal_markdown import render_markdown_to_ansi
from core.tool_presenter import (
    summarize_tool_finished,
    summarize_tool_started,
    tool_target,
)


_DIM = "\x1b[90m"
_CYAN = "\x1b[36m"
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_MAGENTA = "\x1b[35m"
_RESET = "\x1b[0m"


def _arguments(tool_call: dict) -> dict:
    raw = tool_call.get("function", {}).get("arguments", "{}")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _styled_summary(summary: str, target: str) -> str:
    if not target or target not in summary:
        return summary
    before, _, after = summary.partition(target)
    return f"{before}{_CYAN}{target}{_RESET}{after}"


def render_session_history(history: list[dict], width: int) -> str:
    output: list[str] = []
    pending_tools: dict[str, tuple[str, dict]] = {}

    for message in history:
        role = message.get("role")
        if role == "user":
            content = str(message.get("content") or "")
            output.append(f"{_CYAN}✨ You >{_RESET} {content}\n")
            continue

        if role == "assistant":
            content = str(message.get("content") or "")
            reasoning = str(message.get("reasoning_content") or "")
            tagged_reasoning, public_content = split_reasoning_text(content)
            if tagged_reasoning:
                reasoning = reasoning or tagged_reasoning
                content = public_content

            if reasoning:
                output.append(f"{_DIM}{reasoning}{_RESET}\n")

            for call in message.get("tool_calls") or []:
                name = str(call.get("function", {}).get("name") or "tool")
                arguments = _arguments(call)
                pending_tools[str(call.get("id", ""))] = (name, arguments)
                summary = summarize_tool_started(name, arguments)
                summary = _styled_summary(summary, tool_target(name, arguments))
                output.append(f"{_GREEN}●{_RESET} {summary}\n")

            if content.strip():
                output.append(f"{_MAGENTA}🤖 星瑶 >{_RESET}\n")
                output.append(render_markdown_to_ansi(content, width))
            continue

        if role == "tool":
            tool_id = str(message.get("tool_call_id", ""))
            name, arguments = pending_tools.pop(
                tool_id,
                (str(message.get("name") or "tool"), {}),
            )
            result = {"content": message.get("content", "")}
            ok = not str(result["content"]).startswith("🚨")
            summary = summarize_tool_finished(name, arguments, result, ok=ok)
            summary = _styled_summary(summary, tool_target(name, arguments))
            color = _GREEN if ok else _RED
            marker = "✓" if ok else "✗"
            output.append(f"{color}{marker}{_RESET} {summary}\n")

    return "".join(output)
