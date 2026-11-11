"""`/goal` 目标模式的共享核心：状态机、持久化、完成判定、轮次循环。

为什么单独成子包：GUI 与 CLI 的差异全部集中在"怎么发起一轮"，其余（状态取值、
落盘格式、判定优先级、轮次语义）必须完全一致，否则同一个目标在两端会跑出不同
结果。这里只放纯 Python 逻辑，不 import GUI / CLI / 模型层。
"""

from core.slash_commands.goal.runner import (
    GOAL_ACHIEVED_MARKER,
    RoundExecutor,
    RoundOutcome,
    build_round_prompt,
    judge,
    run_goal_loop,
    summarize,
)
from core.slash_commands.goal.state import (
    VALID_STATUSES,
    GoalState,
    GoalStore,
    slugify_session,
)

__all__ = [
    # 任务约定的 6 个公开入口
    "GoalState",
    "GoalStore",
    "RoundOutcome",
    "RoundExecutor",
    "run_goal_loop",
    "judge",
    # 辅助项：适配层渲染状态、CLI 生成文件名、单测断言提示词时都需要
    "VALID_STATUSES",
    "GOAL_ACHIEVED_MARKER",
    "build_round_prompt",
    "summarize",
    "slugify_session",
]
