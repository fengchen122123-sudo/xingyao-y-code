from __future__ import annotations

import json

from core.reasoning_stream import split_reasoning_text
from core.terminal_markdown import render_markdown_to_ansi
from core.tool_presenter import (
    summarize_tool_finished,
    tool_target,
)
from core.turn_renderer import sanitize_protocol_text
from core.ui_markers import (
    ANSI_BOLD,
    ANSI_RESET,
    MARK_DONE,
    MARK_FAILED,
    theme_ansi,
)


def _arguments(tool_call: dict) -> dict:
    raw = tool_call.get("function", {}).get("arguments", "{}")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _styled_summary(
    summary: str,
    target: str,
    theme_name: str | None = None,
) -> str:
    if not target or target not in summary:
        return summary
    before, _, after = summary.partition(target)
    accent_soft = theme_ansi("accent_soft", theme_name)
    return f"{before}{accent_soft}{target}{ANSI_RESET}{after}"


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
    from core.agent_service import strip_context_suffix

    if isinstance(raw, list):
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
    content = strip_context_suffix(content)
    return content.replace("\x1b", "")


def render_session_history(
    history: list[dict],
    width: int,
    tool_verbose: bool | None = None,
    theme_name: str | None = None,
) -> str:
    width = max(20, width)
    from core.computer.provider_generic import is_observation_protocol_message
    from core.injection_markers import is_system_injected_user_text
    if tool_verbose is None:
        tool_verbose = True
    output: list[str] = []
    pending_tools: dict[str, tuple[str, dict]] = {}
    accent = theme_ansi("accent", theme_name)
    border_dim = theme_ansi("border_dim", theme_name)
    ok_c = theme_ansi("ok", theme_name)
    err_c = theme_ansi("err", theme_name)
    muted = theme_ansi("text_muted", theme_name)
    sep_width = width if width <= 60 else min(max(60, width - 4), width - 4)
    separator = f"  {border_dim}{'─' * sep_width}{ANSI_RESET}\n"
    prev_block: str | None = None

    for message in history:
        role = message.get("role")
        if role == "user":
            if (
                is_observation_protocol_message(message)
                or is_observation_protocol_message(message.get("content"))
                or is_system_injected_user_text(message.get("content"))
            ):
                continue
            content = _sanitize_user_content(message.get("content"))
            if prev_block is not None:
                output.append("\n" + separator + "\n")
            output.append(f"  {ANSI_BOLD}{accent}❯ 你{ANSI_RESET} {content}\n")
            prev_block = "user"
            continue

        if role == "assistant":
            content = _strip_escapes(str(message.get("content") or ""))
            _tagged_reasoning, public_content = split_reasoning_text(content)
            if _tagged_reasoning:
                content = public_content

            content = sanitize_protocol_text(content)

            for call in message.get("tool_calls") or []:
                name = _strip_escapes(str(call.get("function", {}).get("name") or "tool"))
                arguments = _strip_escapes(_arguments(call))
                pending_tools[str(call.get("id", ""))] = (name, arguments)

            if content.strip():
                if prev_block not in (None, "user"):
                    output.append("\n")
                try:
                    from core.secretary import get_active_agent_name

                    _agent_label = get_active_agent_name()
                except Exception:
                    _agent_label = "星瑶"
                output.append(f"  {ANSI_BOLD}{accent}✦ {_agent_label}{ANSI_RESET}\n")
                try:
                    output.append(render_markdown_to_ansi(content, width, theme_name=theme_name))
                except Exception:
                    # 单块病态 markdown 只降级为原文，不能让整个历史回放失败
                    output.append(content or "")
                prev_block = "assistant"
            continue

        if role == "tool":
            tool_id = str(message.get("tool_call_id", ""))
            name, arguments = pending_tools.pop(
                tool_id,
                (_strip_escapes(str(message.get("name") or "tool")), {}),
            )
            result = {"content": message.get("content", "")}
            ok = not str(result["content"]).startswith("🚨")
            if not tool_verbose:
                continue
            summary = summarize_tool_finished(name, arguments, result, ok=ok)
            summary = _styled_summary(summary, tool_target(name, arguments), theme_name)
            color = ok_c if ok else err_c
            marker = MARK_DONE if ok else MARK_FAILED
            if prev_block not in (None, "assistant", "tool"):
                output.append("\n")
            output.append(f"  {color}{marker}{ANSI_RESET} {muted}·{ANSI_RESET} {summary}\n")
            prev_block = "tool"

    return "".join(output)
