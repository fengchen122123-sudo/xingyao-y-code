"""Hooks 契约层：事件、注册项与 stdin/stdout 数据契约（面向 ZCode 能力借鉴 · 规格 01 · C1）。

本模块只放「跨模块共享的数据形状」与「结果合并规则」，不触碰文件系统与子进程：

- ``HookEvent``：v1 的 4 个事件点（规格 §2）；
- ``HookRegistration``：一条已解析的 hook。信任校验是执行前的**动态**步骤
  （可被随时撤销），因此不固化在注册项上，digest 只承载指纹、不承载信任结论；
- ``HookInput`` / ``HookOutput``：stdin / stdout 的 JSON 契约（规格 §3.3 / §3.4）；
- ``HookRunResult`` 与 ``merge_output``：多 hook 聚合与合并规则（规格 §5.3）。

为什么合并规则放在契约层：它是「结果语义」而不是「执行编排」，放这里可以被
单元测试直接锁定；runner 只负责按序调用，不掺入字段级合并判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

# 超时约束（规格 §3.2）：默认 60s、上限 300s。常量放契约层供 registry 与 trust
# 共用，避免 registry ↔ trust 为了一个默认值产生导入依赖。
DEFAULT_TIMEOUT_MS = 60000
MAX_TIMEOUT_MS = 300000

# 项目级 hook 默认拿不到完整 tool_input 时，参数值被替换成的占位符
# （C10 数据保护机制，规格 §3.3）。
REDACTED_PLACEHOLDER = "[redacted]"


class HookEvent(str, Enum):
    """v1 支持的 4 个生命周期事件（规格 §2）。"""

    SESSION_START = "SessionStart"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"


#: matcher 仅在工具事件上生效；其余事件默认全匹配（规格 §3.2）。
TOOL_EVENTS: frozenset[HookEvent] = frozenset(
    {HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE}
)


@dataclass(frozen=True)
class HookRegistration:
    """一条已加载的 hook 注册项。

    为什么 frozen：注册项是配置加载的产物，就地修改它等于绕过指纹重算，
    会让「信任绑定的是哪份配置」变得不可追溯。需要变更就重新加载配置。
    """

    event: HookEvent
    matcher: str
    command: str
    timeout_ms: int
    status_message: str
    source_path: str
    scope: Literal["user", "project"]
    digest: str


@dataclass(frozen=True)
class HookInput:
    """发给 hook 的 stdin 载荷（规格 §3.3）。

    事件特有字段以空值表示「不适用」，``to_wire`` 时省略空值：既让不同事件的
    stdin 结构紧凑，也让 hook 侧「字段缺失 = 本事件不适用」的判读稳定。
    """

    hook_event_name: str
    session_id: str
    turn_id: str
    cwd: str
    permission_mode: str
    timestamp: str
    # 事件特有字段（默认空 = 不适用）
    tool_name: str = ""
    tool_input: dict | None = None
    risk_level: str = ""
    permission_decision: str = ""
    tool_result_preview: str = ""
    tool_call_count: int = 0
    response_preview: str = ""

    def to_wire(self) -> dict:
        """序列化为 stdin JSON 字典（省略空值字段）。"""
        payload = {
            "hook_event_name": self.hook_event_name,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "cwd": self.cwd,
            "permission_mode": self.permission_mode,
            "timestamp": self.timestamp,
        }
        if self.tool_name:
            payload["tool_name"] = self.tool_name
        if self.tool_input is not None:
            payload["tool_input"] = self.tool_input
        if self.risk_level:
            payload["risk_level"] = self.risk_level
        if self.permission_decision:
            payload["permission_decision"] = self.permission_decision
        if self.tool_result_preview:
            payload["tool_result_preview"] = self.tool_result_preview
        if self.tool_call_count:
            payload["tool_call_count"] = self.tool_call_count
        if self.response_preview:
            payload["response_preview"] = self.response_preview
        return payload


_VALID_DECISIONS = ("approve", "block")
_VALID_PERMISSION_DECISIONS = ("allow", "ask", "deny")


@dataclass(frozen=True)
class HookOutput:
    """hook stdout 的解析结果（规格 §3.4）。

    字段名 ``continue_`` 是规避 Python 关键字 ``continue`` 的写法，
    wire 层（``from_wire``）仍读 ``"continue"`` 键。
    """

    continue_: bool = True
    decision: str = ""
    reason: str = ""
    additional_context: str = ""
    permission_decision: str = ""

    @classmethod
    def from_wire(cls, data: object) -> "HookOutput":
        """从 stdout 解析出的对象构造输出契约。

        容错策略：任何非法字段/类型都被忽略并回退默认值（默认 = 无干预），
        而不是抛错——hook 是增强能力，畸形输出不应让主流程感知异常（规格 §7）。
        """
        if not isinstance(data, dict):
            return cls()
        continue_ = data.get("continue")
        decision = data.get("decision")
        reason = data.get("reason")
        additional_context = data.get("additional_context")
        permission_decision = data.get("permission_decision")
        return cls(
            continue_=continue_ if isinstance(continue_, bool) else True,
            decision=decision if decision in _VALID_DECISIONS else "",
            reason=reason if isinstance(reason, str) else "",
            additional_context=(
                additional_context if isinstance(additional_context, str) else ""
            ),
            permission_decision=(
                permission_decision
                if permission_decision in _VALID_PERMISSION_DECISIONS
                else ""
            ),
        )

    def requests_block(self) -> bool:
        """是否请求阻断：``continue=false`` 或 ``decision=block``（规格 §3.4）。"""
        return (self.continue_ is False) or (self.decision == "block")


# 权限覆盖的严格度排序：deny > ask > allow（规格 §5.3，永远向更严格方向收敛）。
_PERMISSION_RANK = {"allow": 1, "ask": 2, "deny": 3}


@dataclass
class HookRunResult:
    """多个 hook 的聚合结果（规格 §5.2）。"""

    additional_contexts: list[str] = field(default_factory=list)
    block_requested: bool = False
    block_reason: str = ""
    permission_override: str | None = None
    continue_after_stop: bool = False
    executed_count: int = 0
    failures: list[str] = field(default_factory=list)

    def merge_output(self, output: HookOutput, event: HookEvent) -> None:
        """把单条 hook 的输出合并进聚合结果（规格 §5.3）。

        阻断字段按事件分流，避免接入层误读：
        - PreToolUse 的阻断 → ``block_requested``（拒绝工具执行）；
        - Stop 的阻断 → ``continue_after_stop``（阻止结束回合 = 续跑）；
        - SessionStart / PostToolUse 不支持阻断（规格 §2 能力表），请求被忽略。

        ``permission_override`` 只在 PreToolUse 收集：其他事件返回该字段没有
        语义（规格 §3.4），静默忽略比「意外生效」安全。
        """
        if output.additional_context:
            self.additional_contexts.append(output.additional_context)
        if output.requests_block():
            if event == HookEvent.PRE_TOOL_USE:
                self.block_requested = True
            elif event == HookEvent.STOP:
                self.continue_after_stop = True
            if event in (HookEvent.PRE_TOOL_USE, HookEvent.STOP):
                # reason 取第一个非空值：执行顺序确定，先到先得，不做覆盖。
                if output.reason and not self.block_reason:
                    self.block_reason = output.reason
        if event == HookEvent.PRE_TOOL_USE and output.permission_decision:
            current_rank = _PERMISSION_RANK.get(self.permission_override or "", 0)
            if _PERMISSION_RANK[output.permission_decision] > current_rank:
                self.permission_override = output.permission_decision
