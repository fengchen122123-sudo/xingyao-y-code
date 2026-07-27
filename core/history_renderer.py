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
    ANSI_CYAN,
    ANSI_GREEN,
    ANSI_MAGENTA,
    ANSI_RED,
    ANSI_RESET,
    MARK_DONE,
    MARK_FAILED,
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


def _styled_summary(summary: str, target: str) -> str:
    if not target or target not in summary:
        return summary
    before, _, after = summary.partition(target)
    return f"{before}{ANSI_CYAN}{target}{ANSI_RESET}{after}"


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


def render_session_history(
    history: list[dict],
    width: int,
    tool_verbose: bool | None = None,
) -> str:
    # 窄终端钳制：render_markdown_to_ansi 要求 width >= 20
    width = max(20, width)
    if tool_verbose is None:
        # 读取与实时路径相同的配置项：回放应体现"当前"的详细/简短偏好，
        # 而不是消息记录产生时的偏好。调用方也可以显式传参覆盖（便于测试）。
        try:
            from core.model_manager import load_config
            tool_verbose = bool(load_config().get("tool_verbose", False))
        except Exception:
            tool_verbose = False
    output: list[str] = []
    pending_tools: dict[str, tuple[str, dict]] = {}

    for message in history:
        role = message.get("role")
        if role == "user":
            content = _sanitize_user_content(message.get("content"))
            output.append(f"{ANSI_CYAN}✨ You >{ANSI_RESET} {content}\n")
            continue

        if role == "assistant":
            content = _strip_escapes(str(message.get("content") or ""))
            # reasoning_content 字段与内联 <think>/<thinking> 标签包裹的内部推理
            # 一律不回放：与实时路径保持一致（思考过程只消费不展示），
            # 这里只提取标签之外的公开正文，推理内容本身直接丢弃。
            _tagged_reasoning, public_content = split_reasoning_text(content)
            if _tagged_reasoning:
                content = public_content

            content = sanitize_protocol_text(content)

            for call in message.get("tool_calls") or []:
                name = _strip_escapes(str(call.get("function", {}).get("name") or "tool"))
                arguments = _strip_escapes(_arguments(call))
                pending_tools[str(call.get("id", ""))] = (name, arguments)

            if content.strip():
                output.append(f"{ANSI_MAGENTA}🤖 星瑶 >{ANSI_RESET}\n")
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
            # 与实时详细模式一致：每个工具只回放一行结果（不再有单独的"请求"行）。
            # 简短模式下所有工具调用都保持静默，避免刷屏。
            if not tool_verbose:
                continue
            summary = summarize_tool_finished(name, arguments, result, ok=ok)
            summary = _styled_summary(summary, tool_target(name, arguments))
            color = ANSI_GREEN if ok else ANSI_RED
            marker = MARK_DONE if ok else MARK_FAILED
            output.append(f"{color}{marker}{ANSI_RESET} {summary}\n")

    return "".join(output)
