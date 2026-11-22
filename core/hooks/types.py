from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

DEFAULT_TIMEOUT_MS = 60000
MAX_TIMEOUT_MS = 300000

REDACTED_PLACEHOLDER = "[redacted]"


class HookEvent(str, Enum):

    SESSION_START = "SessionStart"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"


TOOL_EVENTS: frozenset[HookEvent] = frozenset(
    {HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE}
)


@dataclass(frozen=True)
class HookRegistration:

    event: HookEvent
    matcher: str
    command: str
    timeout_ms: int
    status_message: str
    source_path: str
    scope: Literal["user", "project"]
    digest: str
    # 启停标记：默认 False（带默认值保持向后兼容）；不参与 digest——启停不改变
    # 执行内容，指纹只需绑定「配置内容」。执行侧默认加载仍然跳过 disabled 条目，
    # 仅管理列表（include_disabled=True）保留它们以便在管理页重新启用。
    disabled: bool = False


@dataclass(frozen=True)
class HookInput:

    hook_event_name: str
    session_id: str
    turn_id: str
    cwd: str
    permission_mode: str
    timestamp: str
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
            payload["tool_response"] = self.tool_result_preview
        if self.tool_call_count:
            payload["tool_call_count"] = self.tool_call_count
        if self.response_preview:
            payload["response_preview"] = self.response_preview
        return payload


_VALID_DECISIONS = ("approve", "block")
_VALID_PERMISSION_DECISIONS = ("allow", "ask", "deny")


@dataclass(frozen=True)
class HookOutput:

    continue_: bool = True
    decision: str = ""
    reason: str = ""
    additional_context: str = ""
    permission_decision: str = ""

    @classmethod
    def from_wire(cls, data: object) -> "HookOutput":
        if not isinstance(data, dict):
            return cls()
        continue_ = data.get("continue")
        decision = data.get("decision")
        reason = data.get("reason")
        additional_context = data.get("additional_context")
        permission_decision = data.get("permission_decision")
        if not isinstance(reason, str) or not reason:
            alias = data.get("stopReason")
            if isinstance(alias, str):
                reason = alias
        if not isinstance(additional_context, str) or not additional_context:
            alias = data.get("additionalContext")
            if isinstance(alias, str):
                additional_context = alias
        if not isinstance(permission_decision, str) or permission_decision not in _VALID_PERMISSION_DECISIONS:
            alias = data.get("permissionDecision")
            if isinstance(alias, str) and alias in _VALID_PERMISSION_DECISIONS:
                permission_decision = alias
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
        return (self.continue_ is False) or (self.decision == "block")


_PERMISSION_RANK = {"allow": 1, "ask": 2, "deny": 3}


@dataclass
class HookRunResult:

    additional_contexts: list[str] = field(default_factory=list)
    block_requested: bool = False
    block_reason: str = ""
    permission_override: str | None = None
    continue_after_stop: bool = False
    executed_count: int = 0
    failures: list[str] = field(default_factory=list)

    def merge_output(self, output: HookOutput, event: HookEvent) -> None:
        if output.additional_context:
            self.additional_contexts.append(output.additional_context)
        if output.requests_block():
            if event == HookEvent.PRE_TOOL_USE:
                self.block_requested = True
            elif event == HookEvent.STOP:
                self.continue_after_stop = True
            if event in (HookEvent.PRE_TOOL_USE, HookEvent.STOP):
                if output.reason and not self.block_reason:
                    self.block_reason = output.reason
        if event == HookEvent.PRE_TOOL_USE and output.permission_decision:
            current_rank = _PERMISSION_RANK.get(self.permission_override or "", 0)
            if _PERMISSION_RANK[output.permission_decision] > current_rank:
                self.permission_override = output.permission_decision
