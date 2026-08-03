"""Compose stable instructions and per-turn runtime context."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


_DECISION_POLICY = """【决策与行动规则】
1. 明确且简单的任务只做一次判断，然后立即执行，不为已经清楚的目标增加额外流程。
2. 没有新证据时，不得反复重新讨论已经排除的方案、工具或风险。
3. 只读取与当前任务直接匹配的一个或少量 SKILL.md；不要为了确认是否相关而加载无关技能。
4. 需要多个互不依赖的工具或子任务时一次性并行发出，拿到结果后直接汇总。
5. 推理只服务于确定下一步动作；证据充分后立即行动。
6. 需要实时天气时使用 get_weather 并传入明确城市；城市未知时先询问；需要读取网页或 JSON 时使用 fetch_url；search_news 仅用于新闻。
7. 复杂任务先创建包含依赖、角色、文件范围、阶段、verification_type 和验证内容的计划，再按 ready 项执行；信息查询使用 result 验证，只有真实 Shell 命令使用 command 验证。
8. Plan 阶段只读分析，Build 阶段负责实现，Review 阶段只读审查；每个计划项必须关联具体 plan_item_id。`ask_user` 的 options 只能提供 1 到 3 个选项。用户选择必须单独建立准备项并设置 kind=input_preparation，执行项通过 depends_on 依赖该准备项并保持 kind=execution；ask_user(plan_item_id=...) 只能绑定准备项，不得把 ask_user 绑定到真正执行查询或修改的计划项。不能在计划失败后绕过计划直接调用普通工具。
9. 始终使用与用户一致的语言输出；用户使用中文时，所有可见内容（含计划评审、进度说明、最终结论）必须使用中文，不得夹带英文段落。
"""


@dataclass(frozen=True)
class RuntimeContext:
    model_name: str
    permission_mode: str
    working_dir: str
    current_time: str
    active_plan: str = "无"
    memory: str = "无"
    skills: str = "无"
    custom_subagents: str = "无"


def build_stable_prompt(base_prompt: str) -> str:
    """Build the stable prefix that stays unchanged across turns.

    Contains only the base system prompt and the hardcoded decision policy.
    This prefix is safe for provider-side prompt caching because it does not
    include per-turn dynamic data (time, plan snapshot, memory, skills).
    """
    stable = (base_prompt or "").strip()
    parts = [section for section in (stable, _DECISION_POLICY) if section]
    return "\n\n".join(parts)


DYNAMIC_CONTEXT_PREFIX = "【当前运行上下文】"
TIME_NOTE_PREFIX = "【当前时间】"


def build_dynamic_prompt(runtime: RuntimeContext) -> str:
    """Build the semi-static runtime context block.

    该块以 system 角色持久化到会话历史中（append-only），内容不变时不再
    重复注入，保证跨轮请求的 prefix 连续、最大化 provider 侧缓存命中。
    因此块内绝不放每轮必变的字段（当前时间单独用 build_time_note 注入）。
    """
    return (
        f"{DYNAMIC_CONTEXT_PREFIX}\n"
        f"当前模型：{runtime.model_name}\n"
        f"当前权限模式：{runtime.permission_mode}\n"
        f"当前工作目录：{runtime.working_dir}\n"
        f"当前计划：{runtime.active_plan}\n"
        f"长期记忆：{runtime.memory}\n"
        f"已启用技能：{runtime.skills}\n"
        f"可用自定义子 Agent：{runtime.custom_subagents}\n"
        "（若上下文中存在多份本块，以最新一份为准）"
    )


def build_time_note(runtime: RuntimeContext) -> str:
    """每轮新追加的时间注记（纯增量，不破坏 prefix 缓存，旧的保留不删）。"""
    return f"{TIME_NOTE_PREFIX}{runtime.current_time}"


def build_system_prompt(base_prompt: str, runtime: RuntimeContext) -> str:
    """Legacy combined prompt (stable + dynamic).

    Kept for backward compatibility.  New code should prefer
    build_stable_prompt + build_dynamic_prompt to enable prompt caching.
    """
    stable = build_stable_prompt(base_prompt)
    dynamic = build_dynamic_prompt(runtime)
    return "\n\n".join([stable, dynamic])


def without_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy without stale system prompts from previous turns."""
    return [deepcopy(message) for message in messages if message.get("role") != "system"]
