"""Deterministic single-tool execution pipeline."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

import core.app_context as app_context
from core.harness.tool_adapters import ToolAdapterSet
from core.harness.tool_contracts import resolve_tool
from core.tool_registry import ToolRegistry


TOOL_ERROR_PREFIX = "🚨"
PERMISSION_DENIED_MESSAGE = f"{TOOL_ERROR_PREFIX} 用户拒绝了执行该操作。"
PERMISSION_CANCELLED_MESSAGE = f"{TOOL_ERROR_PREFIX} 操作已取消。"
PLAN_MODE_DENIED_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 当前处于计划模式：只允许读取信息和制定计划，"
    "不允许执行任何修改/命令。请改用 plan_write 把操作步骤写入计划，"
    "并提示用户切换到「变更前确认」或「完全访问」模式后再执行。"
)
PERMISSION_SYSTEM_ERROR_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 权限询问系统异常，操作未能完成。"
    f"这不是用户拒绝，请稍后重试或检查权限组件状态。"
)
PATH_VALIDATION_ERROR_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 无法验证路径是否位于当前工作区，已拒绝执行。"
)
_PLAN_MODE_ALLOWED_TOOLS = frozenset({
    "plan_write", "plan_read", "plan_update", "plan_retry", "ask_user",
})
_PLAN_BYPASS_ERROR_PREFIX = "计划已创建：工具"
_PLAN_ROOT_CONTROL_TOOLS = frozenset({
    "plan_write", "plan_update", "plan_read", "plan_retry", "run_subagent", "ask_user",
})
_PLAN_READ_ONLY_TOOLS = frozenset({
    "read_file", "list_directory", "search_code", "find_definition", "glob_files",
    "fetch_url", "search_news", "get_weather", "manage_task",
})
_PLAN_BOUND_ORCHESTRATORS = frozenset({"run_subagent", "ask_user"})

_plan_turn_lock = threading.Lock()
_plan_turn_started_at: dict[str, float] = {}


@dataclass(frozen=True)
class ToolExecutionContext:
    event_callback: Callable | None
    parent_session: str | None
    parent_turn_id: str | None
    cancel_event: threading.Event | None
    allow_subagents: bool
    permission_owner: str | None
    permission_source: str
    permission_checker: Callable
    registry: ToolRegistry
    mcp_manager: object


@dataclass(frozen=True)
class DecodedToolCall:
    call_id: str
    name: str
    arguments: dict
    raw_arguments: str


def mark_plan_turn(turn_id):
    if not turn_id:
        return
    now = time.monotonic()
    with _plan_turn_lock:
        _plan_turn_started_at[str(turn_id)] = now
        cutoff = now - 3600
        for stale_id, started_at in list(_plan_turn_started_at.items()):
            if started_at < cutoff:
                _plan_turn_started_at.pop(stale_id, None)


def is_plan_turn(turn_id):
    if not turn_id:
        return False
    with _plan_turn_lock:
        return str(turn_id) in _plan_turn_started_at


def has_pending_plan(session_id):
    if not session_id:
        return False
    from core.plan import get_default_plan_manager

    plan = get_default_plan_manager()
    if plan.session_id != str(session_id):
        return False
    return any(
        item.get("status") in {"pending", "in_progress"}
        for item in plan.snapshot()
    )


def _escape_control_chars_in_json_strings(text):
    escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    output = []
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                output.append(character)
                escaped = False
            elif character == "\\":
                output.append(character)
                escaped = True
            elif character == '"':
                output.append(character)
                in_string = False
            elif character in escapes:
                output.append(escapes[character])
            else:
                output.append(character)
        else:
            output.append(character)
            if character == '"':
                in_string = True
    return "".join(output)


def _repair_truncated_json(raw):
    if not raw or not raw.strip():
        return raw
    in_string = False
    escaped = False
    stack = []
    for character in raw:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        else:
            if character == '"':
                in_string = True
            elif character in "[{":
                stack.append(character)
            elif character == "]" and stack and stack[-1] == "[":
                stack.pop()
            elif character == "}" and stack and stack[-1] == "{":
                stack.pop()
    repaired = raw.rstrip()
    if in_string:
        if repaired.endswith("\\"):
            repaired = repaired[:-1]
        repaired += '"'
    repaired = repaired.rstrip()
    while repaired.endswith(","):
        repaired = repaired[:-1].rstrip()
    if repaired.endswith(":"):
        repaired += ' ""'
    closing = {"[": "]", "{": "}"}
    while stack:
        repaired += closing[stack.pop()]
    return repaired


def _tool_call_arguments(tool_call):
    raw_arguments = tool_call["function"].get("arguments", "{}")
    if not isinstance(raw_arguments, str):
        raise ValueError("工具参数 JSON 必须是字符串")
    try:
        arguments = json.loads(raw_arguments)
    except ValueError as direct_error:
        try:
            arguments = json.loads(_escape_control_chars_in_json_strings(raw_arguments))
        except ValueError:
            try:
                arguments = json.loads(_repair_truncated_json(raw_arguments))
            except ValueError as repair_error:
                raise ValueError(f"工具参数 JSON 解析失败：{repair_error}") from direct_error
    if not isinstance(arguments, dict):
        raise ValueError("工具参数 JSON 必须是对象")
    return arguments


def decode_tool_call(tool_call: dict) -> DecodedToolCall:
    raw_arguments = tool_call["function"].get("arguments", "{}")
    return DecodedToolCall(
        call_id=tool_call["id"],
        name=tool_call["function"]["name"],
        arguments=_tool_call_arguments(tool_call),
        raw_arguments=raw_arguments,
    )


def _raise_if_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise KeyboardInterrupt("用户终止了工具执行。")


def _tool_message(decoded, content):
    return {
        "role": "tool",
        "tool_call_id": decoded.call_id,
        "name": decoded.name,
        "content": content,
    }


class ToolPipeline:
    def __init__(
        self,
        adapters: ToolAdapterSet | None = None,
        *,
        permission_error_type=(),
        truncate_result: Callable[[str, str], str] | None = None,
        plan_turn_checker: Callable[[str | None], bool] = is_plan_turn,
        pending_plan_checker: Callable[[str | None], bool] = has_pending_plan,
    ):
        self.adapters = adapters or ToolAdapterSet()
        self.permission_error_type = permission_error_type
        self.truncate_result = truncate_result or (lambda _name, content: content)
        self.plan_turn_checker = plan_turn_checker
        self.pending_plan_checker = pending_plan_checker

    def _plan_constraint(self, decoded, context):
        if not context.allow_subagents or not (
            self.plan_turn_checker(context.parent_turn_id)
            or self.pending_plan_checker(context.parent_session)
        ):
            return None
        arguments = decoded.arguments
        name = decoded.name
        plan_item_id = str(arguments.get("plan_item_id", "") or "").strip()
        if name == "plan_write" and self.plan_turn_checker(context.parent_turn_id):
            from core.plan import get_default_plan_manager

            if any(item.get("status") != "pending" for item in get_default_plan_manager().snapshot()):
                return "计划执行约束：不能重建已有执行记录，请使用 /plan retry <id> 重试失败项。"
        if name in _PLAN_BOUND_ORCHESTRATORS and not plan_item_id:
            return (
                f"{_PLAN_BYPASS_ERROR_PREFIX} {name} 必须填写 plan_item_id，"
                "不能执行未归属计划项的任务。"
            )
        if name in _PLAN_READ_ONLY_TOOLS or name in _PLAN_ROOT_CONTROL_TOOLS:
            return None

        from core.plan import get_default_plan_manager

        plan = get_default_plan_manager()
        ready = [item for item in plan.ready_items() if item.get("kind") != "input_preparation"]
        snapshot = plan.snapshot()
        in_progress = any(item.get("status") == "in_progress" for item in snapshot)
        stalled = [
            item for item in snapshot
            if item.get("status") in {"failed", "blocked", "cancelled"}
        ]
        retry_exhausted = bool(stalled) and all(
            int(item.get("attempts", 0)) >= 2 for item in stalled
        )
        if not ready and not in_progress and retry_exhausted:
            return None
        ready_hints = ""
        if ready:
            hints = ", ".join(f"'{item['id']}'({item['subject']})" for item in ready[:5])
            ready_hints = f" 当前可执行的计划项: [{hints}]。"
        return (
            f"{_PLAN_BYPASS_ERROR_PREFIX} {name} 必须通过 run_subagent(plan_item_id=...) 执行，"
            f"不能由主 Agent 直接绕过计划调用。{ready_hints}"
            "（若计划项多次失败（attempts>=2）全部停滞后，主 Agent 可直接调用工具修复。）"
        )

    def _permission_decision(self, resolved, decoded, context):
        if not resolved.spec.requires_permission:
            return None
        name = decoded.name
        arguments = decoded.arguments
        if app_context.get_permission_mode() == "plan" and name not in _PLAN_MODE_ALLOWED_TOOLS:
            return PLAN_MODE_DENIED_MESSAGE

        defer_to_sandbox = False
        if name in {"write_file", "edit_file"}:
            try:
                defer_to_sandbox = self.adapters.is_outside_workspace(
                    arguments.get("file_path", "")
                )
            except Exception:
                return PATH_VALIDATION_ERROR_MESSAGE
        if defer_to_sandbox:
            return None
        try:
            approved = context.permission_checker(
                name,
                arguments,
                event_callback=context.event_callback,
                permission_owner=context.permission_owner,
                parent_turn_id=context.parent_turn_id,
                permission_source=context.permission_source,
                cancel_event=context.cancel_event,
            )
        except self.permission_error_type:
            return PERMISSION_SYSTEM_ERROR_MESSAGE
        if approved:
            return None
        if context.cancel_event is not None and context.cancel_event.is_set():
            return PERMISSION_CANCELLED_MESSAGE
        return PERMISSION_DENIED_MESSAGE

    def execute_one(self, tool_call, context) -> dict:
        try:
            decoded = decode_tool_call(tool_call)
        except ValueError as parse_error:
            name = tool_call["function"]["name"]
            return {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "name": name,
                "content": (
                    f"工具参数 JSON 解析失败：{parse_error}。"
                    "请重新调用该工具，并确保 arguments 是合法、完整、未混入其他文本的 JSON。"
                ),
            }

        _raise_if_cancelled(context.cancel_event)
        constrained = self._plan_constraint(decoded, context)
        if constrained is not None:
            return _tool_message(decoded, constrained)

        try:
            resolved = resolve_tool(decoded.name, context.registry, context.mcp_manager)
        except KeyError:
            return _tool_message(
                decoded, f"{TOOL_ERROR_PREFIX} 未找到名为 {decoded.name} 的工具！"
            )

        self.adapters.announce(decoded.name, decoded.arguments, context.event_callback)
        denied = self._permission_decision(resolved, decoded, context)
        if denied is not None:
            return _tool_message(decoded, denied)
        _raise_if_cancelled(context.cancel_event)

        result = self.adapters.for_call(decoded.call_id).execute(
            resolved, decoded.arguments, context
        )
        content = self.truncate_result(decoded.name, result.to_message_content())
        return _tool_message(decoded, content)
