from __future__ import annotations

import json

from core.reasoning_stream import split_reasoning_text
from core.terminal_markdown import render_markdown_to_ansi
from core.tool_presenter import (
    summarize_tool_finished,
    summarize_tool_started,
    tool_target,
)
from core.turn_renderer import sanitize_protocol_text


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


def _strip_escapes(value):
    """递归剥离字符串中的 ANSI 转义字符。

    历史记录里的 assistant/reasoning/tool 字符串均来自模型生成内容，
    可能携带 \\x1b[2J 之类的终端控制序列，回放时必须统一在输入侧剥离，
    防止注入劫持终端（rich 自身生成的颜色序列不受影响）。
    """
    if isinstance(value, str):
        return value.replace("\x1b", "")
    if isinstance(value, list):
        return [_strip_escapes(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_escapes(item) for key, item in value.items()}
    return value


def _sanitize_user_content(raw) -> str:
    """提取用户消息文本并剥离 ANSI 转义序列，防止 \x1b[ 注入劫持终端。"""
    if isinstance(raw, list):
        # 多模态 list 内容：只提取 text 块，图片块以占位说明表示
        parts = []
        for block in raw:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "image_url":
                    parts.append("[图片]")
        content = " ".join(part for part in parts if part)
    else:
        content = str(raw or "")
    return content.replace("\x1b", "")


def render_session_history(history: list[dict], width: int) -> str:
    # 窄终端钳制：render_markdown_to_ansi 要求 width >= 20
    width = max(20, width)
    output: list[str] = []
    pending_tools: dict[str, tuple[str, dict]] = {}

    for message in history:
        role = message.get("role")
        if role == "user":
            content = _sanitize_user_content(message.get("content"))
            output.append(f"{_CYAN}✨ You >{_RESET} {content}\n")
            continue

        if role == "assistant":
            content = _strip_escapes(str(message.get("content") or ""))
            reasoning = _strip_escapes(str(message.get("reasoning_content") or ""))
            tagged_reasoning, public_content = split_reasoning_text(content)
            if tagged_reasoning:
                reasoning = reasoning or tagged_reasoning
                content = public_content

            content = sanitize_protocol_text(content)

            if reasoning:
                output.append(f"{_DIM}{reasoning}{_RESET}\n")

            for call in message.get("tool_calls") or []:
                name = _strip_escapes(str(call.get("function", {}).get("name") or "tool"))
                arguments = _strip_escapes(_arguments(call))
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
                (_strip_escapes(str(message.get("name") or "tool")), {}),
            )
            result = {"content": message.get("content", "")}
            ok = not str(result["content"]).startswith("🚨")
            summary = summarize_tool_finished(name, arguments, result, ok=ok)
            summary = _styled_summary(summary, tool_target(name, arguments))
            color = _GREEN if ok else _RED
            marker = "✓" if ok else "✗"
            output.append(f"{color}{marker}{_RESET} {summary}\n")

    return "".join(output)
