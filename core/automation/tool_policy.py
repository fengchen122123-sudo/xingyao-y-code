"""星轨自动化回合的工具策略（G3：防任务自我繁殖的唯一事实源）。

星轨自动化派发的回合（automation turn）本身也在跑 Agent，如果不加门控，模型
可以创建/修改/删除自动化任务、或立即执行其他任务，形成"任务自己生任务"的
自我繁殖链（费用与副作用都会指数放大）。本模块集中定义该策略：

- schema 层（core.llm.get_tools_schema）用 ``hidden_tool_names_for_current_turn``
  把写类自动化工具从工具列表里剔除（模型不看到就不会调用）；
- 执行层（core.harness.tool_pipeline）用 ``is_automation_turn_tool_blocked``
  兜底拒绝（MCP / 插件 / 保留名等不经过注册表 schema 的调用同样被拦下）；
- 工具 wrapper（core.tool_registry）用 ``automation_write_denied`` 做最后一道
  拦截，保证直接调用也不落库。

两侧共用同一份策略是本仓库既有范式（参见 core.secretary）：策略只写一处，
避免 schema 与执行层判定漂移。

依赖纪律：只读 core.app_context 的线程本地标记，禁止 import gui_bridge /
sqlite3——工具 schema 构造在模型请求热路径上，任何 GUI / 存储依赖都会把启动
顺序与 IO 风险带进来。
"""

from __future__ import annotations

import logging

import core.app_context as app_context

logger = logging.getLogger(__name__)

# 自动化回合里既不暴露（schema 层）也拒绝执行（执行层）的写类工具。
# automation_list 是只读查询，必须保留：模型需要它来了解现状并向用户提建议。
AUTOMATION_TURN_HIDDEN_TOOLS = frozenset({
    "automation_create",
    "automation_update",
    "automation_delete",
    "automation_run_now",
})

AUTOMATION_TURN_DENIED_MESSAGE = (
    "本回合由星轨自动化派发，为防止任务自我繁殖，不能创建/修改/删除自动化任务，"
    "也不能立即执行其他任务。可以先用 automation_list 查看现状，或把该操作写进给用户的建议里。"
)


def is_automation_turn() -> bool:
    """当前线程是否处于星轨自动化派发的回合。

    标记读取异常按「非自动化回合」放行并记 debug：门控组件故障不得让普通会话
    丢工具或工具直接失败（有意识的 fail-open，与秘书会话判定的处理同风格）。
    """
    try:
        return bool(app_context.get_thread_automation_turn())
    except Exception:
        logger.debug("自动化回合标记读取失败（旁路，按非自动化回合处理）", exc_info=True)
        return False


def hidden_tool_names_for_current_turn() -> frozenset[str]:
    """当前回合需要从工具 schema 中隐藏的工具名集合。

    自动化回合返回 ``AUTOMATION_TURN_HIDDEN_TOOLS``，其余回合返回空集——空集
    时调用方必须保持工具列表逐字节不变（前缀缓存命中的前提）。
    """
    return AUTOMATION_TURN_HIDDEN_TOOLS if is_automation_turn() else frozenset()


def is_automation_turn_tool_blocked(tool_name: str) -> bool:
    """执行侧唯一事实源：该工具在自动化回合里是否必须被拒绝。"""
    name = str(tool_name or "").strip()
    if not name:
        return False
    return is_automation_turn() and name in AUTOMATION_TURN_HIDDEN_TOOLS


def automation_write_denied() -> bool:
    """写类工具 wrapper 的一行判定：自动化回合里一律拒绝。

    被包装的函数本身就是写类工具，因此只需判定回合标记。
    """
    return is_automation_turn()
