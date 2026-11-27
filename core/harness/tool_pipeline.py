from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Callable

import core.app_context as app_context
from core.harness.tool_adapters import ToolAdapterSet
from core.harness.tool_contracts import resolve_tool
from core.harness.models import HarnessEventKind, JournalEventDraft
from core.tool_registry import ToolRegistry
from core.command_policy import classify_command_risk
from core.permission_policy import PermissionRisk

_LOGGER = logging.getLogger(__name__)


TOOL_ERROR_PREFIX = "🚨"
SECRETARY_READONLY_DENIED_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 当前是秘书会话，只允许检索对话 / 派活 / 跳转以及只读查询。"
    "秘书会话不写项目文件；可以让我派活给某个会话去改。"
)
PERMISSION_DENIED_MESSAGE = f"{TOOL_ERROR_PREFIX} 用户拒绝了执行该操作。"
JEV_AUTO_DENIED_MESSAGE = f"{TOOL_ERROR_PREFIX} 自动决策（JEV）评估风险后拒绝了执行该操作。"
PERMISSION_CANCELLED_MESSAGE = f"{TOOL_ERROR_PREFIX} 操作已取消。"
PLAN_MODE_DENIED_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 当前处于计划模式：只允许读取信息、执行明确的只读查询和制定计划，"
    "不允许执行修改或删除操作。请改用 plan_write 把操作步骤写入计划，"
    "并提示用户切换到「变更前确认」「自动编辑」「自动决策」或「完全访问」模式后再执行。"
)
DANGEROUS_COMMAND_DENIED_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 危险命令已被安全策略拒绝。"
    "请改用范围明确、可恢复的操作。"
    "若这是文档/文件内容（而非要执行的命令），请改用文件写入工具（write_file）保存。"
)
PERMISSION_SYSTEM_ERROR_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 权限询问系统异常，操作未能完成。"
    f"这不是用户拒绝，请稍后重试或检查权限组件状态。"
)
PATH_VALIDATION_ERROR_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 无法验证路径是否位于当前工作区，已拒绝执行。"
)
_PLAN_BYPASS_ERROR_PREFIX = "计划已创建：工具"
_PLAN_ROOT_CONTROL_TOOLS = frozenset({
    "plan_write", "plan_update", "plan_read", "plan_retry", "plan_finish",
    "plan_resume_takeover",
    "run_subagent", "ask_user",
})
_PLAN_READ_ONLY_TOOLS = frozenset({
    "read_file", "list_directory", "search_code", "find_definition", "glob_files",
    "fetch_url", "search_news", "get_weather", "manage_task",
    # 项目画像写入（特性 1）：只往 ~/.ycode/memory/projects/ 追加记忆，不改
    # 工作区文件，计划模式下按只读口径放行。
    "remember_decision",
})
_PLAN_BOUND_ORCHESTRATORS = frozenset({"run_subagent", "ask_user"})

_plan_turn_lock = threading.Lock()
_plan_turn_started_at: dict[str, float] = {}

REPEAT_CALL_BLOCKED_TEXT = (
    "🚨 相同工具调用已经成功执行过 2 次，本次调用未执行。"
    "请检查已有结果或改变执行方案。"
    "若该工具是 computer_action，窗口可能已移动，请先 list_elements 或 observe 重建状态再操作。"
)

# 旧子 Agent 执行租约 fencing：超时/取消/失败后，旧子代理的延迟工具调用
# （permission_owner=其 task_id）必须立即取消，防止已由主 Agent 接管的
# 计划项被旧执行者继续改写。查询工具同样拦截，不得继续访问外部状态。
STALE_EXECUTION_LEASE_TEXT = (
    "🚨 该子代理任务已被终止（超时或取消），执行租约已失效，本次工具调用已取消。"
    "计划项将由主 Agent 接管，旧子代理不得继续执行任何工具。"
)

# 计划状态机工具不受重复调用抑制：plan_retry 设计为“首次返回根因分析提示、
# 再次调用强制重置”，若被 RepeatCallGuard 拦截第二次调用，调度器会把计划项
# 锁死在 failed 态（retry 被拒 → run_subagent 要求先重置 → 主 Agent 死锁）
_REPEAT_GUARD_EXEMPT_TOOLS = frozenset({
    "plan_write",
    "plan_update",
    "plan_read",
    "plan_retry",
    "plan_resume_takeover",
    # 收尾可能先失败（验证未过）后成功（修正后再收尾）：豁免重复抑制，
    # 避免第三次同参数 plan_finish 被拦截导致接管项无法收尾
    "plan_finish",
    "lan_peer_poll",
})


_REPEAT_GUARD_EXEMPT_MANAGE_TASK_ACTIONS = frozenset({"status", "list", "wait"})

# follow-up 私有载荷在 message dict 上的内部暂存键（设计 19.3）：仅存在于
# execute_decoded 返回值与 ToolBatchExecutor 构造 record 之间；构造 record 时
# 剥离到 ToolExecutionRecord.followup_messages，绝不进入 Provider/tool message。
_FOLLOWUP_MESSAGES_KEY = "_ycode_followup_messages"

# 电脑操控无副作用/纯观察类 action 不消耗重复额度（2026-09-06 提速计划 Task 5）：
# 逐屏滚动阅读、重新观察与等待是合法重复（会话 147 实测被误拦 10 次，每次白烧
# 一轮模型往返）。点击/输入/sequence 等有副作用动作不豁免：同参数重复点击可能
# 造成重复发送；真死循环由 controller 的 no-progress 熔断（3 次画面不变停止）兜底。
_REPEAT_GUARD_EXEMPT_COMPUTER_KINDS = frozenset(
    {"observe", "scroll", "wait", "move", "list_surfaces", "list_elements"}
)


def repeat_guard_exempt(name: str, arguments: dict) -> bool:
    if name in _REPEAT_GUARD_EXEMPT_TOOLS:
        return True
    if name == "manage_task":
        action = str((arguments or {}).get("action", "")).strip().lower()
        return action in _REPEAT_GUARD_EXEMPT_MANAGE_TASK_ACTIONS
    if name == "computer_action":
        kind = str((arguments or {}).get("kind", "") or "").strip().lower()
        return kind in _REPEAT_GUARD_EXEMPT_COMPUTER_KINDS
    return False


def _hook_permission_decision(authorization) -> str:
    if isinstance(authorization, bool):
        return "allow" if authorization else "deny"
    if not bool(getattr(authorization, "allowed", False)):
        return "deny"
    reason = str(getattr(authorization, "reason", "") or "")
    if reason.startswith("allow_"):
        return "ask"
    return "allow"


def _tool_message_with_hook_contexts(decoded, content, hook_result, event_name):
    from core.hooks.lifecycle import contexts_to_messages

    message = _tool_message(decoded, content)
    contexts = ()
    if hook_result is not None:
        contexts = tuple(getattr(hook_result, "additional_contexts", ()) or ())
    followups = contexts_to_messages(event_name, contexts)
    if followups:
        message[_FOLLOWUP_MESSAGES_KEY] = followups
    return message


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
    # Turn 冻结的电脑操控执行上下文（capability token + owner lease + run id，
    # 设计 19.2）。默认 None：普通回合行为与序列化完全不变；固定 computer
    # 适配器只在携带 lease 时执行动作，否则返回稳定关闭错误。
    computer_lease: object | None = None
    computer_native_selection: object | None = None
    computer_capability: object | None = None
    hook_runner: object | None = None
    # 断点续跑记录（S1-b）：可选的事件落库回调，签名 (kind: str, facts: dict) -> None。
    # facts 只含哈希与元数据，绝不含参数原文或结果正文。默认 None 时事件流、
    # 工具执行顺序与请求字节均与改造前完全一致。
    journal_event_sink: Callable[[str, dict], None] | None = None
    harness_step: object | None = None


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
    followup_messages: tuple[dict, ...] = ()


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


_TOOL_WORKER_POOL_LOCK = threading.Lock()
_TOOL_WORKER_POOL = None
_ORCHESTRATION_WORKER_POOL = None
_ORCHESTRATION_TOOL_NAMES = frozenset({"run_subagent", "ask_user"})


def _get_tool_worker_pool():
    """获取复用的有界线程池，避免高并发只读批次反复创建/销毁 OS 线程。"""
    global _TOOL_WORKER_POOL
    if _TOOL_WORKER_POOL is not None:
        return _TOOL_WORKER_POOL
    with _TOOL_WORKER_POOL_LOCK:
        if _TOOL_WORKER_POOL is None:
            from concurrent.futures import ThreadPoolExecutor

            _TOOL_WORKER_POOL = ThreadPoolExecutor(
                max_workers=16,
                thread_name_prefix="ycode-tool-worker",
            )
        return _TOOL_WORKER_POOL


def _get_orchestration_worker_pool():
    """隔离阻塞等待型的编排工具线程池，避免与子代理内部的叶子工具争夺同一池名额。"""
    global _ORCHESTRATION_WORKER_POOL
    if _ORCHESTRATION_WORKER_POOL is not None:
        return _ORCHESTRATION_WORKER_POOL
    with _TOOL_WORKER_POOL_LOCK:
        if _ORCHESTRATION_WORKER_POOL is None:
            from concurrent.futures import ThreadPoolExecutor

            _ORCHESTRATION_WORKER_POOL = ThreadPoolExecutor(
                max_workers=16,
                thread_name_prefix="ycode-orch-worker",
            )
        return _ORCHESTRATION_WORKER_POOL


def _submit_on_pool(pool, function, *args, **kwargs):
    from core.workspace_context import get_workdir, workspace_scope

    workdir = get_workdir()
    thread_session = app_context.get_thread_session()
    thread_turn_source = app_context.get_thread_turn_source()
    thread_permission_mode = app_context.get_thread_permission_mode()
    from core.plan import get_plan_route, set_current_plan_session as _bind_plan_route

    plan_session_id, plan_storage_dir = get_plan_route()

    def runner():
        cur_thread = threading.current_thread()
        prev_thread_name = cur_thread.name
        prev_session = app_context.get_thread_session()
        prev_turn_source = app_context.get_thread_turn_source()
        prev_plan_session, prev_plan_dir = get_plan_route()
        cur_thread.name = "ycode-tool-call"
        try:
            with workspace_scope(workdir):
                if thread_session:
                    app_context.set_thread_session(thread_session)
                if thread_turn_source:
                    app_context.set_thread_turn_source(thread_turn_source)
                if plan_session_id:
                    _bind_plan_route(plan_session_id, storage_dir=plan_storage_dir)
                permission_scope = (
                    app_context.permission_mode_scope(thread_permission_mode)
                    if thread_permission_mode
                    else None
                )
                if permission_scope is not None:
                    permission_scope.__enter__()
                try:
                    return function(*args, **kwargs)
                finally:
                    if permission_scope is not None:
                        permission_scope.__exit__(None, None, None)
        finally:
            cur_thread.name = prev_thread_name
            try:
                app_context.set_thread_session(prev_session)
            except Exception:
                pass
            try:
                app_context.set_thread_turn_source(prev_turn_source)
            except Exception:
                pass
            try:
                _bind_plan_route(prev_plan_session, storage_dir=prev_plan_dir)
            except Exception:
                pass

    return pool.submit(runner)


def _submit_daemon_call(function, *args, **kwargs):
    return _submit_on_pool(_get_tool_worker_pool(), function, *args, **kwargs)


def _submit_orchestration_call(function, *args, **kwargs):
    return _submit_on_pool(_get_orchestration_worker_pool(), function, *args, **kwargs)


def _legacy_group_tool_batches(
    tool_calls: list[dict], registry: ToolRegistry
) -> tuple[ToolBatch, ...]:
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


def group_tool_batches(tool_calls: list[dict], registry: ToolRegistry) -> tuple[ToolBatch, ...]:
    # 延迟导入：ToolBatch 定义在本模块，若在模块顶层互相导入会形成循环。
    from core.harness.tool_scheduler import plan_tool_batches

    return plan_tool_batches(
        tool_calls,
        registry,
        max_concurrency=max(1, len(tool_calls)),
    )


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


def _journal_text_content(message) -> str:
    try:
        content = message.get("content")
    except Exception:
        return ""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(content)


def _journal_facts_for_call(prepared_call) -> dict:
    """构造只含哈希与元数据的事实字典，绝不包含参数原文。"""
    decoded = getattr(prepared_call, "decoded", None)
    name = str(getattr(decoded, "name", "") or "")
    raw_arguments = str(getattr(decoded, "raw_arguments", "") or "")
    if not raw_arguments:
        try:
            raw_arguments = json.dumps(
                getattr(decoded, "arguments", {}) or {},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except Exception:
            raw_arguments = ""
    arguments_sha256 = None
    if raw_arguments:
        arguments_sha256 = hashlib.sha256(raw_arguments.encode("utf-8")).hexdigest()
    return {
        "tool_name": name,
        "call_ordinal": int(getattr(prepared_call, "call_ordinal", -1)),
        "arguments_sha256": arguments_sha256,
    }


class ToolBatchExecutor:

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
    def _emit_journal(context, kind, facts):
        """把脱敏事实交给可选的 journal sink；sink 异常绝不影响工具执行。"""
        sink = getattr(context, "journal_event_sink", None)
        if sink is None:
            return
        try:
            sink(kind, facts)
        except Exception:
            _LOGGER.warning("journal event sink failed for %s", kind, exc_info=True)

    @staticmethod
    def _raise_if_cancelled(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt("用户终止了工具执行。")

    def _emit_started_journal(self, context, prepared_call):
        facts = _journal_facts_for_call(prepared_call)
        facts["started"] = True
        self._emit_journal(context, "tool_started", facts)

    def _emit_finished_journal(self, context, prepared_call, message, duration_ms):
        facts = _journal_facts_for_call(prepared_call)
        facts["result_sha256"] = hashlib.sha256(
            _journal_text_content(message).encode("utf-8")
        ).hexdigest()
        facts["duration_ms"] = max(0, int(duration_ms))
        facts["result_status"] = (
            "ok" if self.result_ok(prepared_call.decoded.name, message) else "error"
        )
        self._emit_journal(context, "tool_finished", facts)

    def _execute_timed(self, decoded, context):
        started_at = self.clock()
        # 绑定本次调用 id：run_subagent 创建子代理时据此把任务归属到派生它的
        # 调用上，GUI 卡片才能只展示"自己正在执行的这个"子代理。
        # 并行工具线程在提交时未必继承父线程的 thread_session，这里补齐上下文，
        # 确保秘书跨项目只读免确认等依赖 get_thread_session() 的判定一致。
        parent_session = getattr(context, "parent_session", None)
        session_scope = (
            app_context.thread_session_scope(parent_session)
            if parent_session and not app_context.get_thread_session()
            else nullcontext()
        )
        with session_scope:
            with app_context.tool_call_scope(decoded.call_id):
                message = self.execute_decoded(decoded, context)
        duration_ms = round((self.clock() - started_at) * 1000)
        return message, max(0, duration_ms)

    def _execute_guarded(self, prepared_call, context):
        decoded = prepared_call.decoded
        # 进入即写 STARTED：执行中途崩溃（进程被杀/抛错）时，journal 里
        # 「有 STARTED 无 FINISHED」就是不确定态，续跑据此归 BLOCK，绝不重放。
        self._emit_started_journal(context, prepared_call)
        guard_exempt = repeat_guard_exempt(decoded.name, decoded.arguments)
        if not guard_exempt and not self.repeat_guard.should_execute(decoded.name, decoded.raw_arguments):
            # 被重复调用守卫拦下也成对写 FINISHED：它确实"跑过"，不能留成
            # 不确定态，否则续跑会无谓 BLOCK 该只读调用。
            blocked = _repeat_call_blocked_result(decoded)
            self._emit_finished_journal(context, prepared_call, blocked, 0)
            return blocked, 0
        message, duration_ms = self._execute_timed(decoded, context)
        if not guard_exempt:
            self.repeat_guard.record(
                decoded.name,
                decoded.raw_arguments,
                ok=self.result_ok(decoded.name, message),
            )
        self._emit_finished_journal(context, prepared_call, message, duration_ms)
        return message, duration_ms

    def _execute_parallel(self, prepared_call, context):
        with self._parallel_slots:
            return self._execute_guarded(prepared_call, context)

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
            def _submit_parallel_call(prepared_call):
                if (
                    self.submit_call is _submit_daemon_call
                    and prepared_call.decoded.name in _ORCHESTRATION_TOOL_NAMES
                ):
                    return _submit_orchestration_call(
                        self._execute_parallel,
                        prepared_call,
                        context,
                    )
                return self.submit_call(
                    self._execute_parallel,
                    prepared_call,
                    context,
                )

            futures = [
                _submit_parallel_call(prepared_call)
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
                    self._execute_guarded(prepared_call, context)
                )

        records = []
        for prepared_call, (message, duration_ms) in zip(batch.calls, results):
            followup_messages = tuple(
                message.pop(_FOLLOWUP_MESSAGES_KEY, ()) or ()
            )
            record = ToolExecutionRecord(
                prepared_call.call_ordinal,
                message,
                duration_ms,
                followup_messages=followup_messages,
            )
            records.append(record)
            finished_payload = self.finished_event_payload(
                prepared_call.decoded,
                message,
                duration_ms,
            )
            self._emit(on_event, "tool_finished", finished_payload)
        return tuple(records)

    def execute(
        self,
        tool_calls: list[dict],
        context: ToolExecutionContext,
        on_event: Callable[[str, dict], None] | None,
    ) -> tuple[ToolExecutionRecord, ...]:
        records: list[ToolExecutionRecord] = []
        total_calls = 0

        for batch in self.prepare(tool_calls, context):
            step = getattr(context, "harness_step", None)
            total_calls += len(batch.calls)
            if step is not None:
                try:
                    step.mark_tool_call_count(total_calls)
                except Exception:
                    _LOGGER.warning("mark_tool_call_count failed", exc_info=True)
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
    return any(item.get("status") != "completed" for item in plan.snapshot())


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
    # B1-01：工具结果是数据——入历史前中和保留标记（幂等、不影响可见
    # 文本），防内容里的「【当前运行上下文】」等字面量被误认为系统注入
    # （信任混淆）或被 strip_context_suffix 从错误位置截断。
    from core.injection_markers import neutralize_reserved_markers

    return {
        "role": "tool",
        "tool_call_id": decoded.call_id,
        "name": decoded.name,
        "content": neutralize_reserved_markers(content),
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

    def _active_takeover_item(self, context):
        try:
            from core.task_scheduler import get_default_scheduler

            scheduler = get_default_scheduler()
        except Exception:
            return None
        takeover = scheduler.active_takeover(context.parent_turn_id)
        if (
            takeover is not None
            and takeover.get("status") == "in_progress"
            and takeover.get("execution_mode") == "main_takeover"
            and str(takeover.get("takeover_turn_id", "")) == str(context.parent_turn_id)
        ):
            return takeover
        return None

    @staticmethod
    def _takeover_files_violation(takeover, decoded):
        name = decoded.name
        if name not in {"write_file", "edit_file", "multi_edit"}:
            return None
        declared = [
            str(value).strip()
            for value in (takeover.get("files") or [])
            if str(value).strip()
        ]
        if not declared:
            return None
        from core.workspace_context import get_workdir

        file_path = str(decoded.arguments.get("file_path", "") or "").strip()
        if not file_path:
            return "计划接管约束：写文件必须提供 file_path。"
        workdir = get_workdir()

        def _normalized(value):
            candidate = value if os.path.isabs(value) else os.path.join(workdir, value)
            return os.path.normcase(os.path.realpath(os.path.abspath(candidate)))

        allowed = {_normalized(value) for value in declared}
        if _normalized(file_path) in allowed:
            return None
        return (
            "计划接管约束：接管项只声明了以下文件，写文件仅允许命中这些路径："
            f"[{', '.join(declared)}]。其他文件请先用 plan_update 调整计划项的 files 字段。"
        )

    @staticmethod
    def _plan_target_hint(name: str) -> str:
        try:
            from core.plan import get_default_plan_manager

            snapshot = get_default_plan_manager().snapshot()
        except Exception:
            return ""
        if not snapshot or all(item.get("status") == "completed" for item in snapshot):
            return ""
        want_prep = name == "ask_user"
        targets = [
            item
            for item in snapshot
            if (item.get("kind") == "input_preparation") == want_prep
            and item.get("status") in {"pending", "in_progress"}
        ]
        targets.sort(key=lambda item: item.get("status") != "pending")
        if targets:
            hints = ", ".join(
                f"'{item['id']}'({item['subject']})" for item in targets[:5]
            )
            return f" 当前可绑定的计划项: [{hints}]。"
        return " 当前没有可绑定的计划项，请先用 plan_read 查看各项状态再决定下一步。"

    def _plan_constraint(self, decoded, context):
        if not context.allow_subagents or not (
            self.plan_turn_checker(context.parent_turn_id)
            or self.pending_plan_checker(context.parent_session)
        ):
            return None
        arguments = decoded.arguments
        name = decoded.name
        if (
            name == "execute_command"
            and not bool(str(arguments.get("shell_id") or "").strip())
            and classify_command_risk(str(arguments.get("command", "")))
            is PermissionRisk.QUERY
        ):
            return None
        plan_item_id = str(arguments.get("plan_item_id", "") or "").strip()
        if name == "plan_write" and self.plan_turn_checker(context.parent_turn_id):
            from core.plan import get_default_plan_manager

            if any(item.get("status") != "pending" for item in get_default_plan_manager().snapshot()):
                return "计划执行约束：不能重建已有执行记录，请使用 /plan retry <id> 重试失败项。"
        if name in _PLAN_BOUND_ORCHESTRATORS and not plan_item_id:
            return (
                f"{_PLAN_BYPASS_ERROR_PREFIX} {name} 必须填写 plan_item_id，"
                f"不能执行未归属计划项的任务。{self._plan_target_hint(name)}"
            )
        if name in _PLAN_READ_ONLY_TOOLS or name in _PLAN_ROOT_CONTROL_TOOLS:
            return None

        from core.plan import get_default_plan_manager

        plan = get_default_plan_manager()
        snapshot = plan.snapshot()
        if not snapshot or all(
            item.get("status") == "completed" for item in snapshot
        ):
            return None
        takeover = self._active_takeover_item(context)
        if takeover is not None:
            violation = self._takeover_files_violation(takeover, decoded)
            if violation is not None:
                return violation
            return None
        ready = [item for item in plan.ready_items() if item.get("kind") != "input_preparation"]
        ready_hints = ""
        if ready:
            hints = ", ".join(f"'{item['id']}'({item['subject']})" for item in ready[:5])
            ready_hints = f" 当前可执行的计划项: [{hints}]。"
        else:
            # 没有可执行项时，光说“走 run_subagent”等于把模型留在原地打转：
            # 必须指名卡住的项和唯一的恢复动作，否则模型会反复重试同一个被拒工具。
            stalled = [
                item
                for item in snapshot
                if item.get("status") in {"failed", "blocked", "cancelled", "completed"}
            ]
            if stalled:
                names = ", ".join(
                    f"'{item['id']}'({item.get('status')})" for item in stalled[:5]
                )
                ready_hints = (
                    f" 当前没有可执行的计划项；不可直接执行的项: [{names}]。"
                    "failed/blocked/cancelled/completed 项请先用 plan_retry 重置为 pending"
                    "（被取消的 input_preparation 项重置后用 ask_user 重新提问），"
                    "再用 run_subagent 执行。"
                )
        return (
            f"{_PLAN_BYPASS_ERROR_PREFIX} {name} 必须通过 run_subagent(plan_item_id=...) 执行，"
            f"不能由主 Agent 直接绕过计划调用。{ready_hints}"
            "（若计划项子代理超时，主 Agent 接管后即可直接调用工具修复。）"
        )

    def _permission_decision(self, resolved, decoded, context):
        name = decoded.name
        arguments = decoded.arguments
        path_value = arguments.get("file_path") or arguments.get("dir_path") or arguments.get("path")
        outside_workspace = False
        if path_value:
            try:
                path_access = self.adapters.classify_path_access(name, path_value)
            except Exception:
                return None, PATH_VALIDATION_ERROR_MESSAGE
            outside_workspace = path_access in {"outside", "credential_write"}
        permission_name = name
        permission_arguments = arguments
        if outside_workspace:
            # 外部路径边界必须先以独立权限名暴露给兼容注入点，避免替身/
            # 嵌入方在 adapter 的存在性探测前只看到普通 edit/write 权限。
            permission_name = "sandbox_outside_access"
            permission_arguments = {
                "path": str(path_value or ""),
                "operation": name,
            }
        try:
            authorization = context.permission_checker(
                permission_name,
                permission_arguments,
                tool_spec=resolved.spec,
                outside_workspace=outside_workspace,
                event_callback=context.event_callback,
                permission_owner=context.permission_owner,
                parent_turn_id=context.parent_turn_id,
                permission_source=context.permission_source,
                cancel_event=context.cancel_event,
            )
        except self.permission_error_type:
            return None, PERMISSION_SYSTEM_ERROR_MESSAGE
        if isinstance(authorization, bool):
            return authorization, None if authorization else PERMISSION_DENIED_MESSAGE
        if authorization.allowed:
            return authorization, None
        if authorization.reason == "catastrophic_denied":
            return authorization, DANGEROUS_COMMAND_DENIED_MESSAGE
        if authorization.reason == "plan_mutation_denied":
            return authorization, PLAN_MODE_DENIED_MESSAGE
        if authorization.reason == "jev_auto_deny":
            return authorization, JEV_AUTO_DENIED_MESSAGE
        if authorization.cancelled or authorization.reason == "cancelled":
            return authorization, PERMISSION_CANCELLED_MESSAGE
        return authorization, PERMISSION_DENIED_MESSAGE

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


    def _stale_execution_lease(self, context):
        """旧子 Agent 延迟工具调用 fencing（在权限询问与 adapter 之前）。

        permission_owner 形如 subagent-xxxx 的调用来自子代理 LLM 链路：
        若该执行租约已失效（任务终态或不存在），返回取消错误；
        主代理（owner 为空/turn id）与计划验证 runner（subagent:角色）不受影响。
        """
        owner = str(getattr(context, "permission_owner", "") or "")
        from core.tools import is_subagent_task_id

        if not is_subagent_task_id(owner):
            return None
        try:
            from core.tools import subagent_manager
            if subagent_manager.is_execution_lease_active(owner):
                return None
        except Exception:
            # 执行租约真实性无法确认时必须 fail-closed；否则注册表短暂异常
            # 会把已经超时/取消的旧子 Agent 重新放回工具与权限执行链。
            return STALE_EXECUTION_LEASE_TEXT
        return STALE_EXECUTION_LEASE_TEXT

    def _secretary_gate_blocked(self, decoded, context) -> bool:
        name = str(getattr(decoded, "name", "") or "")
        if not name:
            return False
        try:
            from core.secretary import is_secretary_tool_blocked
        except Exception:
            return False
        scope = ""
        try:
            from core.llm import tool_registry as _registry

            scope = str(getattr(_registry.get(name), "side_effect_scope", "") or "")
        except Exception:
            scope = ""
        try:
            return bool(
                is_secretary_tool_blocked(
                    name,
                    scope,
                    session_name=str(getattr(context, "parent_session", "") or "") or None,
                )
            )
        except Exception:
            return False
    def _automation_gate_blocked(self, decoded, context):
        try:
            from core.automation import tool_policy as automation_tool_policy

            if automation_tool_policy.is_automation_turn_tool_blocked(decoded.name):
                return _tool_message(
                    decoded, automation_tool_policy.AUTOMATION_TURN_DENIED_MESSAGE
                )
        except Exception:
            _LOGGER.debug("自动化回合门控判定失败（旁路，按放行处理）", exc_info=True)
        return None

    def _browser_setting_gate_blocked(self, decoded, context):
        if str(getattr(decoded, "name", "") or "") != "browser_pane":
            return None
        cap = getattr(context, "computer_capability", None)
        if cap is None:
            lease = getattr(context, "computer_lease", None)
            cap = getattr(lease, "capability", None) if lease is not None else None
        configured_browser = str(getattr(cap, "browser", "") or "").strip()
        if not configured_browser:
            try:
                from core.computer.settings import load_computer_settings

                configured_browser = str(
                    getattr(load_computer_settings(), "computer_browser", "") or ""
                ).strip()
            except Exception:
                configured_browser = ""
        targets = tuple(getattr(cap, "targets", ()) or ()) if cap is not None else ()
        browser_cap_on = bool(
            getattr(context, "allow_subagents", False)
            and cap is not None
            and getattr(cap, "enabled", False)
            and "browser" in targets
        )
        if configured_browser and configured_browser != "embedded":
            from core.computer.settings import get_browser_label

            label = get_browser_label(configured_browser)
            if browser_cap_on:
                return _tool_message(
                    decoded,
                    f"{TOOL_ERROR_PREFIX} 工具调用被系统设置拦截：当前系统设置「默认浏览器」为 "
                    f"{label}（{configured_browser}），并非星瑶内置浏览器。请优先使用 "
                    f"computer_action(target=\"browser\", ...) 操作 {label}，不要调用 browser_pane。",
                )
            return _tool_message(
                decoded,
                f"{TOOL_ERROR_PREFIX} 工具调用被系统设置拦截：当前系统设置「默认浏览器」为 "
                f"{label}（{configured_browser}），并非星瑶内置浏览器；当前未启用浏览器操控，"
                "禁止改用 browser_pane 打开内置浏览器绕开系统设置。",
            )
        if configured_browser == "embedded" and browser_cap_on:
            args = decoded.arguments if isinstance(decoded.arguments, dict) else {}
            act = str(args.get("action") or "").strip().lower()
            url = str(args.get("url") or "").strip()
            url_lower = url.lower()
            is_web_nav = (
                act in ("navigate", "back", "forward", "reload")
                and (
                    act != "navigate"
                    or not url
                    or url_lower.startswith(("http://", "https://"))
                )
            ) or (
                act == "open"
                and (not url or url_lower.startswith(("http://", "https://")))
            )
            if is_web_nav:
                return _tool_message(
                    decoded,
                    f"{TOOL_ERROR_PREFIX} 工具调用被浏览器操控优先级拦截：当前系统设置「默认浏览器」"
                    "为星瑶内置浏览器且已开启「浏览器操控」，打开或导航网页必须优先使用 "
                    "computer_action(target=\"browser\", kind=\"navigate\"/\"observe\"/\"back\"/"
                    "\"forward\"/\"reload\")，避免启动无调试端口的只读面板；"
                    "browser_pane 仅用于 close/state/set_bounds、本地 file:// HTML 预览或关闭浏览器操控后使用。",
                )
        return None
    def _run_tool_hook(
        self,
        decoded,
        context,
        event_name: str,
        *,
        permission_decision: str = "",
        tool_result_preview: str = "",
    ):
        runner = getattr(context, "hook_runner", None)
        if runner is None:
            return None
        from core.hooks.lifecycle import build_hook_input, run_hook_safely

        return run_hook_safely(
            runner,
            event_name,
            build_hook_input(
                event_name,
                session_id=getattr(context, "parent_session", ""),
                turn_id=getattr(context, "parent_turn_id", ""),
                tool_name=decoded.name,
                tool_input=decoded.arguments,
                permission_decision=permission_decision,
                tool_result_preview=tool_result_preview,
            ),
            cancel_event=getattr(context, "cancel_event", None),
        )

    def _hook_ask_override(self, decoded, context) -> str:
        try:
            from core import permission_terminal as terminal

            wait = getattr(terminal, "_wait_permission_option", None)
            options_fn = getattr(terminal, "_permission_options", None)
            if wait is None or options_fn is None:
                return "approved"
            # 与权限链相同的 llm-first 解析：测试/入口层 patch
            # core.llm._wait_permission_option 时必须生效。
            resolve = getattr(terminal, "_llm_first", None)
            if resolve is not None:
                wait = resolve("_wait_permission_option", wait)
            task_grant = project_rule = None
            facts_fn = getattr(terminal, "_permission_rule_facts", None)
            if facts_fn is not None:
                try:
                    _task_hit, _project_hit, task_grant, project_rule = facts_fn(
                        decoded.name, decoded.arguments
                    )
                except Exception:
                    task_grant = project_rule = None
            options = options_fn(
                sandbox_request=False,
                credential_write=False,
                task_grant=task_grant,
                project_rule=project_rule,
            )
            option_key = wait(
                decoded.name,
                decoded.arguments,
                options,
                event_callback=getattr(context, "event_callback", None),
                permission_owner=getattr(context, "permission_owner", None),
                parent_turn_id=getattr(context, "parent_turn_id", None),
                permission_source=getattr(context, "permission_source", "主代理"),
                cancel_event=getattr(context, "cancel_event", None),
            )
            if option_key == "cancelled":
                return "cancelled"
            if option_key == "deny":
                return "denied"
            return "approved"
        except Exception:
            return "approved"

    def execute_decoded(self, decoded: DecodedToolCall, context) -> dict:
        _raise_if_cancelled(context.cancel_event)
        fenced = self._stale_execution_lease(context)
        if fenced is not None:
            return _tool_message(decoded, fenced)
        constrained = self._plan_constraint(decoded, context)
        if constrained is not None:
            return _tool_message(decoded, constrained)

        # 秘书只读门控兜底（设计 2026-10-15 第 6 节）：放在权限判定之前——
        # 秘书会话里被禁的工具直接失败，不弹确认卡（避免"先确认再硬拒"）。
        # 放行时行为与本改动前完全一致；schema 层已先行过滤，这里是兜底。
        if self._secretary_gate_blocked(decoded, context):
            _LOGGER.info(
                "秘书会话只读门控拒绝工具：%s（会话 %s）",
                decoded.name,
                getattr(context, "parent_session", "")
                or app_context.get_current_session(),
            )
            return _tool_message(decoded, SECRETARY_READONLY_DENIED_MESSAGE)

        # 自动化回合安全门控（G3 防任务自我繁殖）：与秘书门控一样放在权限判定
        # 之前——无人值守回合里确认卡等于卡死，直接失败更清晰。schema 层已隐藏
        # 同名工具，这里兜住 MCP / 插件 / 同名注册工具的旁路调用。
        automation_blocked = self._automation_gate_blocked(decoded, context)
        if automation_blocked is not None:
            _LOGGER.info(
                "自动化回合拒绝写类自动化工具：%s（run_id=%s）",
                decoded.name,
                app_context.get_thread_automation_turn() or "",
            )
            return automation_blocked

        browser_blocked = self._browser_setting_gate_blocked(decoded, context)
        if browser_blocked is not None:
            _LOGGER.info(
                "系统设置默认浏览器门控拦截 browser_pane：%s",
                decoded.arguments.get("action") if isinstance(decoded.arguments, dict) else "",
            )
            return browser_blocked

        # 固定 computer 适配（设计 19.2）：保留名 computer_action 在 resolve_tool
        # 之前识别，不经过注册表与权限链（设置开关即用户授权边界）。
        reserved = self._execute_reserved_computer(decoded, context)
        if reserved is not None:
            return reserved

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
        pre_hook = None
        try:
            self.adapters.announce(
                decoded.name, decoded.arguments, context.event_callback
            )
            authorization, denied = self._permission_decision(resolved, decoded, context)
            if denied is not None:
                return _tool_message(decoded, denied)
            _raise_if_cancelled(context.cancel_event)

            pre_hook = self._run_tool_hook(
                decoded,
                context,
                "PreToolUse",
                permission_decision=_hook_permission_decision(authorization),
            )
            if pre_hook is not None:
                if pre_hook.block_requested:
                    return _tool_message_with_hook_contexts(
                        decoded,
                        f"{TOOL_ERROR_PREFIX} 工具调用被 PreToolUse hook 阻断："
                        f"{str(pre_hook.block_reason or '').strip() or 'hook 请求阻断本次调用'}",
                        pre_hook,
                        "PreToolUse",
                    )
                if pre_hook.permission_override == "deny":
                    return _tool_message_with_hook_contexts(
                        decoded,
                        f"{TOOL_ERROR_PREFIX} 工具调用被 PreToolUse hook 拒绝："
                        f"{str(pre_hook.block_reason or '').strip() or 'hook 请求拒绝本次调用'}",
                        pre_hook,
                        "PreToolUse",
                    )
                if (
                    pre_hook.permission_override == "ask"
                    and _hook_permission_decision(authorization) == "allow"
                ):
                    verdict = self._hook_ask_override(decoded, context)
                    if verdict == "denied":
                        return _tool_message_with_hook_contexts(
                            decoded, PERMISSION_DENIED_MESSAGE, pre_hook, "PreToolUse"
                        )
                    if verdict == "cancelled":
                        return _tool_message_with_hook_contexts(
                            decoded, PERMISSION_CANCELLED_MESSAGE, pre_hook, "PreToolUse"
                        )

            legacy_boolean_authorization = isinstance(authorization, bool)
            bypass = bool(getattr(authorization, "sandbox_bypass", False))
            if (
                legacy_boolean_authorization
                and not bypass
                and decoded.name in {"read_file", "write_file", "edit_file", "multi_edit"}
            ):
                # 兼容只返回 bool 的旧权限替身：外部边界既已批准，adapter
                # 必须在同一次临时 bypass 中执行，不能再次询问或提前探测路径。
                path_access = self.adapters.classify_path_access(
                    decoded.name, decoded.arguments.get("file_path")
                )
                bypass = path_access in {"outside", "credential_write"}
            with self.adapters.sandbox_bypass() if bypass else nullcontext():
                result = self.adapters.for_call(decoded.call_id).execute(
                    resolved, decoded.arguments, context
                )
            content = result.to_message_content()
        except Exception as tool_err:
            content = (
                f"{TOOL_ERROR_PREFIX} 调用工具 {decoded.name} 时发生系统内部错误：{tool_err}"
            )
        message = _tool_message(decoded, self.truncate_result(decoded.name, content))
        post_hook = self._run_tool_hook(
            decoded, context, "PostToolUse", tool_result_preview=content
        )
        hook_messages: tuple[dict, ...] = ()
        if pre_hook is not None:
            from core.hooks.lifecycle import contexts_to_messages

            hook_messages = contexts_to_messages(
                "PreToolUse", pre_hook.additional_contexts
            )
        if post_hook is not None:
            from core.hooks.lifecycle import contexts_to_messages

            hook_messages = hook_messages + contexts_to_messages(
                "PostToolUse", post_hook.additional_contexts
            )
        if hook_messages:
            message[_FOLLOWUP_MESSAGES_KEY] = hook_messages
        return message

    def _execute_reserved_computer(self, decoded: DecodedToolCall, context) -> dict | None:
        if decoded.name == "computer_select_target":
            from core.computer.provider_openai import execute_reserved_computer_selector

            selection = getattr(context, "computer_native_selection", None)
            try:
                result = execute_reserved_computer_selector(decoded, selection)
            except Exception as tool_err:
                return _tool_message(
                    decoded,
                    f"{TOOL_ERROR_PREFIX} 调用工具 {decoded.name} 时发生系统内部错误：{tool_err}",
                )
            if result is None:
                return None
            return _tool_message(
                decoded,
                self.truncate_result(decoded.name, result.to_message_content()),
            )
        if decoded.name != "computer_action":
            return None
        from core.computer.provider_generic import execute_reserved_computer_tool

        try:
            result = execute_reserved_computer_tool(decoded, context)
        except Exception as tool_err:
            return _tool_message(
                decoded,
                f"{TOOL_ERROR_PREFIX} 调用工具 {decoded.name} 时发生系统内部错误：{tool_err}",
            )
        if result is None:
            return None
        message = _tool_message(
            decoded,
            self.truncate_result(decoded.name, result.to_message_content()),
        )
        if result.followup_messages:
            message[_FOLLOWUP_MESSAGES_KEY] = tuple(result.followup_messages)
        return message

    def execute_one(self, tool_call, context) -> dict:
        try:
            decoded = decode_tool_call(tool_call)
        except ValueError as parse_error:
            return self._decode_error_message(tool_call, parse_error)
        message = self.execute_decoded(decoded, context)
        # 旧单调用入口没有 follow-up 持久化通道：剥离私有暂存，
        # 绝不让 _ycode_followup_messages 泄漏进普通 tool message。
        message.pop(_FOLLOWUP_MESSAGES_KEY, None)
        return message
