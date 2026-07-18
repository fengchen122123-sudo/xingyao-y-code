"""Compose stable instructions and per-turn runtime context."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeContext:
    model_name: str
    permission_mode: str
    working_dir: str
    current_time: str
    active_plan: str = "无"
    memory: str = "无"
    skills: str = "无"


def build_system_prompt(base_prompt: str, runtime: RuntimeContext) -> str:
    stable = (base_prompt or "").strip()
    dynamic = (
        "【当前运行上下文】\n"
        f"当前模型：{runtime.model_name}\n"
        f"当前权限模式：{runtime.permission_mode}\n"
        f"当前工作目录：{runtime.working_dir}\n"
        f"当前时间：{runtime.current_time}\n"
        f"当前计划：{runtime.active_plan}\n"
        f"长期记忆：{runtime.memory}\n"
        f"已启用技能：{runtime.skills}"
    )
    return f"{stable}\n\n{dynamic}" if stable else dynamic


def without_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy without stale system prompts from previous turns."""
    return [deepcopy(message) for message in messages if message.get("role") != "system"]
