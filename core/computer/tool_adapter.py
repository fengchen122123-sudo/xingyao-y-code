"""computer_action 的固定 Harness 适配器（设计 19.2）。

本模块只做保留名声明、Turn 上下文（capability token + owner lease + run id）
校验与 ToolSpec 语义声明，不执行鼠标或键盘；Provider 投影在
provider_generic.py（设计第 6 节依赖规则 5/7）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.computer.actions import COMPUTER_ACTION_TOOL_SCHEMA
from core.computer import stop_notice as stop_notice
from core.computer.models import (
    ComputerCapability,
    ComputerControlError,
    ComputerOwner,
    ComputerProtocol,
)
from core.tool_protocol import ToolResult, ToolSpec

COMPUTER_ACTION_TOOL_NAME = "computer_action"
COMPUTER_SELECT_TARGET_TOOL_NAME = "computer_select_target"

# Harness 保留名（设计第 6 节规则 8）：插件与 MCP 不得注册同名工具；
# computer_action 由固定适配器在 resolve_tool 之前识别（Task 9），
# computer_select_target 归原生协议接线（Task 11a）。
RESERVED_COMPUTER_TOOL_NAMES = frozenset({
    COMPUTER_ACTION_TOOL_NAME,
    COMPUTER_SELECT_TARGET_TOOL_NAME,
})


@dataclass(frozen=True)
class ComputerTurnLease:
    """Turn 冻结的电脑操控执行上下文（capability token + owner lease + run id）。

    由 Turn 接线层在 start_run 之后构造并注入 ToolExecutionContext.computer_lease
    （Task 10 的接线点）；固定适配器凭它校验设置开关、owner 租约与 run，
    并通过 controller 执行动作。controller 只要求 ComputerControlService 的
    execute(run_id, owner, action) / snapshot() 两个公共方法（测试可注入 fake）。
    """

    capability: ComputerCapability
    owner: ComputerOwner
    controller: Any
    run_id: str = ""


def computer_action_tool_spec() -> ToolSpec:
    """computer_action 的 ToolSpec 语义声明（设计 19.2）。

    仅供语义查询与测试消费：固定适配器不经注册表执行（注册表会拒绝保留名），
    execute 直接调用是编程错误。requires_permission=False 表示设置开关已经是
    用户授权边界，固定适配路径不再走权限询问链。
    """

    def _reserved_execution() -> ToolResult:
        raise RuntimeError(
            "computer_action 由固定 Harness 适配器执行，不得经注册表调用"
        )

    function = COMPUTER_ACTION_TOOL_SCHEMA["function"]
    return ToolSpec(
        name=COMPUTER_ACTION_TOOL_NAME,
        description=str(function["description"]),
        input_schema=function["parameters"],
        execute=_reserved_execution,
        read_only=False,
        mutates_files=False,
        requires_permission=False,
        cancellable=True,
        parallel_safe=False,
        # 规格 03 §3.2 点名 computer_action 归 system（A 阶段评审补修缺口）。
        side_effect_scope="system",
        activity_description="电脑操控",
    )


def lease_from_context(context) -> ComputerTurnLease | None:
    """从 ToolExecutionContext 提取 Turn 冻结的电脑操控 lease；无则 None。"""
    lease = getattr(context, "computer_lease", None)
    return lease if isinstance(lease, ComputerTurnLease) else None


def computer_turn_context_error(lease: ComputerTurnLease | None) -> ComputerControlError | None:
    """固定适配的 Turn 上下文校验：设置关闭 / 协议不符 / run 缺失返回稳定错误。"""
    if stop_notice.is_user_stop_active():
        # 用户点过停止：run 已经收尾，但回合还在继续，此时若只靠 lease 校验，
        # 模型仍会拿到"run 已结束"之类可重试的信号并继续调 computer_action。
        # 这里先给一个不可重试的稳定错误码，逼模型换策略或向用户说明。
        return ComputerControlError(
            "computer_user_stopped",
            "用户已停止电脑操控：本任务后续不要调用任何电脑操控工具，"
            "请改用其它方式或直接向用户说明",
        )
    if lease is None:
        return ComputerControlError(
            "computer_disabled",
            "当前 Turn 未冻结电脑操控能力，computer_action 不可用",
        )
    capability = lease.capability
    if not capability.enabled:
        return ComputerControlError(
            capability.error_code or "computer_disabled",
            capability.reason or "电脑操控不可用",
        )
    if capability.protocol != ComputerProtocol.GENERIC.value:
        return ComputerControlError(
            "computer_disabled",
            "当前 Turn 的电脑操控协议不是 generic，不能调用 computer_action",
        )
    if not lease.run_id:
        return ComputerControlError(
            "computer_invalid_state",
            "电脑操控 run 尚未建立，不能执行动作",
        )
    return None
