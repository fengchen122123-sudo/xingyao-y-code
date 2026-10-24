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
    ANSI_GRAY,
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
    from core.agent_service import strip_context_suffix

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
    # 历史回放：剥离当轮注入的「当前运行上下文 / 当前时间」后缀，
    # 那部分是给模型看的运行快照，不该回放到终端历史里
    content = strip_context_suffix(content)
    return content.replace("\x1b", "")


def render_session_history(
    history: list[dict],
    width: int,
    tool_verbose: bool | None = None,
) -> str:
    # 窄终端钳制：render_markdown_to_ansi 要求 width >= 20
    width = max(20, width)
    # 系统注入 user 消息识别：电脑操控观察跟进（core.computer 可能较重，
    # 惰性导入，与 memory.py 同一裁决）+ 后台任务回报/护栏催促/hook 上下文
    from core.computer.provider_generic import is_observation_protocol_message
    from core.injection_markers import is_system_injected_user_text
    # 历史回放默认显示工具行：查看历史时需要完整工具执行轨迹。
    # 调用方可显式传 tool_verbose=False 静默工具行（仅用于测试）。
    if tool_verbose is None:
        tool_verbose = True
    output: list[str] = []
    pending_tools: dict[str, tuple[str, dict]] = {}
    # 轮次分隔线：user 消息之间用一条灰色细线隔开；首条消息前不打印
    separator = f"{ANSI_GRAY}{'─' * min(width, 60)}{ANSI_RESET}\n"
    # 上一个输出块类型：None(尚未输出) / "user" / "assistant" / "tool"
    prev_block: str | None = None

    for message in history:
        role = message.get("role")
        if role == "user":
            # 系统注入的 user 消息（后台任务回报/护栏催促/hook 追加上下文/
            # 电脑操控观察跟进）不是用户发送的内容，历史回放一律跳过，
            # 避免冒充用户发言；与 GUI 气泡、红点导航同一裁决。
            if (
                is_observation_protocol_message(message)
                or is_observation_protocol_message(message.get("content"))
                or is_system_injected_user_text(message.get("content"))
            ):
                continue
            content = _sanitize_user_content(message.get("content"))
            if prev_block is not None:
                output.append("\n" + separator + "\n")
            output.append(f"{ANSI_CYAN}✨ You >{ANSI_RESET} {content}\n")
            prev_block = "user"
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
                # 与上一块之间补一个空行（紧跟在 user 提问后时不需要）
                if prev_block not in (None, "user"):
                    output.append("\n")
                try:
                    # 历史回放里的自称是“当前会话内的自称”：秘书会话显示秘书名
                    from core.secretary import get_active_agent_name

                    _agent_label = get_active_agent_name()
                except Exception:
                    _agent_label = "星瑶"
                output.append(f"{ANSI_MAGENTA}🤖 {_agent_label} >{ANSI_RESET}\n")
                try:
                    output.append(render_markdown_to_ansi(content, width))
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
            # 历史回放：每个工具只回放一行结果（不再有单独的"请求"行）。
            # tool_verbose=False 时静默工具行（仅用于测试）。
            if not tool_verbose:
                continue
            summary = summarize_tool_finished(name, arguments, result, ok=ok)
            summary = _styled_summary(summary, tool_target(name, arguments))
            color = ANSI_GREEN if ok else ANSI_RED
            marker = MARK_DONE if ok else MARK_FAILED
            # 工具行紧跟所属的 assistant 块；若上文是其他块则先补一个空行
            if prev_block not in (None, "assistant", "tool"):
                output.append("\n")
            output.append(f"{color}{marker}{ANSI_RESET} {summary}\n")
            prev_block = "tool"

    return "".join(output)
