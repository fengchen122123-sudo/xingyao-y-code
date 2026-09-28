"""Compose stable instructions and per-turn runtime context."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.injection_markers import DYNAMIC_CONTEXT_PREFIX, TIME_NOTE_PREFIX

if TYPE_CHECKING:
    from core.computer.models import ComputerCapability


_DECISION_POLICY = """【决策与行动规则】
1. 任务先拆解再动手；子任务不超过 3 条的直接执行（只做一次判断，不为已经清楚的目标增加额外流程）；超过 3 条的先建立计划再逐条执行。
2. 没有新证据时，不得反复重新讨论已经排除的方案、工具或风险。
3. 接到任务先对照【可用技能目录】检查匹配的技能（任何工具调用或文件修改之前）；哪怕只有轻微相关也先 read_file 读取对应 SKILL.md 再动手，发现不匹配再放弃；确定无关的技能不加载。
4. 需要多个互不依赖的工具或子任务时一次性并行发出，拿到结果后直接汇总。
5. 推理只服务于确定下一步动作；证据充分后立即行动。
6. 需要实时天气时使用 get_weather 并传入明确城市；城市未知时先询问；需要读取网页或 JSON 时使用 fetch_url；search_news 仅用于新闻。
7. 子任务超过 3 条的任务先创建包含依赖、角色、文件范围、阶段、verification_type 和验证内容的计划，再按 ready 项执行；信息查询使用 result 验证，只有真实 Shell 命令使用 command 验证。
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
    command_shell: str = "自动（系统默认）"
    memory: str = "无"
    skills: str = "无"
    custom_subagents: str = "无"
    # Turn 起点冻结的电脑操控能力（Task 10，设计 9.3）：默认 None，
    # disabled/None 时动态块字节与历史版本完全一致。
    computer_capability: "ComputerCapability | None" = None
    # 按回合撤销纠正动态块（规格 15.3）：无纠正时为空串，动态块字节不变。
    workspace_corrections: str = ""
    # AI 生成内容目录（Task 6）：配置级稳定值（默认 ~/.ycode/tmp），
    # 跨轮不变，符合动态块"不放每轮必变字段"的约束。
    ai_output_dir: str = ""


def build_stable_prompt(base_prompt: str) -> str:
    """Build the stable prefix that stays unchanged across turns.

    Contains only the base system prompt and the hardcoded decision policy.
    This prefix is safe for provider-side prompt caching because it does not
    include per-turn dynamic data (time, plan snapshot, memory, skills).
    """
    stable = (base_prompt or "").strip()
    parts = [section for section in (stable, _DECISION_POLICY) if section]
    return "\n\n".join(parts)


# 保留标记的唯一来源已收敛到 core/injection_markers.py（T1.5 防伪造基础设施），
# 由文件顶部 import 引入。常量值逐字未变，且
# `from core.context_builder import DYNAMIC_CONTEXT_PREFIX` 等既有引用路径保持可用。


def build_dynamic_prompt(runtime: RuntimeContext) -> str:
    """Build the per-turn runtime context block.

    该块以 system 角色持久化到会话历史中（append-only），内容不变时不再
    重复注入，保证跨轮请求的 prefix 连续、最大化 provider 侧缓存命中。
    因此块内绝不放每轮必变的字段（当前时间单独用 build_time_note 注入）。

    注意：长期记忆 / 技能目录 / 自定义子 Agent 清单等跨轮基本不变的静态内容
    已并入稳定系统提示词（core.agent_service.build_stable_system_message），
    本块只承载会话内可能变化的字段（模型 / 权限模式 / 工作目录 / 计划快照）。

    电脑操控说明（Task 10，设计 10.1）：只在 Turn 冻结的 capability 开启时
    追加到块末尾；默认 None / disabled 时返回值与历史版本逐字节一致。

    渲染方式（规格 02 §3.2 步骤 2 渲染接管）：本函数不再手工拼段，而是遍历
    注入注册表的 PERSISTED/SYSTEM 通道——注入点 3（_build_runtime_context_block）
    / 注入点 6（电脑操控说明）/ 注入点 7（按回合撤销纠正）——按登记顺序取非空段
    并以 "\\n\\n" 连接。分段点正落在改造前手工追加 "\\n\\n" 的位置，故输出逐字节
    一致。防递归：注册表的 runtime_context_block builder 指向基础块函数
    _build_runtime_context_block，绝不回调本函数。
    """
    from core.context_injection import build_default_injection_registry

    registry = build_default_injection_registry(runtime_provider=lambda: runtime)
    sections = [section for section in registry.build_persisted_sections() if section]
    return "\n\n".join(sections)


def _build_runtime_context_block(runtime: RuntimeContext) -> str:
    """运行上下文基础块（注入点 3）：不含电脑操控说明与按回合撤销纠正。

    为什么从 build_dynamic_prompt 里拆出来：注册表把基础块 / 电脑操控说明 /
    按回合纠正登记为三条独立 PERSISTED/SYSTEM source（规格 02 §2.2），每条
    builder 必须能独立取值，否则会出现"基础块已含 6/7、注册表再拼一次"的双份
    注入；同时这也避免注册表 builder 回调 build_dynamic_prompt 造成的无限递归。
    """
    plan_instruction = ""
    if runtime.permission_mode == "plan":
        plan_instruction = (
            "计划模式约束：只允许读取信息、执行明确的只读查询和维护计划元数据；"
            "不得修改或删除工作区文件。最终回答必须输出可执行计划，并说明需要"
            "切换到变更前确认、自动编辑或完全访问模式后再实施。\n"
        )
    prompt = (
        f"{DYNAMIC_CONTEXT_PREFIX}\n"
        f"当前模型：{runtime.model_name}\n"
        f"当前权限模式：{runtime.permission_mode}\n"
        f"当前工作目录：{runtime.working_dir}\n"
        f"默认命令解释器：{runtime.command_shell}\n"
        "生成命令时必须优先使用该解释器的语法；命令失败后根据错误调整语法，"
        "必要时可通过 execute_command 的 shell_id 显式选择其他已安装解释器。\n"
        f"AI 生成内容目录：{runtime.ai_output_dir}\n"
        "AI 产出的图片、下载文件与临时脚本一律写到该目录；"
        "用户显式指定了保存位置时以用户指定为准。\n"
        f"当前计划：{runtime.active_plan}\n"
        f"{plan_instruction}"
        "（若上下文中存在多份本块，以最新一份为准）"
    )
    return prompt


def _build_computer_prompt_section(capability) -> str:
    """电脑操控动态说明：capability 关闭时返回空串（动态块字节不变）。"""
    if capability is None or not capability.enabled:
        return ""
    from core.computer.models import ComputerProtocol
    from core.computer.provider_generic import build_generic_dynamic_prompt

    generic_section = build_generic_dynamic_prompt(capability)
    if generic_section:
        return generic_section
    if capability.protocol == ComputerProtocol.NATIVE_OPENAI.value:
        return (
            "电脑操控使用说明（OpenAI 原生协议）：请先单独调用 "
            "computer_select_target 选择唯一操控目标，之后再执行原生电脑动作；"
            "每次动作后依据返回的最新截图决定下一步。"
        )
    return ""


def build_time_note(runtime: RuntimeContext) -> str:
    """每轮的时间注记，由 agent_service 内联到最后一条 user 消息 content 末尾。

    不再作为独立 role=system 消息持久化——那样会使 system_sha256 每轮变化，
    导致 provider 侧前缀缓存从 system 块开始整体失效。内联到 user 消息后，
    system_messages 数组跨轮稳定，前缀缓存可覆盖整个对话历史。
    """
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
