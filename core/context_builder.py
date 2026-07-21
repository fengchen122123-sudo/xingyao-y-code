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
    sections = [section for section in (stable, _DECISION_POLICY, dynamic) if section]
    return "\n\n".join(sections)


def without_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy without stale system prompts from previous turns."""
    return [deepcopy(message) for message in messages if message.get("role") != "system"]
