"""Deterministic single-tool execution pipeline."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Callable

import core.app_context as app_context
from core.harness.tool_adapters import ToolAdapterSet
from core.harness.tool_contracts import resolve_tool
from core.harness.models import HarnessEventKind, JournalEventDraft
from core.tool_registry import ToolRegistry


TOOL_ERROR_PREFIX = "🚨"
PERMISSION_DENIED_MESSAGE = f"{TOOL_ERROR_PREFIX} 用户拒绝了执行该操作。"
PERMISSION_CANCELLED_MESSAGE = f"{TOOL_ERROR_PREFIX} 操作已取消。"
PLAN_MODE_DENIED_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 当前处于计划模式：只允许读取信息和制定计划，"
    "不允许执行任何修改/命令。请改用 plan_write 把操作步骤写入计划，"
    "并提示用户切换到「变更前确认」「自动编辑」或「完全访问」模式后再执行。"
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

REPEAT_CALL_BLOCKED_TEXT = (
    "🛑 检测到相同的工具调用已成功执行过 2 次，结果未发生变化。请改用其他方式推进任务。"
)

# 计划状态机工具不受重复调用抑制：plan_retry 设计为“首次返回根因分析提示、
# 再次调用强制重置”，若被 RepeatCallGuard 拦截第二次调用，调度器会把计划项
# 锁死在 failed 态（retry 被拒 → run_subagent 要求先重置 → 主 Agent 死锁）
_REPEAT_GUARD_EXEMPT_TOOLS = frozenset({
    "plan_write",
    "plan_update",
    "plan_read",
    "plan_retry",
})


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


@dataclass(frozen=True)
class ToolExecutionRecord:
    call_ordinal: int
    message: dict
    duration_ms: int


@dataclass(frozen=True)
class ToolBatch:
    parallel: bool
    calls: tuple[dict, ...]


@dataclass(frozen=True)
class _PreparedToolCall:
    call_ordinal: int
    raw: dict
    decoded: DecodedToolCall


@dataclass(frozen=True)
class _PreparedToolBatch:
    parallel: bool
    calls: tuple[_PreparedToolCall, ...]


class RepeatCallGuard:
    """Block a third successful call with the same canonical signature."""

    BLOCK_AFTER_SUCCESSES = 2

    def __init__(self):
        self._success_counts: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def signature(name, arguments_json):
        try:
            parsed = json.loads(arguments_json or "{}")
            canonical = json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except Exception:
            canonical = str(arguments_json or "")
        return f"{name}:{canonical}"

    def should_execute(self, name, arguments_json):
        signature = self.signature(name, arguments_json)
        with self._lock:
            return self._success_counts.get(signature, 0) < self.BLOCK_AFTER_SUCCESSES

    def record(self, name, arguments_json, ok):
        if not ok:
            return
        signature = self.signature(name, arguments_json)
        with self._lock:
            self._success_counts[signature] = (
                self._success_counts.get(signature, 0) + 1
            )


def _repeat_call_blocked_result(decoded: DecodedToolCall) -> dict:
    return {
        "role": "tool",
        "tool_call_id": decoded.call_id,
        "name": decoded.name,
        "content": REPEAT_CALL_BLOCKED_TEXT,
    }


def _submit_daemon_call(function, *args, **kwargs):
    future = Future()

    def runner():
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)

    threading.Thread(
        target=runner,
        daemon=True,
        name="ycode-tool-call",
    ).start()
    return future


def group_tool_batches(tool_calls: list[dict], registry: ToolRegistry) -> tuple[ToolBatch, ...]:
    batches: list[ToolBatch] = []
    for tool_call in tool_calls:
        name = tool_call["function"]["name"]
        try:
            parallel = bool(registry.get(name).parallel_safe)
        except KeyError:
            parallel = False
        if batches and batches[-1].parallel and parallel:
            previous = batches[-1]
            batches[-1] = ToolBatch(True, (*previous.calls, tool_call))
        else:
            batches.append(ToolBatch(parallel, (tool_call,)))
    return tuple(batches)


def build_tool_result_event_draft(
    step_id: str,
    tool_call: dict,
    record: ToolExecutionRecord,
    *,
    message_index: int,
    ok: bool,
) -> JournalEventDraft:
    raw_arguments = tool_call["function"].get("arguments", "{}")
    result_content = str(record.message.get("content", "") or "")
    return JournalEventDraft(
        HarnessEventKind.TOOL_RESULT_COMMITTED,
        step_id=step_id,
        message_index=message_index,
        tool_name=tool_call["function"]["name"],
        call_ordinal=record.call_ordinal,
        arguments_sha256=hashlib.sha256(raw_arguments.encode("utf-8")).hexdigest(),
        result_sha256=hashlib.sha256(result_content.encode("utf-8")).hexdigest(),
        duration_ms=max(0, int(record.duration_ms)),
        result_status="ok" if ok else "error",
    )


class ToolBatchExecutor:
    """Execute adjacent safe tools concurrently and return model-ordered records."""

    def __init__(
        self,
        pipeline: "ToolPipeline",
        *,
        repeat_guard: RepeatCallGuard | None = None,
        execute_decoded: Callable[[DecodedToolCall, ToolExecutionContext], dict]
        | None = None,
        event_payload: Callable[[DecodedToolCall], dict] | None = None,
        finished_event_payload: Callable[[DecodedToolCall, dict, int], dict]
        | None = None,
        result_ok: Callable[[str, dict], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        submit_call: Callable = _submit_daemon_call,
        poll_interval: float = 0.1,
        max_parallel: int = 4,
    ):
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self.pipeline = pipeline
        self.repeat_guard = repeat_guard or RepeatCallGuard()
        self.execute_decoded = execute_decoded or pipeline.execute_decoded
        self.event_payload = event_payload or self._default_event_payload
        self.finished_event_payload = (
            finished_event_payload or self._default_finished_event_payload
        )
        self.result_ok = result_ok or self._default_result_ok
        self.clock = clock
        self.submit_call = submit_call
        self.poll_interval = min(0.1, max(0.001, float(poll_interval)))
        self._parallel_slots = threading.BoundedSemaphore(max_parallel)

    @staticmethod
    def _default_event_payload(decoded):
        return {
            "id": decoded.call_id,
            "name": decoded.name,
            "arguments": decoded.arguments,
        }

    @staticmethod
    def _default_result_ok(_name, message):
        return not str(message.get("content", "") or "").startswith(TOOL_ERROR_PREFIX)

    @classmethod
    def _default_finished_event_payload(cls, decoded, message, duration_ms):
        payload = cls._default_event_payload(decoded)
        payload.update(
            ok=cls._default_result_ok(decoded.name, message),
            result=str(message.get("content", "") or ""),
            duration_ms=max(0, int(duration_ms)),
        )
        return payload

    @staticmethod
    def _emit(on_event, kind, payload):
        if on_event is not None:
            on_event(kind, payload)

    @staticmethod
    def _raise_if_cancelled(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt("用户终止了工具执行。")

    def _execute_timed(self, decoded, context):
        started_at = self.clock()
        message = self.execute_decoded(decoded, context)
        duration_ms = round((self.clock() - started_at) * 1000)
        return message, max(0, duration_ms)

    def _execute_guarded(self, decoded, context):
        guard_exempt = decoded.name in _REPEAT_GUARD_EXEMPT_TOOLS
        if not guard_exempt and not self.repeat_guard.should_execute(decoded.name, decoded.raw_arguments):
            return _repeat_call_blocked_result(decoded), 0
        message, duration_ms = self._execute_timed(decoded, context)
        if not guard_exempt:
            self.repeat_guard.record(
                decoded.name,
                decoded.raw_arguments,
                ok=self.result_ok(decoded.name, message),
            )
        return message, duration_ms

    def _execute_parallel(self, decoded, context):
        with self._parallel_slots:
            return self._execute_guarded(decoded, context)

    def _wait_for(self, future, cancel_event):
        while not future.done():
            self._raise_if_cancelled(cancel_event)
            try:
                future.result(timeout=self.poll_interval)
            except FutureTimeoutError:
                pass
        self._raise_if_cancelled(cancel_event)
        return future.result()

    def prepare(self, tool_calls: list[dict], context: ToolExecutionContext):
        decoded_by_id = {
            id(tool_call): decode_tool_call(tool_call) for tool_call in tool_calls
        }
        ordinal_by_id = {
            id(tool_call): ordinal for ordinal, tool_call in enumerate(tool_calls)
        }
        return tuple(
            _PreparedToolBatch(
                batch.parallel,
                tuple(
                    _PreparedToolCall(
                        ordinal_by_id[id(tool_call)],
                        tool_call,
                        decoded_by_id[id(tool_call)],
                    )
                    for tool_call in batch.calls
                ),
            )
            for batch in group_tool_batches(tool_calls, context.registry)
        )

    def begin_batch(self, batch, context, on_event):
        self._raise_if_cancelled(context.cancel_event)
        for prepared_call in batch.calls:
            self._emit(
                on_event,
                "tool_requested",
                self.event_payload(prepared_call.decoded),
            )
        for prepared_call in batch.calls:
            self._emit(
                on_event,
                "tool_started",
                self.event_payload(prepared_call.decoded),
            )

    def execute_batch(self, batch, context, on_event):
        self._raise_if_cancelled(context.cancel_event)
        if batch.parallel and len(batch.calls) > 1:
            futures = [
                self.submit_call(
                    self._execute_parallel,
                    prepared_call.decoded,
                    context,
                )
                for prepared_call in batch.calls
            ]
            results = [
                self._wait_for(future, context.cancel_event) for future in futures
            ]
        else:
            results = []
            for prepared_call in batch.calls:
                self._raise_if_cancelled(context.cancel_event)
                results.append(
                    self._execute_guarded(prepared_call.decoded, context)
                )

        records = []
        for prepared_call, (message, duration_ms) in zip(batch.calls, results):
            record = ToolExecutionRecord(
                prepared_call.call_ordinal,
                message,
                duration_ms,
            )
            records.append(record)
            self._emit(
                on_event,
                "tool_finished",
                self.finished_event_payload(
                    prepared_call.decoded,
                    message,
                    duration_ms,
                ),
            )
        return tuple(records)

    def execute(
        self,
        tool_calls: list[dict],
        context: ToolExecutionContext,
        on_event: Callable[[str, dict], None] | None,
    ) -> tuple[ToolExecutionRecord, ...]:
        records: list[ToolExecutionRecord] = []

        for batch in self.prepare(tool_calls, context):
            self.begin_batch(batch, context, on_event)
            records.extend(self.execute_batch(batch, context, on_event))

        return tuple(sorted(records, key=lambda record: record.call_ordinal))


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
        if not snapshot:
            # 计划在同一回合内被清空（plan_write 成功标记了计划回合后又被清空）：
            # 门禁失去意义，必须放行，否则主 Agent 陷入“没有计划项却被要求
            # 提供 plan_item_id”的死锁（execute_command 全被拦截、无任何提示可跟）
            return None
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

    @staticmethod
    def _decode_error_message(tool_call, parse_error) -> dict:
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

    def execute_decoded(self, decoded: DecodedToolCall, context) -> dict:
        _raise_if_cancelled(context.cancel_event)
        constrained = self._plan_constraint(decoded, context)
        if constrained is not None:
            return _tool_message(decoded, constrained)

        # 契约解析（严格注册表 + MCP 回退）：invariant 异常（如损坏的注册表）
        # 必须显式传播，不得吞成工具错误；仅"未找到"转为友好提示。
        try:
            resolved = resolve_tool(decoded.name, context.registry, context.mcp_manager)
        except KeyError:
            return _tool_message(
                decoded, f"{TOOL_ERROR_PREFIX} 未找到名为 {decoded.name} 的工具！"
            )

        # 执行段（状态播报/权限询问/适配器 IO）与旧 _execute_single_tool_call
        # 的兜底边界一致：单工具系统级异常（如权限终端 input 的 OSError）转成
        # 工具错误结果返回，绝不击穿外层循环终结整个回合；KeyboardInterrupt
        # （取消路径）不属于 Exception，仍向上传递。
        try:
            self.adapters.announce(
                decoded.name, decoded.arguments, context.event_callback
            )
            denied = self._permission_decision(resolved, decoded, context)
            if denied is not None:
                return _tool_message(decoded, denied)
            _raise_if_cancelled(context.cancel_event)

            result = self.adapters.for_call(decoded.call_id).execute(
                resolved, decoded.arguments, context
            )
            content = result.to_message_content()
        except Exception as tool_err:
            content = (
                f"{TOOL_ERROR_PREFIX} 调用工具 {decoded.name} 时发生系统内部错误：{tool_err}"
            )
        return _tool_message(decoded, self.truncate_result(decoded.name, content))

    def execute_one(self, tool_call, context) -> dict:
        try:
            decoded = decode_tool_call(tool_call)
        except ValueError as parse_error:
            return self._decode_error_message(tool_call, parse_error)
        return self.execute_decoded(decoded, context)
