import difflib
import json
import hashlib
import logging
import queue
import random
import re
import threading
import time
import uuid
from collections import deque
# Python 3.10 及以下版本里，concurrent.futures.TimeoutError 和内置的 TimeoutError 不是同一个类，
# 等待授权的循环必须两个都捕获，否则一超时就崩
from concurrent.futures import TimeoutError
from copy import deepcopy
from typing import Dict, List

from core.harness.request_envelope import (
    PreparedStreamRequest,
    RequestEnvelope,
    _responses_input_from_messages,
    _responses_tools_from_chat_tools,
    prepare_native_continuation_request,
    prepare_stream_request,
)
from core.harness.journal import journal_storage_supported
from core.harness.runtime import TurnRuntime
import core.app_context as app_context
from core import permission_grants, permission_store
from core.command_policy import (
    _session_rule_is_exact,
    classify_command_risk,
    command_matches_session_rule,
    parse_manual_command_rule,
    session_rule_for_command,
)
from core.harness.stream_assembler import StreamAssembler, complete_thinking_blocks
from core.permission_broker import (
    CANCELLED_RESULT,
    PermissionOption,
    PermissionOptionKey,
    permission_broker,
)
from core.permission_policy import (
    PermissionAction,
    PermissionFacts,
    PermissionRisk,
    decide_permission,
)
from core.reasoning_stream import ReasoningStreamParser, split_reasoning_text
from core.tool_presenter import (
    build_tool_display,
    summarize_tool_finished,
    summarize_tool_started,
)
from core.tools import (
    edit_file,
    execute_command,
    fetch_url,
    get_weather,
    glob_files,
    is_outside_workspace,
    list_directory,
    manage_task,
    read_file,
    run_subagent,
    sandbox_bypass,
    search_code,
    search_news,
    write_file,
)
from core.turn_events import TurnEventFactory
from core.injection_markers import ORCHESTRATION_NUDGE_TEXT as _ORCHESTRATION_NUDGE_TEXT

# 电脑操控（Task 10，设计 9.3/19）：这里是轻量依赖，不碰键鼠也不碰浏览器；
# controller 顶层也不反过来 import core.llm，不会循环导入。测试时通过 monkeypatch
# 替换 llm.resolve_computer_capability / llm.get_computer_control_service 注入假实现。
from core.computer.capability import (
    resolve_computer_capability,
    take_turn_computer_capability,
)
from core.computer.controller import get_computer_control_service
from core.computer.models import (
    ComputerCapability,
    ComputerControlError,
    ComputerOwner,
    ComputerProtocol,
    ComputerRunStatus,
)
from core.computer.provider_generic import (
    build_generic_computer_tool,
    computer_error_tool_message,
    computer_tool_arguments_projection,
    validate_computer_tool_batch,
)
from core.computer.provider_openai import (
    NATIVE_CONTINUATION_OPTION_KEYS,
    BoundNativeController,
    NativeContinuationState,
    NativeTargetSelection,
    build_computer_call_output,
    build_native_call_persistence_row,
    build_openai_computer_tools,
    execute_native_computer_call,
    native_computer_call_from_stream,
    native_has_dual_targets,
)
from core.computer.tool_adapter import (
    COMPUTER_ACTION_TOOL_NAME,
    RESERVED_COMPUTER_TOOL_NAMES,
    ComputerTurnLease,
)

# 已结束的回合状态集合：收尾时查这里，避免对同一回合重复结束或停止
_COMPUTER_RUN_TERMINAL_STATUSES = frozenset({
    ComputerRunStatus.COMPLETED.value,
    ComputerRunStatus.STOPPED.value,
    ComputerRunStatus.FAILED.value,
})

logger = logging.getLogger(__name__)


from core.llm_transport import (
    API_REQUEST_MAX_TIMEOUT_SECONDS,
    API_REQUEST_TIMEOUT_SECONDS,
    REQUEST_WAIT_GIVE_UP_SECONDS,
    WAIT_NOTICE_INTERVAL_SECONDS,
    WAIT_NOTICE_THRESHOLD_SECONDS,
    RequestWaitGiveUpError,
    _InflightStream,
    _close_stream_quietly,
    _inflight_lock,
    _inflight_streams,
    _context_overflow_error,
    _dispatch_completion,
    _dispatch_responses,
    _register_inflight_stream,
    _submit_daemon_call,
    _token_counter,
    _unregister_inflight_stream,
    abort_inflight_streams,
    is_rate_limit_error,
)

from core.llm_error_mapper import map_llm_error

# 这几类失败重试没用（额度用完 / 密钥无效 / 权限不够 / 模型不存在 / 网关内容过滤），
# 命中即直接报错收尾；限流等临时性错误继续重试
_DETERMINISTIC_API_FAILURE_CATEGORIES = frozenset({
    "insufficient_quota",
    "auth_invalid",
    "permission_denied",
    "model_not_found",
    "model_unavailable",
    "policy_refusal",
    # 网关内容过滤是确定性的：上下文里带着触发词，重试多少次都会命中同一条规则。
    "gateway_content_filtered",
})


def _is_deterministic_api_failure(exc):
    try:
        return map_llm_error(exc).category in _DETERMINISTIC_API_FAILURE_CATEGORIES
    except Exception:
        return False


_RETRYABLE_STREAM_EXC_TYPE_MARKERS = (
    "remoteprotocolerror",
    "readerror",
    "writeerror",
    "networkerror",
    "connecterror",
    "connectionerror",
    "connectionreseterror",
    "connectionabortederror",
    "brokenpipeerror",
    "incompleteread",
    "apiconnectionerror",
)

_RETRYABLE_STREAM_TEXT_MARKERS = (
    "connectionterminated",
    "server disconnected",
    "connection reset",
    "connection aborted",
    "remote end closed",
    "goaway",
)


def _is_retryable_stream_interruption(exc):
    """流式阶段「对端主动断开连接」类异常，零产出时可换连接重发。

    httpcore 收到 HTTP/2 GOAWAY 会把整条连接判死，并对当前流抛
    RemoteProtocolError("<ConnectionTerminated ...>")——与请求内容无关，
    换新连接重发完全等价。读超时、等待首字节超时、协议违规、用户取消
    都不在此列。
    """
    if exc is None:
        return False
    if isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
        return False
    name = type(exc).__name__.lower()
    if "localprotocolerror" in name:
        return False
    if any(marker in name for marker in _RETRYABLE_STREAM_EXC_TYPE_MARKERS):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _RETRYABLE_STREAM_TEXT_MARKERS)


_KEY_RELATED_FAILURE_CATEGORIES = frozenset({
    "insufficient_quota",
    "auth_invalid",
    "permission_denied",
})


def _is_key_related_failure(exc):
    """判断异常是否属于"换 Key 可能解决"的厂商 Key 类失败（纯函数，便于单测）。

    整体用 try/except 兜底：分类失败时返回 False（保守），绝不误触发换 Key。
    """
    try:
        return map_llm_error(exc).category in _KEY_RELATED_FAILURE_CATEGORIES
    except Exception:
        return False

from core.permission_terminal import (
    ASK_USER_ERROR_PREFIX,
    PATH_VALIDATION_ERROR_MESSAGE,
    PERMISSION_CANCELLED_MESSAGE,
    PERMISSION_DENIED_MESSAGE,
    PERMISSION_SYSTEM_ERROR_MESSAGE,
    PLAN_MODE_DENIED_MESSAGE,
    TOOL_ERROR_PREFIX,
    TOOL_INTERNAL_ERROR_PREFIX,
    AuthorizationResult,
    PermissionSystemError,
    _KNOWN_QUERY_TOOLS,
    _PLAN_BOUND_ORCHESTRATORS,
    _PLAN_BYPASS_ERROR_PREFIX,
    _PLAN_INTERNAL_PERMISSION_TOOLS,
    _PLAN_READ_ONLY_TOOLS,
    _PLAN_ROOT_CONTROL_TOOLS,
    _SENSITIVE_TOOL_NAMES,
    _SESSION_GRANTS,
    _agent_display_name,
    _canonical_permission_path,
    _command_matches_prefix,
    _format_session_command_rule,
    _grant_command_add,
    _grant_command_hit,
    _grant_edit_add,
    _grant_edit_hit,
    _grant_exact_command_add,
    _grant_exact_command_hit,
    _grant_write_add,
    _grant_write_hit,
    _gui_permission_callback,
    _has_pending_plan,
    _is_plan_turn,
    _last_tracked_session,
    _mark_plan_turn,
    _notify_event,
    _permission_options,
    _permission_risk,
    _permission_rule_facts,
    _plan_turn_lock,
    _plan_turn_started_at,
    _raw_permission_lock,
    _session_prefix_for_command,
    _session_whitelist_lock,
    _wait_permission_option,
    ask_in_raw_terminal,
    authorize_tool_call,
    check_permission_and_ask,
    check_session_reset,
    clear_session_grants,
    safe_print,
    session_allowed_commands,
    session_allowed_edit_files,
    session_allowed_exact_commands,
    session_allowed_write_files,
)


def _pipeline_permission_checker(function_name, arguments, **context):
    return check_permission_and_ask(
        function_name,
        arguments,
        structured=True,
        **context,
    )


def _current_agent_display_name() -> str:
    try:
        from core.secretary import get_active_agent_name

        return get_active_agent_name()
    except Exception:
        return _agent_display_name()

from core.llm_diagnostics import (
    _USAGE_FIELD_MISSING,
    _account_server_usage,
    _build_cache_request_diagnostic,
    _cache_control_request_metrics,
    _cache_message_fingerprints,
    _cache_request_states,
    _cache_usage_key,
    _cache_usage_states,
    _canonical_diagnostic_json,
    _classify_cache_request,
    _classify_cache_request_details,
    _extract_cached_tokens,
    _mark_cache_usage_unavailable,
    _normalize_server_usage,
    _optional_nonnegative_int,
    _persist_cache_request_diagnostic,
    _persist_token_usage,
    _record_cache_usage,
    _strip_cache_control_markers,
    _usage_field,
    clear_cache_usage_session,
    clear_session_window_usage,
    get_session_window_usage,
    get_token_usage_snapshot,
    rename_cache_usage_session,
    session_token_usage,
    set_session_window_usage,
    set_token_usage_listener,
    token_usage_lock,
)

from core.tool_registry import build_default_registry, filter_reserved_tool_schemas
from core import secretary


class _LazyToolRegistry:

    __slots__ = ("_inner", "_building")

    def __init__(self) -> None:
        object.__setattr__(self, "_inner", None)
        object.__setattr__(self, "_building", False)

    def _load(self):
        inner = object.__getattribute__(self, "_inner")
        if inner is not None:
            return inner
        with _TOOL_REGISTRY_BUILD_LOCK:
            inner = object.__getattribute__(self, "_inner")
            if inner is not None:
                return inner
            if object.__getattribute__(self, "_building"):
                # 建表过程中可能又被同线程绕回来取一次（插件加载时会往上找当前活跃表）：
                # 这时返回一个空表顶上，避免无限递归。
                from core.tool_registry import ToolRegistry

                return ToolRegistry()
            object.__setattr__(self, "_building", True)
            try:
                inner = build_default_registry()
            finally:
                object.__setattr__(self, "_building", False)
            object.__setattr__(self, "_inner", inner)
            return inner

    def __getattr__(self, name):
        return getattr(self._load(), name)

    def __setattr__(self, name, value):
        setattr(self._load(), name, value)


_TOOL_REGISTRY_BUILD_LOCK = threading.RLock()

tool_registry = _LazyToolRegistry()


_mcp_first_wait_done = False


def _get_mcp_manager():
    from core.mcp_client import mcp_manager
    return mcp_manager


_DEBUG_TOOLS_DUMP_FLAG = r"D:\ycodesso\llm_tools_dump.on"
_DEBUG_TOOLS_DUMP_PATH = r"D:\ycodesso\llm_tools_dump.jsonl"


def _debug_tools_dump_enabled() -> bool:
    try:
        import os as _os

        if _os.environ.get("YCODE_DEBUG_TOOLS_DUMP", "").strip() == "1":
            return True
        return _os.path.exists(_DEBUG_TOOLS_DUMP_FLAG)
    except Exception:
        return False


def _debug_dump_request_tools(model_name, api_base, request_tools):
    """【纯旁路 / 临时诊断】把即将发往上游的工具列表落盘。

    为什么需要：上游对 tools[].function.description 做必填校验时只回「tools[N]
    缺 description」，N 是它看到的数组下标；本地复算不出真实列表就只能靠这份
    落盘对齐。仅在开关打开时写，任何异常只记 debug，绝不改变发送字节。
    """
    try:
        import hashlib as _hashlib
        import os as _os
        import time as _time

        if not _debug_tools_dump_enabled():
            return
        tools = [tool for tool in (request_tools or []) if isinstance(tool, dict)]
        blob = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        row = {
            "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": str(model_name or ""),
            "api_base": str(api_base or ""),
            "count": len(tools),
            "bytes": len(blob.encode("utf-8")),
            "sha256": _hashlib.sha256(blob.encode("utf-8")).hexdigest(),
            "tools": [
                {
                    "i": index,
                    "name": tool.get("function", {}).get("name"),
                    "desc": tool.get("function", {}).get("description"),
                }
                for index, tool in enumerate(tools)
            ],
        }
        with open(_DEBUG_TOOLS_DUMP_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("工具列表落盘失败（旁路，已忽略）", exc_info=True)


def _snapshot_tool_breakdown(request_tools, *, session_id: str | None = None):
    """【纯旁路】按 MCP 工具名集合拆分统计工具定义字符量（设计规格 §3.3；
    2026-09-23 拆分系统/MCP）。失败仅记 debug，绝不改变 request_tools 与发送字节。"""
    try:
        from core.context_breakdown import update_source as _update_ctx_source
        mcp_tool_names = {
            s.get("function", {}).get("name")
            for s in _get_mcp_manager().get_schemas()
        }
        system_tools = [
            s for s in request_tools
            if s.get("function", {}).get("name") not in mcp_tool_names
        ]
        mcp_tools = [
            s for s in request_tools
            if s.get("function", {}).get("name") in mcp_tool_names
        ]
        _update_ctx_source(
            "system_tools",
            len(json.dumps(system_tools, ensure_ascii=False)) if system_tools else 0,
            session_id=session_id,
        )
        _update_ctx_source(
            "mcp_tools",
            len(json.dumps(mcp_tools, ensure_ascii=False)) if mcp_tools else 0,
            session_id=session_id,
        )
    except Exception:
        logger.debug("工具定义字符量统计失败（旁路，已忽略）", exc_info=True)


def _registry_scope_map() -> dict[str, str]:
    scopes: dict[str, str] = {}
    try:
        for spec in tool_registry.all():
            scopes[str(getattr(spec, "name", "") or "")] = str(
                getattr(spec, "side_effect_scope", "") or ""
            )
    except Exception:
        logger.debug("读取工具作用域失败（旁路，按未知作用域处理）", exc_info=True)
    return scopes


def _secretary_tools_schema() -> list[dict]:
    from core.secretary import secretary_allows_tool

    scopes = _registry_scope_map()
    schemas: list[dict] = []
    for schema in tool_registry.api_schemas():
        name = str(schema.get("function", {}).get("name", "") or "")
        try:
            allowed = bool(name) and secretary_allows_tool(name, scopes.get(name, ""))
        except Exception:
            allowed = False
        if allowed:
            schemas.append(schema)
    return schemas


def _ordinary_tools_schema(excluded_names) -> list[dict]:
    """普通会话的工具 schema：注册表全量（剔除秘书专属项）+ MCP（保留名过滤后）。

    设计第 2/7 节硬约束：三个调度工具只对秘书会话可见，普通会话工具集与本次
    改动前逐元素一致（工具列表逐字节稳定是前缀缓存命中的前提）。
    """
    excluded = {str(name) for name in (excluded_names or ())}
    schemas = list(tool_registry.api_schemas())
    if excluded:
        schemas = [
            schema for schema in schemas
            if str(schema.get("function", {}).get("name", "") or "") not in excluded
        ]
    # 合并 MCP 工具时，要把电脑操控占用的名字挡掉（设计 19.2）：
    # computer_action / computer_select_target 由固定的 Harness 适配器提供，
    # MCP 不能再占用同名工具；没有重名时，工具列表和过滤前完全一样。
    return schemas + filter_reserved_tool_schemas(_get_mcp_manager().get_schemas())


def get_tools_schema():
    global _mcp_first_wait_done
    if not _mcp_first_wait_done:
        _mcp_first_wait_done = True
        try:
            _get_mcp_manager()._ensure_connected()
        except Exception:
            pass
    try:
        # 秘书身份解析出错就当普通会话处理：不能因为策略组件出故障，让普通对话少掉工具
        is_secretary = bool(secretary.is_secretary_session())
    except Exception:
        logger.debug("秘书会话判定失败（旁路，按普通会话处理）", exc_info=True)
        is_secretary = False
    if is_secretary:
        try:
            return _secretary_tools_schema()
        except Exception:
            logger.debug("秘书工具 schema 过滤失败（旁路，回落普通路径）", exc_info=True)
    # 自动化回合（G3）：再多藏掉一批会写东西的自动化工具，防止任务自己生自己。
    # 策略读取出错就按空集处理，走的调用和原来完全一致——普通回合的工具列表
    # 必须和改动前一个字节都不差（否则前缀缓存就命中不了）。
    hidden = frozenset()
    try:
        from core.automation import tool_policy as _automation_tool_policy

        hidden = _automation_tool_policy.hidden_tool_names_for_current_turn()
    except Exception:
        logger.debug("自动化回合工具过滤失败（旁路，按普通回合处理）", exc_info=True)
        hidden = frozenset()
    if hidden:
        return _ordinary_tools_schema(
            set(secretary.SECRETARY_ONLY_TOOLS) | {str(name) for name in hidden}
        )
    return _ordinary_tools_schema(secretary.SECRETARY_ONLY_TOOLS)


def completion(**kwargs):
    return _dispatch_completion(**kwargs)


from core.harness.responses_adapter import (
    _chat_chunk,
    _merge_stream_response_id,
    _native_continuation_instructions,
    _normalize_responses_event_type,
    _obj_value,
    _plain_value,
    _responses_events_as_chat_chunks,
    _responses_stream_as_chat_chunks,
    _responses_usage_as_chat_usage,
)


def _request_stream(prepared_or_protocol, kwargs=None, *, timeout=None, cancel_event=None):
    if isinstance(prepared_or_protocol, PreparedStreamRequest):
        if kwargs is not None or timeout is not None:
            raise TypeError("PreparedStreamRequest already contains provider kwargs and timeout")
        prepared = prepared_or_protocol
    else:
        if kwargs is None or timeout is None:
            raise TypeError("legacy request calls require kwargs and timeout")
        prepared = prepare_stream_request(prepared_or_protocol, kwargs, timeout)

    holder = _register_inflight_stream(cancel_event)
    try:
        if prepared.protocol != "responses":
            stream = completion(**prepared.kwargs)
            if holder is not None:
                holder.attach(stream)
            return stream, holder
        raw_stream = _dispatch_responses(**prepared.kwargs)
        if holder is not None:
            holder.attach(raw_stream)
        return _responses_stream_as_chat_chunks(raw_stream), holder
    except Exception:
        # 请求未建立：登记无意义，立即摘除，避免表中残留空句柄
        _unregister_inflight_stream(cancel_event, holder)
        raise


_token_estimate_cache = {"sig": None, "value": 0}
_token_prefix_cache = {"model": None, "msg_count": 0, "char_len": 0, "value": 0}
_token_cache_lock = threading.RLock()


def _sanitize_error_text(text):
    """异常文本入 UI / 落库前脱敏。

    网关 / Provider 异常文本常携带完整请求 URL 与请求上下文（query 里的 api_key、
    Authorization: Bearer 头、URL userinfo 等），直接发 error/turn_error 事件会把
    凭据泄露到界面与会话记录。这里对 URL 凭据段与常见密钥模式做不可逆脱敏。
    """
    text = str(text or "")
    text = re.sub(
        r"([a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)", r"\1***\2", text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|token|secret|password|passwd|authorization)\s*[=:]\s*)[^\s,;&\"']+",
        r"\1***",
        text,
    )
    # Authorization: Bearer xxx
    text = re.sub(r"(?i)\bBearer\s+[^\s\"',;]+", "Bearer ***", text)
    return re.sub(
        r"\b(?:sk|ghp|gho|ghu|ghs|ghr|glpat|hf|xoxb|xoxp|xoxa|AKIA|ASIA|AIza)[-_][A-Za-z0-9_.-]{6,}\b",
        "***",
        text,
    )


def _format_user_facing_error(exc):
    """将异常转换为用户友好的中文诊断说明，并确保脱敏。"""
    try:
        from core.llm_error_mapper import format_user_facing_error

        return format_user_facing_error(exc)
    except Exception:
        return _sanitize_error_text(exc)


def _fold_old_media_for_estimate(messages, *, active_computer_run_id=None):
    from core.computer.frames import find_retained_media_index

    last_media_idx = find_retained_media_index(
        messages, active_computer_run_id=active_computer_run_id
    )
    folded = []
    for idx, msg in enumerate(messages):
        content = msg.get("content")
        if (
            idx != last_media_idx
            and isinstance(content, list)
            and any(
                isinstance(item, dict) and item.get("type") in ("image", "image_url")
                for item in content
            )
        ):
            folded.append({"role": msg.get("role"), "content": _flatten_media_content(content)})
        else:
            folded.append(msg)
    return folded


def estimate_tokens(model_name, messages):
    """本地离线快速估算 Token 数，防止超载崩溃（带签名缓存 + 前缀增量缓存）

    大会话（10 万+ token）全量 tokenize 一次要数秒，而历史是 append-only 的，
    每轮只有尾部几条消息是新的。前缀增量缓存记录“前 K 条消息”的计数结果，
    下一轮只对新增尾部计数，把每轮估算开销从秒级降到毫秒级。
    前缀一致性用字符总长校验（估算场景容忍极小概率的等长突变误判）。
    """
    if not messages:
        return 0
    # 两份缓存里的字段互相关联，必须在一个锁范围内读、算、写，
    # 否则并发时会拼出“A 模型的签名配 B 模型的值”这种混搭快照。
    with _token_cache_lock:
        return _estimate_tokens_locked(model_name, messages)


def _estimate_tokens_locked(model_name, messages):
    try:
        # 签名里必须带上内容哈希：只用（模型、消息数、总字数）的话，内容改过但长度没变
        # （比如改写历史）会命中旧缓存，算出过期的 token 数
        parts = [str(m.get("content") or "") for m in messages]
        total_len = sum(len(p) for p in parts)
        sig = (str(model_name), len(messages), total_len, hash(tuple(parts)))
    except Exception:
        sig = None
    if sig is not None and _token_estimate_cache.get("sig") == sig:
        return _token_estimate_cache["value"]

    def _count(msgs):
        try:
            return _token_counter(model_name, _fold_old_media_for_estimate(msgs))
        except Exception:
            # 模型不支持或报错时，按大约 2 个字符算 1 个 token 估个大概
            return len(str(_fold_old_media_for_estimate(msgs))) // 2

    value = None
    pc = _token_prefix_cache
    if (
        pc.get("model") == str(model_name)
        and 0 < pc.get("msg_count", 0) < len(messages)
    ):
        prefix = messages[: pc["msg_count"]]
        try:
            prefix_len = sum(len(str(m.get("content") or "")) for m in prefix)
        except Exception:
            prefix_len = -1
        if prefix_len == pc.get("char_len"):
            value = pc["value"] + _count(messages[pc["msg_count"]:])
    if value is None:
        value = _count(messages)
    if sig is not None:
        _token_estimate_cache["sig"] = sig
        _token_estimate_cache["value"] = value
        _token_prefix_cache["model"] = str(model_name)
        _token_prefix_cache["msg_count"] = len(messages)
        _token_prefix_cache["char_len"] = total_len
        _token_prefix_cache["value"] = value
    return value

def _yield_content(text):
    for char in text:
        yield char


_FILLER_TOKENS = frozenset(
    {
        "好", "好的", "嗯", "哦", "噢", "ok", "okay", "行", "可以",
        "收到", "明白", "了解", "继续", "输出", "完毕",
        "yes", "no",
    }
)

# 与 _is_meaningful_assistant_content 共用同一套噪声定义，剥离与切分口径一致，避免正则漂移。
# 比原判据增加省略号“…”，否则“收到……”无法剥离；纯标点仍按原判据判空。
_ASSISTANT_NOISE_PATTERN = r"[\s`*_#>~\-\[\](){}|.,!?，。！？:：;；/\\…]+"


def _is_filler_only(text):
    raw = text or ""
    stripped = re.sub(_ASSISTANT_NOISE_PATTERN, "", raw)
    if not stripped:
        return False
    # 剩下的长度还这么长，不可能只是一句客套的短回应
    if len(stripped) > 6:
        return False
    fragments = [seg for seg in re.split(_ASSISTANT_NOISE_PATTERN, raw) if seg]
    if not fragments:
        return False
    return all(seg.lower() in _FILLER_TOKENS for seg in fragments)


def _is_meaningful_assistant_content(text):
    if not bool(re.sub(_ASSISTANT_NOISE_PATTERN, "", text or "")):
        return False
    return not _is_filler_only(text)


_DEGENERATE_WINDOW = 3
_DEGENERATE_MAX_FINGERPRINT_CHARS = 20
_DEGENERATE_SIMILARITY_THRESHOLD = 0.9
_DEGENERATE_MAX_NUDGES = 1


def _degenerate_fingerprint(text):
    return re.sub(_ASSISTANT_NOISE_PATTERN, "", text or "")


def _fingerprint_similarity(left, right):
    if not left or not right:
        return 0.0
    edit_ratio = difflib.SequenceMatcher(None, left, right).ratio()
    left_set, right_set = set(left), set(right)
    containment = len(left_set & right_set) / min(len(left_set), len(right_set))
    return max(edit_ratio, containment)


def _is_degenerate_repeat(fingerprints):
    window = list(fingerprints)[-_DEGENERATE_WINDOW:]
    if len(window) < _DEGENERATE_WINDOW:
        return False
    if not all(window):
        return False
    if (
        len(set(window)) == 1
        and len(window[0]) <= _DEGENERATE_MAX_FINGERPRINT_CHARS
    ):
        return True
    return all(
        _fingerprint_similarity(earlier, later)
        >= _DEGENERATE_SIMILARITY_THRESHOLD
        for index, earlier in enumerate(window)
        for later in window[index + 1:]
    )


# _read_text_with_encoding_fallback 的唯一实现位于 core/llm_execution.py，
# 由下方 re-export 区导入（utf-8 → gbk → latin-1 → utf-8(replace)），
# 此处不得保留同名副本，避免模块内出现两份行为不一致的实现。


def chat_with_model(
    model_name, api_key, messages, api_base=None, stream=False, model_id=None
):
    gen = _chat_with_model_generator(
        model_name,
        api_key,
        messages,
        api_base=api_base,
        stream=False,
        model_id=model_id,
    )
    if stream:
        return gen
    else:
        for _ in gen:
            pass
        if messages and messages[-1]["role"] == "assistant":
            # 尾部 assistant 消息 content 可能为 None（例如仅工具调用轮）：
            # 为保持兼容，统一归一化为空字符串返回，不向调用方暴露 None
            return messages[-1].get("content", "") or ""
        return ""


def _claim_takeover_at_turn_start(
    parent_session,
    parent_turn_id,
    event_callback=None,
):
    session_id = str(parent_session or "").strip()
    turn_id = str(parent_turn_id or "").strip()
    if not session_id or not turn_id:
        return None, None
    from core.task_scheduler import get_default_scheduler, takeover_handoff_payload

    scheduler = get_default_scheduler()
    claimed = scheduler.claim_takeover_for_turn(turn_id)
    if claimed is not None and event_callback is not None:
        event_callback("plan_item_handoff", takeover_handoff_payload(claimed))
        event_callback("plan_updated", {
            "title": "当前任务计划",
            "items": scheduler.plan.snapshot(),
        })
    return scheduler, claimed


# 入口层受控回合来源白名单：只有入口层（GUI/CLI 本地、LAN 手机端、飞书通道、
# 手机端 MCP 客户端经局域网 MCP 服务投递、/goal 目标模式自动续跑）能设置，
# 绝不来自用户消息或模型参数。
# 非 local 的回合一律按「远程」处理（禁用电脑操控、不解析 @{会话名}、不抢桌面当前
# 会话），见 core/computer/capability.py 与 core/agent_service.py 的
# turn_source != "local" 分支。
# 新增来源时必须同时更新 gui_bridge.TURN_SOURCES（有测试断言两者一致）。
COMPUTER_TURN_SOURCES = ("local", "lan", "feishu", "mcp", "goal", "server")


def resolve_turn_computer_capability(
    model_name,
    api_base=None,
    model_id=None,
    turn_source="local",
):
    from core.model_manager import get_model_config_by_litellm_model, get_model_protocol

    model = get_model_config_by_litellm_model(model_name, api_base)
    provider = {
        "protocol": get_model_protocol(model_name, api_base),
        "base_url": api_base,
    }
    try:
        from core.computer.settings import load_computer_settings

        settings = load_computer_settings()
    except ComputerControlError as error:
        return ComputerCapability(
            enabled=False,
            error_code=error.code,
            reason=error.message,
        )
    return resolve_computer_capability(model, provider, settings, turn_source)


def chat_with_model_events(
    model_name,
    api_key,
    messages,
    api_base=None,
    persist=True,
    turn_id=None,
    cancel_event=None,
    allow_subagents=True,
    allowed_tools=None,
    consume_background_notifications=True,
    parent_session=None,
    parent_turn_id=None,
    permission_owner=None,
    permission_source="主代理",
    cache_scope="main",
    cache_session_id=None,
    model_id=None,
    computer_turn_source="local",
    computer_capability=None,
    budget_notice_provider=None,
    request_reason: str = "",
    resume_turn: bool = False,
    # 只读目标预算：None 走 TurnProgressGuard 默认（交互回合 48）。
    # 一次性子代理（尤其只读审查）由调用方显式放宽，避免大批量逐章读取
    # 在收敛出结论前被误判为“无进展”而熔断。
    progress_read_budget: int | None = None,
):
    if computer_turn_source not in COMPUTER_TURN_SOURCES:
        raise ValueError(
            f"computer_turn_source 只允许受控值 {'|'.join(COMPUTER_TURN_SOURCES)}"
        )
    resolved_turn_id = turn_id or f"turn-{uuid.uuid4().hex[:8]}"
    resolved_parent_turn_id = parent_turn_id or resolved_turn_id
    # Turn 起点解析一次电脑操控能力（Task 10 接线点①，设计 9.3）：局部不可变
    # 快照贯穿全部 Step，绝不写模块级状态；子代理回合（allow_subagents=False）
    # 不是入口层回合，不解析也不注入电脑操控。handoff 总是取出（一次性），
    # 即使显式传参也清空，避免跨回合/跨测试泄漏旧快照。
    if allow_subagents:
        handoff_capability = take_turn_computer_capability()
        if computer_capability is None:
            computer_capability = handoff_capability
        if computer_capability is None:
            computer_capability = resolve_turn_computer_capability(
                model_name, api_base, model_id, computer_turn_source
            )
    else:
        computer_capability = None
    computer_turn_capability = computer_capability
    # Turn 起点快照：编排三件套（run_subagent / plan_write / plan_update）是否可用。
    # 设置页「子代理」开关关闭时整体不可用——计划项的执行器就是 run_subagent，
    # 只关 run_subagent 而保留 plan_write/plan_update 会让计划停在 in_progress
    # 永久死锁，因此必须整体关闭。快照按 Turn 固定（同一 Turn 内各 Step 复用），
    # 设置改动从下一回合生效；惰性导入避免 core.subagent_settings → config 的
    # 模块级循环依赖。
    from core.subagent_settings import orchestration_allowed as _orchestration_allowed

    allow_orchestration = _orchestration_allowed(allow_subagents)
    _manager = app_context.get_manager() if persist else None
    if persist and not journal_storage_supported(_manager):
        harness_runtime = None
    else:
        harness_runtime = TurnRuntime.start(
            resolved_turn_id,
            parent_session,
            persist=bool(persist),
            manager=_manager,
            cancel_event=cancel_event,
            clock_ms=lambda: time.time_ns() // 1_000_000,
            create_row=not resume_turn,
        )
    factory = TurnEventFactory(resolved_turn_id)
    pending_events = []
    pending_lock = threading.Lock()
    parser = ReasoningStreamParser()
    thinking_open = False
    turn_failed = False

    def emit(kind, payload):
        nonlocal thinking_open
        with pending_lock:
            if thinking_open:
                pending_events.append(factory.emit("thinking_finished", {}))
                thinking_open = False
            pending_events.append(factory.emit(kind, payload))

    def drain_public_events():
        nonlocal turn_failed
        with pending_lock:
            ready = pending_events[:]
            del pending_events[:]
        if any(event.kind == "turn_error" for event in ready):
            turn_failed = True
        return ready

    def events_for_parts(parts):
        nonlocal thinking_open
        ready = []
        for part in parts:
            if part.kind == "thinking":
                if not thinking_open:
                    ready.append(factory.emit("thinking_started", {}))
                    thinking_open = True
                if part.text:
                    ready.append(factory.emit("thinking_delta", {"text": part.text}))
                continue

            if thinking_open:
                ready.append(factory.emit("thinking_finished", {}))
                thinking_open = False
            if part.text:
                ready.append(factory.emit("assistant_delta", {"text": part.text}))
        return ready

    takeover_scheduler = None
    takeover_claimed = None
    takeover_released = False

    def release_claimed_takeover(*, cancelled, reason):
        nonlocal takeover_released
        if takeover_released or takeover_scheduler is None:
            return None
        released = takeover_scheduler.release_takeover_for_turn(
            resolved_parent_turn_id,
            cancelled=bool(cancelled),
            reason=str(reason),
        )
        if released is not None:
            # 只有真实释放后才标记幂等：子 Agent 可能在回合开始
            # 认领之后、收尾之前才超时移交，finally 必须仍能捕获。
            takeover_released = True
            emit("plan_updated", {
                "title": "当前任务计划",
                "items": takeover_scheduler.plan.snapshot(),
            })
        return released

    try:
        if allow_subagents:
            takeover_scheduler, takeover_claimed = _claim_takeover_at_turn_start(
                parent_session,
                resolved_parent_turn_id,
                emit,
            )

        generator = _chat_with_model_generator(
            model_name,
            api_key,
            messages,
            api_base=api_base,
            stream=persist,
            event_callback=emit,
            cancel_event=cancel_event,
            allow_subagents=allow_subagents,
            allowed_tools=allowed_tools,
            consume_background_notifications=consume_background_notifications,
            parent_session=parent_session,
            parent_turn_id=resolved_parent_turn_id,
            permission_owner=permission_owner or resolved_parent_turn_id,
            permission_source=permission_source,
            cache_scope=cache_scope,
            cache_session_id=cache_session_id or parent_session,
            model_id=model_id,
            harness_runtime=harness_runtime,
            computer_capability=computer_turn_capability,
            budget_notice_provider=budget_notice_provider,
            request_reason=request_reason,
            progress_read_budget=progress_read_budget,
        )
        for chunk in generator:
            yield from drain_public_events()
            if chunk:
                yield from events_for_parts(parser.feed(chunk))

        release_claimed_takeover(
            cancelled=False,
            reason="主回合结束前未完成接管项，已等待下一回合继续",
        )
        yield from drain_public_events()
        yield from events_for_parts(parser.finish())
        if thinking_open:
            yield factory.emit("thinking_finished", {})
            thinking_open = False
        # 异常结束的回合只发 turn_error，不再发 turn_finished，避免 UI 把失败当成功
        if not turn_failed:
            yield factory.emit("turn_finished", {})
    except KeyboardInterrupt:
        if harness_runtime is not None:
            harness_runtime.cancel()
        release_claimed_takeover(
            cancelled=True,
            reason="用户取消了主 Agent 接管回合",
        )
        yield from drain_public_events()
        yield factory.emit("turn_cancelled", {})
        raise
    except Exception as exc:
        if harness_runtime is not None:
            harness_runtime.fail(type(exc).__name__)
        release_claimed_takeover(
            cancelled=bool(cancel_event is not None and cancel_event.is_set()),
            reason=(
                "用户取消了主 Agent 接管回合"
                if cancel_event is not None and cancel_event.is_set()
                else f"主回合异常结束：{type(exc).__name__}"
            ),
        )
        yield from drain_public_events()
        yield factory.emit("turn_error", {"message": _format_user_facing_error(exc)})
        raise
    finally:
        # 消费端主动 close 会以 GeneratorExit 跳过上面的两个异常分支；这里
        # 只做幂等状态收尾。此时无法再向已关闭的消费者发送展示事件。
        release_claimed_takeover(
            cancelled=bool(cancel_event is not None and cancel_event.is_set()),
            reason=(
                "用户取消了主 Agent 接管回合"
                if cancel_event is not None and cancel_event.is_set()
                else "主回合在完成接管项前结束，已等待下一回合继续"
            ),
        )

from core.llm_dialects import (
    _affinity_disabled_lock,
    _affinity_disabled_routes,
    _affinity_route_disabled,
    _ANTHROPIC_REASONING_BUDGETS,
    _BUILTIN_DEFAULT_MAX_TOKENS,
    _CACHE_CONTROL_PROVIDERS,
    _CACHE_CONTROL_REJECTION_TTL_SECONDS,
    _K3_REASONING_MODEL_NAMES,
    _KIMI_NAME_RE,
    _QWEN_CACHE_CFG_TTL_SECONDS,
    _QWEN_CACHE_CONFIG_KEYS,
    _REASONING_EFFORT_LEVELS,
    _REASONING_FIELD_PROVIDERS,
    _STEPFUN_ANTHROPIC_EFFORT_MODELS,
    _apply_cache_control,
    _apply_reasoning_choice,
    _assert_reasoning_request_consistent,
    _attach_kimi_request_extras,
    _attach_qwen_request_extras,
    _builtin_default_max_tokens,
    _cache_affinity_session_id,
    _cache_control_disabled_lock,
    _cache_control_disabled_routes,
    _cache_control_route_disabled,
    _cache_control_route_key,
    _configured_reasoning_choice,
    _deepseek_stable_header_matches,
    _deepseek_thinking_enabled,
    _disable_affinity_route,
    _disable_cache_control_route,
    _flatten_media_content,
    _glm_keepalive_thinking_locked,
    _is_cache_control_rejection,
    _is_deepseek_family,
    _is_glm_family,
    _is_kimi_affinity_rejection,
    _is_kimi_family,
    _is_official_dashscope_route,
    _is_official_deepseek_route,
    _is_official_kimi_route,
    _is_official_zhipu_route,
    _is_qwen_family,
    _merge_system_messages_into_leading,
    _model_system_prompt_extra,
    _new_tail_after_ledger_prefix,
    _pending_ledger_rotations,
    _pending_ledger_rotations_lock,
    _prepare_deepseek_ledger_request,
    _provider_cache_affinity_options,
    _provider_request_options,
    _qwen_cache_cfg_cache,
    _qwen_cache_cfg_lock,
    _qwen_cache_config_stamp,
    _qwen_cache_control_configured,
    _qwen_cache_enabled,
    _requires_single_leading_system,
    _requires_tool_call_reasoning,
    _resolve_cache_session_identity,
    _resolve_model_config,
    _select_request_tools_for_route,
    _strip_cache_affinity_options,
    _strip_media_and_merge,
    _supports_cache_control,
    _supports_cache_keepalive,
    _supports_reasoning_fields,
    _supports_vision,
    _supports_vision_locked,
    _tool_contract_fingerprint,
    _uses_deepseek_prompt_ledger,
    _vision_support_cache,
    consume_deepseek_generation_rotation,
    is_deepseek_ledger_route,
    mark_deepseek_generation_rotation,
)

def _complete_thinking_blocks(delta):
    return complete_thinking_blocks(delta)

# _notify_event 唯一实现位于 core/permission_terminal.py，经上方 re-export
# 进入本命名空间，此处不得保留同名副本。


def _stream_chunks_with_idle_notice(response_stream, event_callback, raise_if_cancelled, inflight_holder=None, cancel_event=None):
    chunks_queue: "queue.Queue" = queue.Queue()
    _END = object()

    def _pump():
        try:
            for chunk in response_stream:
                chunks_queue.put((True, chunk))
        except BaseException as err:
            chunks_queue.put((False, err))
        finally:
            chunks_queue.put((False, _END))

    threading.Thread(target=_pump, daemon=True, name="llm-stream-pump").start()
    idle_started_at = time.monotonic()
    last_notice_at = idle_started_at
    try:
        while True:
            raise_if_cancelled()
            try:
                ok, item = chunks_queue.get(timeout=0.5)
            except queue.Empty:
                idle = time.monotonic() - idle_started_at
                if idle >= REQUEST_WAIT_GIVE_UP_SECONDS:
                    if inflight_holder is not None:
                        try:
                            inflight_holder.close()
                        except Exception:
                            pass
                    raise RequestWaitGiveUpError(
                        f"模型流式响应已连续 {int(idle)} 秒"
                        f"（约 {int(REQUEST_WAIT_GIVE_UP_SECONDS // 60)} 分钟上限）"
                        "无新数据，本轮已自动结束；可重试或检查模型服务状态"
                    )
                if (
                    idle >= WAIT_NOTICE_THRESHOLD_SECONDS
                    and time.monotonic() - last_notice_at >= WAIT_NOTICE_INTERVAL_SECONDS
                ):
                    last_notice_at = time.monotonic()
                    wait_message = (
                        f"模型流式响应中断，已 {int(idle)} 秒未收到新数据，仍在等待连接恢复；"
                        "可按 Ctrl+C 中断"
                    )
                    if event_callback is None:
                        print(f"\n[⏳ {wait_message}]")
                    else:
                        _notify_event(event_callback, "status", {"message": wait_message})
                        yield None
                continue
            idle_started_at = time.monotonic()
            last_notice_at = idle_started_at
            if not ok:
                if item is _END:
                    return
                raise_if_cancelled()
                raise item
            yield item
    finally:
        _unregister_inflight_stream(cancel_event, inflight_holder)


from core.llm_execution import (
    CLAIM_NEGATION_PREFIXES,
    COMMAND_TOOL_NAME_MARKERS,
    PROMOTED_REASONING_PREVIEW_CHARS,
    SLOW_STEP_ABORT_SECONDS,
    SLOW_STEP_MAX_NUDGES,
    TOOL_RESULT_EVENT_MAX_CHARS,
    VERIFY_CLAIM_MARKERS,
    VERIFY_CLAIM_MAX_NUDGES,
    VERIFY_CLAIM_NUDGE_TEXT,
    _PLAN_STATE_RESULT_TOOLS,
    _RepeatCallGuard,
    _TOOL_RESULT_EXEMPT,
    _TOOL_RESULT_LIMITS,
    _TOOL_RESULT_MAX_CHARS,
    _TOOL_RESULT_TAIL_TOOLS,
    _build_tool_pipeline_and_context,
    _call_vision_model_for_content,
    _SUBAGENT_COMMAND_EVIDENCE_TYPES,
    _canonical_arguments_signature,
    _claims_verification_without_evidence,
    _compact_ephemeral_messages_in_place,
    _describe_images_with_secondary,
    _escape_control_chars_in_json_strings,
    _execute_single_tool_call,
    _get_imom_active_publish_root,
    _is_ephemeral_subagent_turn,
    _persisted_tool_calls,
    _raw_tool_call_arguments,
    _read_file_result_limit,
    _read_text_with_encoding_fallback,
    _refill_messages_after_compression,
    _repair_truncated_json,
    _sandbox_outside_guard,
    _show_file_diff,
    _step_progress_observation,
    _subagent_counts_as_command_evidence,
    _tool_call_activity_kinds,
    _tool_call_arguments,
    _tool_call_name,
    _tool_event_parts,
    _tool_event_payload,
    _tool_event_payload_from_parts,
    _tool_finished_event_payload,
    _tool_output_preview,
    _tool_result_ok,
    _tool_result_text,
    _truncate_tool_result_for_context,
    _unplanned_subagent_progress_part,
    _vision_fallback_text,
)

_ORCHESTRATION_NUDGE_THRESHOLD = 3
_ORCHESTRATION_COUNTED_TOOLS = frozenset({"write_file", "edit_file", "multi_edit"})
_ORCHESTRATION_COMMAND_TOOL = "execute_command"

# 慢步催促文案（core/tools.py 无该生成点；此处抽为模块级常量便于展示层过滤
# 前缀与生成点对账，防止文案漂移）。
SLOW_STEP_NUDGE_TEXT = (
    "你在最近一步耗时过长，且既没有调用任何工具、也没有输出任何"
    "正文内容，已出现明显的思考空转。请立即停止自检和复述思考过程，"
    "直接输出最终结论：任务已完成就给出中文结论，仍需操作就立即"
    "调用相应工具。禁止继续只输出思考内容。"
)
SUMMARY_NUDGE_TOOL_DONE_TEXT = (
    "工具已经执行完毕。现在必须只输出最终中文检查结论，"
    "说明发现的问题或下一步建议；不要调用工具，也不要只输出标点或 Markdown 标记。"
)
SUMMARY_NUDGE_THINKING_ONLY_TEXT = (
    "你刚才只输出了思考过程，既没有调用工具执行任何操作，"
    "也没有给出正式回复。请立即继续：需要执行操作就调用相应工具，"
    "任务已完成就直接输出中文结论；不要只输出思考内容。"
)
PLAN_CONTINUE_NUDGE_TEXT = (
    "计划尚未执行完毕，仍有可执行的计划项: [{hints}]。"
    "请继续调用 run_subagent(plan_item_id=...) 执行剩余计划项；"
    "若某项确实无法完成，请先用 plan_read 查看状态并明确说明原因，"
    "不要在计划未完成时直接结束回答。"
)

# 按 turn_id 的进程内去重：同一 turn_id 异常重入（生成器被重复驱动）时，
# 编排提醒只注入一次。写法参考 core/permission_terminal.py 的
# _plan_turn_started_at（带 TTL + 过期清理，避免无限增长）。
_orchestration_nudged_turns: dict[str, float] = {}
_orchestration_nudged_lock = threading.Lock()
_ORCHESTRATION_NUDGE_TURN_TTL_SECONDS = 3600


def _orchestration_turn_already_nudged(turn_id) -> bool:
    """该 turn_id 是否已注入过编排提醒（只读，不登记）。"""
    if turn_id is None:
        return False
    with _orchestration_nudged_lock:
        return str(turn_id) in _orchestration_nudged_turns


def _register_orchestration_nudge(turn_id, *, now=None) -> None:
    if turn_id is None:
        return
    now = time.monotonic() if now is None else now
    with _orchestration_nudged_lock:
        _orchestration_nudged_turns[str(turn_id)] = now
        cutoff = now - _ORCHESTRATION_NUDGE_TURN_TTL_SECONDS
        for stale_id, started_at in list(_orchestration_nudged_turns.items()):
            if started_at < cutoff:
                _orchestration_nudged_turns.pop(stale_id, None)


def _count_substantive_ops(tool_calls):
    from core.permission_policy import PermissionRisk

    count = 0
    for tool_call in tool_calls or ():
        function = tool_call.get("function") or {}
        name = str(function.get("name") or "")
        if name in _ORCHESTRATION_COUNTED_TOOLS:
            count += 1
            continue
        if name != _ORCHESTRATION_COMMAND_TOOL:
            continue
        raw = str(function.get("arguments") or "")
        try:
            arguments = json.loads(raw) if raw.strip() else {}
        except (TypeError, ValueError):
            count += 1
            continue
        command = str((arguments or {}).get("command") or "")
        if command and classify_command_risk(command) is not PermissionRisk.QUERY:
            count += 1
    return count


def _should_nudge_orchestration(*, allow_subagents, count, session_id, turn_id, already_sent):
    if not allow_subagents or already_sent:
        return False
    if count <= _ORCHESTRATION_NUDGE_THRESHOLD:
        return False
    if _orchestration_turn_already_nudged(turn_id):
        return False
    return not (_has_pending_plan(session_id) or _is_plan_turn(turn_id))


_ORIGINAL_EXECUTE_SINGLE_TOOL_CALL = _execute_single_tool_call


def _chat_with_model_generator(
    model_name,
    api_key,
    messages,
    api_base=None,
    stream=False,
    event_callback=None,
    cancel_event=None,
    allow_subagents=True,
    allowed_tools=None,
    consume_background_notifications=True,
    parent_session=None,
    parent_turn_id=None,
    permission_owner=None,
    permission_source="主代理",
    cache_scope="main",
    cache_session_id=None,
    model_id=None,
    *,
    harness_runtime=None,
    computer_capability=None,
    budget_notice_provider=None,
    request_reason: str = "",
    progress_read_budget: int | None = None,
):
    from core.model_manager import get_model_protocol

    request_protocol = get_model_protocol(model_name, api_base)
    if request_protocol is None:
        request_protocol = "anthropic" if str(model_name).startswith("anthropic/") else "openai"
    turn_snapshot = deepcopy(messages)
    # 电脑操控「用户已停止」抑制态与待投递通知（core/computer/stop_notice.py）：
    # 函数内导入而非模块顶层，让依赖方向保持单向（stop_notice 只用标准库，
    # 不反向依赖 llm）。恢复条件②在这里生效：用户新消息命中恢复关键词即解除
    # 抑制——停止不再终止回合后模型会被硬拦（computer_user_stopped），必须留
    # 一条用户侧的解除路径，否则用户说"继续操作"也解不开。只扫最后一条 user
    # 消息：更早的历史消息代表过去的意图，不能用来解除当前抑制。
    from core.computer import stop_notice as _stop_notice

    for _turn_msg in reversed(messages):
        if isinstance(_turn_msg, dict) and _turn_msg.get("role") == "user":
            _resume_content = _turn_msg.get("content")
            if isinstance(_resume_content, list):
                _resume_content = " ".join(
                    str(_part.get("text") or "")
                    for _part in _resume_content
                    if isinstance(_part, dict)
                )
            if _stop_notice.user_message_resumes_control(str(_resume_content or "")):
                _stop_notice.deactivate_user_stop("user_message")
            break
    keepalive_interval = 0
    if cache_scope == "main":
        from core.cache_keepalive import cache_keepalive_service, load_keepalive_interval
        configured_keepalive = load_keepalive_interval()
        # 主模型请求开始只停掉"本会话"的旧快照，防止切换 provider 后仍保活旧模型；
        # 其他会话（后台/多窗口切换）的快照继续保活，切回时不再全量 miss。
        cache_keepalive_service.register_request(
            {},
            interval=0,
            session_key=_resolve_cache_session_identity(cache_session_id),
        )
        if request_protocol != "responses" and _supports_cache_keepalive(model_name, api_base):
            keepalive_interval = configured_keepalive
    # 1. 自动注入 KAIROS 长期记忆指示与 MEMORY.md 索引。
    # 注意：main.py 的动态上下文（尾部第二条 system 消息）里已经带了完整记忆段，
    # 所以必须检查所有 system 消息，避免重复注入（重复会翻倍 token 消耗）。
    # 同时绝不能把动态内容拼进 messages[0]——那是提示词缓存的稳定前缀，
    # 一旦每轮变化，provider 侧的 prompt cache 将永远无法命中。
    import core.memory_kairos as mk

    _memory_already_injected = any(
        msg.get("role") == "system" and "# Auto Memory" in str(msg.get("content") or "")
        for msg in messages
    )
    if not _memory_already_injected:
        memory_section = mk.build_memory_system_section()
        if memory_section and memory_section.strip():
            insert_at = len(messages)
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    insert_at = i
                    break
            messages.insert(insert_at, {"role": "system", "content": memory_section})

    # 1b. 自我演变块（自我模型 + 当前用户档案）兜底注入。与记忆段同样作为独立
    # system 消息插入到最后一条 user 消息之前，绝不改写 messages[0]（缓存稳定
    # 前缀）；默认安装（无成长数据）该块为空串，此时不插入、提示词逐字节不变。
    try:
        _identity_already_injected = any(
            msg.get("role") == "system"
            and "# 自我模型" in str(msg.get("content") or "")
            for msg in messages
        )
        if not _identity_already_injected:
            from core.self_prompt import build_identity_prompt_block_for_session

            identity_block = build_identity_prompt_block_for_session(
                _resolve_cache_session_identity(cache_session_id)
            )
            if identity_block and identity_block.strip():
                identity_insert_at = len(messages)
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].get("role") == "user":
                        identity_insert_at = i
                        break
                messages.insert(
                    identity_insert_at, {"role": "system", "content": identity_block}
                )
    except Exception:
        pass

    tool_activity_seen = False
    final_summary_retry_count = 0
    # "只思考不产出"时的最终总结重试上限：连续多次欠佳仍尝试补救，
    # 避免长任务中一两轮失败就静默收尾（用户反馈"任务未执行完就结束"）。
    FINAL_SUMMARY_MAX_RETRIES = 2
    slow_step_nudge_count = 0
    # 退化（复读）检测：最近 3 步的正文指纹滑窗 + 已触发的收敛催促次数。
    # 与 slow_step_nudge_count 同生命周期（随回合重置），且只存本回合内存：
    # 不写 Journal、不入库、不落盘（与 read_signatures 同策略，避免污染缓存与历史）。
    recent_content_fingerprints = deque(maxlen=_DEGENERATE_WINDOW)
    degenerate_nudge_count = 0
    verify_claim_nudge_count = 0
    turn_tool_call_count = 0
    # 本回合是否出现过写入类/命令执行类工具（按工具名累计，只读不写请求）。
    used_write_tools = False
    used_command_tools = False
    from core.harness.tool_pipeline import RepeatCallGuard

    repeat_call_guard = RepeatCallGuard()
    from core.harness.progress_guard import TurnProgressGuard

    progress_guard = TurnProgressGuard(max_unique_reads=progress_read_budget)
    consecutive_error_steps = 0
    last_loop_guard_turn = -10
    loop_guard_count = 0
    computer_controller = None
    computer_owner = None
    computer_run_id = ""
    computer_lease = None
    computer_exit_kind = "stopped"

    def _finalize_computer_run():
        nonlocal computer_controller, computer_owner, computer_run_id
        if (
            native_selection is not None
            and native_selection.run_id
            and not computer_run_id
        ):
            computer_controller = native_selection.controller
            computer_owner = native_selection.owner
            computer_run_id = native_selection.run_id
        if computer_controller is None or not computer_run_id:
            return
        try:
            snapshot = computer_controller.snapshot()
            if (
                snapshot is not None
                and snapshot.run_id == computer_run_id
                and snapshot.status in _COMPUTER_RUN_TERMINAL_STATUSES
            ):
                return
            if (
                snapshot is not None
                and snapshot.run_id == computer_run_id
                and snapshot.status == ComputerRunStatus.PROVIDER_SAFETY_REQUIRED.value
            ):
                # Task 11b（设计 11.5）：provider_safety_required 留给用户显式
                # 停止或升级客户端——未确认前不可 resume，Turn 收尾既不
                # complete 也不 stop。
                return
            if computer_exit_kind == "completed":
                computer_controller.complete_run(
                    computer_run_id, computer_owner, retain_browser_session=True
                )
            else:
                # cancel_owner=False：owner 的 cancel_event 就是 gui_bridge
                # 判定"回合是否被用户取消"的事件（gui_bridge.py:6220 与 :6377）。
                # cancelled/failed/stopped 三类非正常收尾里只有真取消才该保留该
                # 语义，而真取消时 event 早已由停止按钮/Ctrl+C 路径置位，收尾不
                # 需要也不应再设它：否则一次普通失败（如上游 402 额度不足）会被
                # 误标成"已取消"，界面上错误原因全部丢失（2026-10-01 实机故障，
                # 日志 gui-runtime.log 20:14-20:28 连续 8 次）。与下方
                # turn_end_finalize_fallback 兜底口径保持一致。
                computer_controller.stop(
                    computer_run_id,
                    "turn_cancelled" if computer_exit_kind == "cancelled" else "turn_stopped",
                    cancel_owner=False,
                )
        except Exception as error:
            # 捕获范围必须宽于 ComputerControlError（2026-10-01 审查补修）：
            # snapshot()/complete_run()/stop() 可能抛出非领域异常，若任其逃逸，
            # 既没有兜底释放租约，还会把异常抛进回合 finally、打断整个收尾流程。
            logger.warning(
                "电脑操控 run 收尾失败（run=%s kind=%s，%s）：%s",
                computer_run_id, computer_exit_kind, type(error).__name__, error,
            )
            # L2（2026-10-01 "接管—交还"修复）：收尾失败兜底，保证"回合结束绝不
            # 留活租约"。complete_run 不可达的一切情形（paused 与
            # starting/completing/stopping 等非终态——paused → completing 属于
            # 既有契约明令拒绝的转移）以及其他导致收尾动作失败的异常，都在此
            # 强制 stop 释放租约，否则 run 会既无终态也无租约释放，下一回合被
            # computer_session_busy 挡死。整段兜底只 warning，绝不向上抛，也不
            # 掩盖回合原本结局（provider_safety_required 的提前 return 语义不受
            # 影响：该分支在上面已 return，不会进入这里）。
            try:
                fallback_snapshot = computer_controller.snapshot()
                if (
                    fallback_snapshot is not None
                    and fallback_snapshot.run_id == computer_run_id
                    and fallback_snapshot.status not in _COMPUTER_RUN_TERMINAL_STATUSES
                ):
                    # cancel_owner=False：gui_bridge.py:6220 与 :6377 用该 owner 的
                    # cancel_event 判定"回合是否被用户取消"，收尾兜底不得把正常
                    # 结束的回合误标成"已取消"。
                    computer_controller.stop(
                        computer_run_id,
                        "turn_end_finalize_fallback",
                        cancel_owner=False,
                    )
            except Exception:
                logger.warning(
                    "电脑操控 run 收尾兜底释放失败（run=%s kind=%s，已忽略）",
                    computer_run_id, computer_exit_kind, exc_info=True,
                )
        try:
            manager = app_context.get_manager()
            fold_session = parent_session or app_context.get_current_session()
            if manager is not None and fold_session:
                folded = manager.fold_session_computer_media(str(fold_session))
                if folded:
                    logger.info(
                        "已折叠会话 %s 的 %d 张电脑操控观察截图",
                        fold_session, folded,
                    )
        except Exception:
            logger.warning("折叠电脑操控观察截图失败（已忽略）", exc_info=True)
    # 计划未执行完时的续推次数上限：防止模型反复提前收尾与续推消息互相打转
    plan_continue_nudges = 0
    orchestration_nudge_sent = False
    substantive_op_count = 0
    pending_persistence = []
    persistence_failure = None
    current_step = None
    current_tool_calls = []
    pending_tool_records = {}
    stream_assembler = None

    def _model_display_snapshot(metrics=None):
        """为落库的 assistant 消息附加"生成所用模型"及性能指标的显示元数据。
        从 config 的 providers[].models[] 反查 model_name（litellm_model）对应的
        显示名 name；查不到或读取异常时静默回退为 model_name 本身，绝不抛错
        （此处位于持久化热路径，任何异常都不能影响消息落库）。"""
        disp = {"model_id": model_name, "model_name": model_name}
        try:
            from core.model_manager import load_config
            for provider in load_config().get("providers", []):
                for model in provider.get("models", []):
                    if (
                        model.get("litellm_model") == model_name
                        or model.get("id") == model_name
                    ):
                        disp["model_name"] = model.get("name") or model_name
                        break
        except Exception:
            # 读配置失败不影响落库：保留回退为 model_name 原值的行为，仅记录
            # 调试日志留下痕迹，便于排查显示名缺失。
            logger.debug("读取模型显示名配置失败，回退为 model_name", exc_info=True)
        if metrics:
            disp["metrics"] = metrics
        return {"_display": disp}

    def flush_persistence():
        """把本轮已确认的助手/工具消息按序落库（与正常结束路径一致）。"""
        nonlocal pending_persistence, persistence_failure, pending_tool_records
        if stream and pending_persistence:
            try:
                if harness_runtime is not None:
                    if current_step is None:
                        # 前置阶段产生的通知由首个 Step 一并原子提交。
                        return
                    from core.harness.models import HarnessEventKind, JournalEventDraft
                    from core.harness.tool_pipeline import (
                        ToolExecutionRecord,
                        build_tool_result_event_draft,
                    )

                    tool_calls_by_id = {
                        tool_call.get("id"): tool_call
                        for tool_call in current_tool_calls
                    }
                    events = []
                    for message_index, (role, content) in enumerate(pending_persistence):
                        if role == "assistant":
                            events.append(JournalEventDraft(
                                HarnessEventKind.ASSISTANT_COMMITTED,
                                step_id=current_step.step_id,
                                message_index=message_index,
                            ))
                            continue
                        if role != "tool" or not isinstance(content, dict):
                            continue
                        call_id = content.get("tool_call_id")
                        tool_call = tool_calls_by_id.get(call_id)
                        record = pending_tool_records.get(call_id)
                        if tool_call is None:
                            events.append(JournalEventDraft(
                                HarnessEventKind.TOOL_RESULT_COMMITTED,
                                step_id=current_step.step_id,
                                message_index=message_index,
                            ))
                            continue
                        if record is None:
                            record = ToolExecutionRecord(
                                call_ordinal=current_tool_calls.index(tool_call),
                                message=content,
                                duration_ms=0,
                            )
                        events.append(build_tool_result_event_draft(
                            current_step.step_id,
                            tool_call,
                            record,
                            message_index=message_index,
                            ok=_tool_result_ok(tool_call["function"]["name"], content),
                        ))
                    current_step.turn.journal.commit_messages(
                        current_step.turn.session_name,
                        current_step.turn.turn_id,
                        current_step.step_id,
                        pending_persistence,
                        events,
                    )
                    del pending_persistence[:]
                    pending_tool_records = {}
                    return
                _mgr = app_context.get_manager()
                if _mgr is None:
                    raise RuntimeError("消息持久化失败：存储管理器不可用")
                _mgr.add_messages(parent_session, pending_persistence)
                del pending_persistence[:]
            except Exception as error:
                persistence_failure = error
                raise

    def salvage_persistence_on_cancel():
        answered = {
            content.get("tool_call_id")
            for role, content in pending_persistence
            if role == "tool" and isinstance(content, dict)
        }
        for role, content in list(pending_persistence):
            if role != "assistant" or not isinstance(content, dict):
                continue
            for tc in content.get("tool_calls") or []:
                tcid = tc.get("id")
                if tcid and tcid not in answered:
                    answered.add(tcid)
                    name = (tc.get("function") or {}).get("name")
                    if name == COMPUTER_ACTION_TOOL_NAME:
                        cancelled_content = computer_error_tool_message(
                            tc,
                            ComputerControlError(
                                "computer_cancelled",
                                "用户取消了本回合，该电脑动作未执行完成",
                            ),
                        )["content"]
                    else:
                        cancelled_content = "[用户终止了对话，该工具未执行完成]"
                    pending_persistence.append(("tool", {
                        "role": "tool",
                        "tool_call_id": tcid,
                        "name": name,
                        "content": cancelled_content,
                    }))

    def salvage_streaming_partial():
        assembler = stream_assembler
        if assembler is None:
            return
        try:
            assembled = assembler.finish()
        except Exception:
            return
        public_content = (assembled.content or "").strip()
        if not public_content:
            return
        salvaged_message = {
            "role": "assistant",
            "content": f"{public_content}\n\n[本轮流式中断]",
        }
        if assembled.reasoning_content:
            salvaged_message["reasoning_content"] = assembled.reasoning_content
        salvaged_message.update(_model_display_snapshot())
        pending_persistence.append(("assistant", salvaged_message))

    from core.tools import subagent_manager
    drained_notifications = []
    if consume_background_notifications and parent_session:
        drained_notifications = subagent_manager.drain_notifications(parent_session)
        for notif in drained_notifications:
            notif_msg = {"role": "user", "content": notif}
            messages.append(notif_msg)
            # 立即加入待持久化队列并落库：即使本轮后续被取消，通知也不会丢失，
            # 否则模型上下文会出现"收到通知并回复了，但下一轮找不到通知"的矛盾。
            # 注意：队列元素必须是 (role, content) 二元组，与 add_messages 的解包约定一致。
            pending_persistence.append(("user", notif))
        flush_persistence()

    # 电脑操控「用户已停止」通知由 _stop_notice（回合入口已导入）提供，本处只
    # 准备"本轮已 drain"的镜像列表：异常/取消回滚时要像后台通知一样把已 drain
    # 的文案放回内存 messages，否则同一轮的后续步骤会看不到这条事实。
    drained_stop_notices = []

    def rollback_turn_messages():
        """回滚本轮新增的消息残影；已 drain 的后台通知重新保留，避免丢失。"""
        messages[:] = turn_snapshot
        for notif in drained_notifications:
            messages.append({"role": "user", "content": notif})
        for stop_notice_text in drained_stop_notices:
            messages.append({"role": "user", "content": stop_notice_text})

    # 注意：不因“计划进行中”裁剪 tools——计划约束由执行层
    # （_execute_single_tool_call 的 plan_enforced 拦截）统一保证，裁剪是双重保险。
    # tools 列表逐字节稳定是 DeepSeek/Kimi 前缀缓存命中的前提，
    # 计划创建↔完成之间切换工具集会让缓存从头部整体失效，代价远大于省下的 schema token。
    # MCP 管理工具默认不进请求：用户几乎都通过 /mcp 界面管理 MCP，
    # 这 4 个 schema 每轮白吃约 500 token。需要让 AI 代管 MCP 时在
    # config.json 里设 "expose_mcp_admin_tools": true 即可恢复。
    _MCP_ADMIN_TOOLS = {"mcp_add_server", "mcp_remove_server", "mcp_set_enabled", "mcp_list_servers"}
    try:
        from core.model_manager import load_config as _load_cfg_for_tools
        _expose_mcp_admin = bool(_load_cfg_for_tools().get("expose_mcp_admin_tools", False))
    except Exception:
        _expose_mcp_admin = False
    # Step 循环前的编排可用快照：子代理开关（设置 → 子代理）关闭时，编排三件套
    # run_subagent / plan_write / plan_update 整体不可用——计划项的执行器就是
    # run_subagent，只关 run_subagent 会让计划永久死锁在 in_progress。快照按 Turn
    # 固定，设置改动从下一回合生效；电脑操控相关的 allow_subagents 判定不受影响
    # （那是"是否入口层回合"语义，与本开关无关）。惰性导入避免核心层循环依赖。
    from core.subagent_settings import orchestration_allowed as _orchestration_allowed

    allow_orchestration = _orchestration_allowed(allow_subagents)
    _browser_pane_allowed = True
    if allowed_tools is None or "browser_pane" not in allowed_tools:
        _effective_browser = ""
        if computer_capability is not None and getattr(computer_capability, "browser", ""):
            _effective_browser = str(computer_capability.browser or "").strip()
        else:
            try:
                from core.computer.settings import (
                    load_computer_settings as _load_computer_settings_for_tools,
                )

                _effective_browser = str(
                    getattr(_load_computer_settings_for_tools(), "computer_browser", "") or ""
                ).strip()
            except Exception:
                _effective_browser = ""
        if _effective_browser and _effective_browser != "embedded":
            _browser_pane_allowed = False

    request_tools = [
        schema
        for schema in get_tools_schema()
        if allow_orchestration
        or schema.get("function", {}).get("name")
        not in {"run_subagent", "plan_write", "plan_update"}
        if _browser_pane_allowed
        or schema.get("function", {}).get("name") != "browser_pane"
        if allowed_tools is None
        or schema.get("function", {}).get("name") in allowed_tools
        if _expose_mcp_admin
        or (allowed_tools is not None and schema.get("function", {}).get("name") in allowed_tools)
        or schema.get("function", {}).get("name") not in _MCP_ADMIN_TOOLS
    ]

    # generic 电脑操控工具只在 Turn 冻结 capability 为 enabled+generic 且主代理
    # 回合时参与请求（Task 10 Step 5，设计 19.1）：复制列表追加，
    # 绝不缓存进全局 get_tools_schema()；capability 关闭时 tools 字节与
    # pre-feature 基线完全一致（deepcopy 副本，不污染模块级 schema 常量）。
    if computer_capability is not None and allow_subagents:
        computer_tool = build_generic_computer_tool(computer_capability)
        if computer_tool is not None:
            request_tools = [*request_tools, computer_tool]

    request_tools = _select_request_tools_for_route(
        model_name, api_base, request_tools
    )
    telemetry_session_id = _resolve_cache_session_identity(cache_session_id)
    # 【纯旁路】工具定义字符量快照（设计规格 §3.3；2026-09-23 拆分系统/MCP）：
    # 仅主对话作用域按当前会话写入，避免子代理/压缩/保活请求覆盖主会话面板占比。
    if str(cache_scope or "main") == "main":
        _snapshot_tool_breakdown(request_tools, session_id=telemetry_session_id)
    _debug_dump_request_tools(model_name, api_base, request_tools)
    native_computer_capability = (
        computer_capability
        if (
            computer_capability is not None
            and allow_subagents
            and computer_capability.enabled
            and computer_capability.protocol == ComputerProtocol.NATIVE_OPENAI.value
        )
        else None
    )
    native_selection = None
    if native_computer_capability is not None and native_has_dual_targets(
        native_computer_capability
    ):
        native_selection = NativeTargetSelection(
            native_computer_capability,
            ComputerOwner(
                session_id=str(parent_session or ""),
                turn_id=str(parent_turn_id or ""),
                session_label=str(parent_session or parent_turn_id or ""),
                cancel_event=(
                    cancel_event
                    if cancel_event is not None
                    else threading.Event()
                ),
            ),
            get_computer_control_service(),
        )
    # Task 11b（设计 11.3）：原生续传状态——只存活于本生成器的局部变量
    # （Turn 内存）。computer_call 响应更新 state/output，最终文本或空响应
    # 清空（previous_response_id 不得跨响应复用）；绝不写入 message、
    # Journal、event 或 runtime log。
    native_continuation_state = None
    native_continuation_output = None
    request_tool_contract = _tool_contract_fingerprint(request_tools)
    deepseek_ledger_enabled = _uses_deepseek_prompt_ledger(
        model_name, api_base, cache_scope, stream, parent_session
    )
    deepseek_ledger_manager = (
        app_context.get_manager() if deepseek_ledger_enabled else None
    )
    if deepseek_ledger_enabled and deepseek_ledger_manager is None:
        raise RuntimeError("DeepSeek prompt 账本不可用：存储管理器未初始化")

    kwargs = {
        "model": model_name,
        "api_key": api_key,
        "messages": messages,
        # tools 每轮都带（含简单问候轮）：请求形状逐字节稳定是前缀缓存命中的前提，
        # 问候轮省下的 schema token 远小于缓存整体失效的代价；
        # 模型在 tool_choice=auto 下对闲聊本来就不会调用工具
        "tools": request_tools,
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
    }
    request_model_id = str(model_id or "") or None
    if request_model_id is None and parent_session:
        try:
            from core.model_manager import get_active_model as _get_bound_model
            bound_model = _get_bound_model(parent_session) or {}
            same_model = str(bound_model.get("litellm_model") or "") == str(model_name or "")
            same_base = (
                str(bound_model.get("base_url") or "").rstrip("/").lower()
                == str(api_base or "").rstrip("/").lower()
            )
            if same_model and same_base:
                request_model_id = str(bound_model.get("id") or "") or None
        except Exception:
            request_model_id = None
    # 模型级系统提示扩展（system_prompt_extra）：只对显式配置了该字段的模型生效，
    # 在请求消息最前面插入一条独立 system 消息。必须构造新列表——messages 是调用
    # 方共享的同一 list，原地 append 会污染调用方与持久化历史；未配置时完全不介入，
    # 请求字节与既有行为逐字节一致（稳定前缀逻辑因此完全不受影响）。
    model_system_extra = _model_system_prompt_extra(
        model_name, api_base, model_id=request_model_id
    )
    extra_injected_message = None
    original_messages = None
    original_message_ids = frozenset()
    if model_system_extra:
        original_messages = messages
        original_message_ids = frozenset(id(item) for item in messages)
        extra_injected_message = {"role": "system", "content": model_system_extra}
        messages = [extra_injected_message] + list(messages)
        kwargs["messages"] = messages
    kwargs.update(_provider_request_options(
        model_name,
        api_base,
        model_id=request_model_id,
        cache_scope=cache_scope,
        cache_session_id=telemetry_session_id,
        tool_contract=request_tool_contract,
    ))
    deepseek_thinking_request = (
        _is_deepseek_family(model_name, api_base)
        and kwargs.get("extra_body", {}).get("thinking", {}).get("type") == "enabled"
    )
    if not deepseek_thinking_request:
        kwargs["tool_choice"] = "auto"

    # 默认输出长度上限：防止免费/轻量模型（如 glm-4-flash）默认 max_tokens 过小
    # 导致长回复被静默截断。若 provider 已显式配置则尊重其值；内置默认表未覆盖的
    # 模型在这里兜底 32000。
    if "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs:
        kwargs["max_tokens"] = 32000

    if api_base:
        kwargs["api_base"] = api_base

    vision_user_message = next(
        (message for message in reversed(messages) if message.get("role") == "user"),
        None,
    )
    vision_original_content = (
        deepcopy(vision_user_message.get("content"))
        if vision_user_message is not None
        else None
    )
    _describe_images_with_secondary(messages, model_name, event_callback=event_callback, cancel_event=cancel_event)
    if (
        stream
        and parent_session
        and vision_user_message is not None
        and vision_user_message.get("content") != vision_original_content
    ):
        manager = app_context.get_manager()
        if manager is None:
            raise RuntimeError("视觉 canonical 消息持久化失败：存储管理器不可用")
        replaced = manager.replace_latest_user_message_content(
            parent_session,
            vision_original_content,
            vision_user_message.get("content"),
        )
        if not replaced:
            raise RuntimeError("视觉 canonical 消息持久化失败：最新用户消息已变化")

    def raise_if_cancelled():
        if harness_runtime is not None:
            harness_runtime.heartbeat()
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt("用户终止了对话。")

    response_stream = None
    try:
        from core.harness.turn_limits import load_turn_step_limit

        max_turns = load_turn_step_limit()
        if harness_runtime is not None:
            harness_runtime.mark_running()
        latest_step_metrics = None
        for turn in range(max_turns):
            raise_if_cancelled()
            if progress_guard.should_abort():
                msg = (
                    f"{TOOL_ERROR_PREFIX} 连续 {progress_guard.MAX_STALLED_STEPS} 轮"
                    "没有检测到后台任务、计划或工具结果"
                    "状态变化，回合已终止，避免继续循环消耗。"
                )
                no_progress_message = {"role": "assistant", "content": msg}
                messages.append(no_progress_message)
                if stream:
                    persisted_no_progress = deepcopy(no_progress_message)
                    persisted_no_progress.update(_model_display_snapshot())
                    pending_persistence.append(("assistant", persisted_no_progress))
                flush_persistence()
                if harness_runtime is not None:
                    harness_runtime.fail("NoProgressError")
                _notify_event(event_callback, "error", {"message": msg})
                # Harness 已进入 failed 终态，事件层必须同步发出
                # turn_error；否则外层会把正常 return 误判为 turn_finished。
                _notify_event(event_callback, "turn_error", {"message": msg})
                return
            if harness_runtime is not None:
                current_step = harness_runtime.begin_step()
            _drained_stop_notices = _stop_notice.drain_stop_notices()
            if _drained_stop_notices:
                for _stop_notice_text in _drained_stop_notices:
                    _stop_notice_msg = {
                        "role": "user",
                        "content": _stop_notice_text,
                    }
                    messages.append(_stop_notice_msg)
                    pending_persistence.append(("user", _stop_notice_msg))
                drained_stop_notices.extend(_drained_stop_notices)
                flush_persistence()
            if budget_notice_provider is not None:
                try:
                    _budget_notice_text = budget_notice_provider()
                except Exception:
                    _budget_notice_text = None
                if _budget_notice_text:
                    _budget_notice_msg = {
                        "role": "user",
                        "content": str(_budget_notice_text),
                    }
                    messages.append(_budget_notice_msg)
                    pending_persistence.append(("user", _budget_notice_msg))
                    flush_persistence()
                    _notify_event(
                        event_callback,
                        "budget_notice",
                        {"message": str(_budget_notice_text)},
                    )
            if turn > 0:
                _notify_event(event_callback, "round_started", {"round": turn + 1})

            response_stream = None
            inflight_holder = None
            # 多 Key 粘性故障转移：每个 Step 重建「本请求链已尝试 Key」集合
            # （同 Step 重试循环共享、跨 Step 重置），Key 池自身（sticky/冷却）
            # 为进程内存态跨 Step 保留。单 Key / 路由未知 / 任何异常时
            # key_failover=None，行为与现状完全一致；绝不打印 Key 明文。
            key_failover = None
            try:
                from core.provider_keys import build_key_failover

                key_failover = build_key_failover(
                    model_name, api_base, model_id=request_model_id
                )
                if key_failover is not None:
                    kwargs["api_key"] = key_failover.initial_key()
            except Exception:
                key_failover = None
                logger.debug("多 Key 池初始化失败，退回单 Key 行为", exc_info=True)
            key_tried = set()
            max_api_retries = 5
            if key_failover is not None:
                max_api_retries = max(5, key_failover.key_count + 2)
            base_delay = 1.0
            overflow_compress_attempts = 0
            rotate_deepseek_generation = False
            classified_message_shape = None
            request_cache_reason = "冷启动"
            request_diagnostic = None
            active_deepseek_generation_id = None
            active_deepseek_message_count = None
            active_deepseek_rotation_reason = ""
            cache_control_downgraded = False
            affinity_downgraded = False
            wait_started_at = None
            prepared_request = None

            for attempt in range(max_api_retries):
                raise_if_cancelled()
                try:
                    effective_request_tools = request_tools
                    active_run_scope = (
                        computer_run_id
                        if computer_controller is not None and computer_run_id
                        else None
                    )
                    cleaned_current, folded_count, keep_media = _strip_media_and_merge(
                        messages,
                        model_name,
                        api_base,
                        active_computer_run_id=active_run_scope,
                    )
                    cleaned_messages = cleaned_current
                    if deepseek_ledger_enabled:
                        ledger_request = _prepare_deepseek_ledger_request(
                            deepseek_ledger_manager,
                            parent_session,
                            model_name,
                            api_base,
                            cache_scope,
                            cleaned_current,
                            request_tools,
                            force_new=rotate_deepseek_generation,
                        )
                        rotate_deepseek_generation = False
                        cleaned_messages = ledger_request["messages"]
                        effective_request_tools = ledger_request["tools"]
                        kwargs["tools"] = effective_request_tools
                        active_deepseek_generation_id = ledger_request["generation_id"]
                        active_deepseek_message_count = len(cleaned_messages)
                        active_deepseek_rotation_reason = (
                            ledger_request.get("rotation_reason") or ""
                        )
                    if _is_kimi_family(model_name, api_base):
                        for _m in cleaned_messages:
                            if (
                                _m.get("role") == "assistant"
                                and _m.get("tool_calls")
                                and not _m.get("reasoning_content")
                            ):
                                _m["reasoning_content"] = " "
                    current_message_shape = _cache_message_fingerprints(cleaned_messages)
                    if current_message_shape != classified_message_shape:
                        from core.route_overrides import (
                            resolve_route_override,
                        )

                        request_diagnostic = _classify_cache_request_details(
                            telemetry_session_id,
                            cache_scope,
                            model_name,
                            api_base,
                            _tool_contract_fingerprint(effective_request_tools),
                            cleaned_messages,
                            tools=effective_request_tools,
                            route_override_fingerprint=(
                                resolve_route_override(model_name, api_base)["fingerprint"]
                            ),
                        )
                        request_cache_reason = request_diagnostic["request_reason"]
                        if active_deepseek_rotation_reason:
                            request_diagnostic["request_reason"] = (
                                active_deepseek_rotation_reason
                            )
                            request_cache_reason = active_deepseek_rotation_reason
                        classified_message_shape = current_message_shape
                    kwargs["messages"] = _apply_cache_control(
                        cleaned_messages,
                        model_name,
                        api_base,
                        disabled=cache_control_downgraded,
                    )
                    if request_diagnostic is not None:
                        (
                            request_diagnostic["cache_control_markers"],
                            request_diagnostic["request_bytes_final"],
                        ) = _cache_control_request_metrics(kwargs["messages"])
                    try:
                        _est_prompt_tokens = int(estimate_tokens(model_name, cleaned_messages) or 0)
                    except Exception:
                        _est_prompt_tokens = 0
                    attempt_timeout = min(
                        API_REQUEST_MAX_TIMEOUT_SECONDS,
                        API_REQUEST_TIMEOUT_SECONDS + max(0, _est_prompt_tokens) // 1500,
                    )
                    if native_continuation_state is not None:
                        prepared_request = prepare_native_continuation_request(
                            native_continuation_state,
                            native_continuation_output,
                            timeout=attempt_timeout,
                        )
                    else:
                        step_native_tools = ()
                        if native_computer_capability is not None:
                            selected_target = (
                                native_selection.target
                                if native_selection is not None
                                else None
                            )
                            step_native_tools = build_openai_computer_tools(
                                native_computer_capability, selected_target
                            )
                            if native_has_dual_targets(native_computer_capability):
                                kwargs["parallel_tool_calls"] = False
                        prepared_request = prepare_stream_request(
                            request_protocol,
                            kwargs,
                            attempt_timeout,
                            native_computer_tools=step_native_tools,
                        )
                    # 指纹层只观察请求：序列化失败（NaN/不可序列化对象等）不能
                    # 杀掉主请求，跳过捕获并告警（该 step 缺指纹，不影响续跑——
                    # resume 对缺指纹的 started-not-finished 一律 BLOCK）
                    try:
                        request_envelope = RequestEnvelope.capture(
                            prepared_request,
                            cache_scope=cache_scope,
                            request_reason=request_cache_reason,
                            attempt=attempt + 1,
                            deepseek_generation_id=active_deepseek_generation_id,
                            deepseek_message_count=active_deepseek_message_count,
                            turn_reason=request_reason,
                        )
                    except Exception:
                        logger.warning("请求指纹捕获失败（跳过，不影响请求）", exc_info=True)
                        request_envelope = None
                    if current_step is not None and request_envelope is not None:
                        current_step.capture_request(request_envelope)
                    future = _submit_daemon_call(
                        _request_stream,
                        prepared_request,
                        cancel_event=cancel_event,
                    )
                    wait_started_at = time.monotonic()
                    last_wait_notice_at = wait_started_at
                    try:
                        while not future.done():
                            raise_if_cancelled()
                            try:
                                future.result(timeout=0.1)
                            except Exception:
                                # result(timeout=0.1) 在超时或异常时都会抛；
                                # 交给下面统一处理 future.exception()
                                pass
                            # 等待超过阈值后按间隔报一次等待状态，避免大载荷请求
                            # 长时间无反馈被误判为“卡死”（重试提示只在异常后才有）
                            waited = time.monotonic() - wait_started_at
                            if waited >= REQUEST_WAIT_GIVE_UP_SECONDS:
                                # 等待硬上限：上层重试会把单次超时在
                                # 同一 Step 内放大数倍，这里按客户端真实等待时长兜底，
                                # 到点结束本轮而非无限等
                                raise RequestWaitGiveUpError(
                                    f"等待模型响应已达 {int(waited)} 秒"
                                    f"（约 {int(REQUEST_WAIT_GIVE_UP_SECONDS // 60)} 分钟上限）"
                                    "仍未收到首字节，本轮已自动结束；"
                                    "可重试、缩小上下文或检查模型服务状态"
                                )
                            if (
                                waited >= WAIT_NOTICE_THRESHOLD_SECONDS
                                and time.monotonic() - last_wait_notice_at >= WAIT_NOTICE_INTERVAL_SECONDS
                            ):
                                last_wait_notice_at = time.monotonic()
                                wait_message = (
                                    f"模型响应中，已等待 {int(waited)} 秒"
                                    f"（本轮载荷约 {_est_prompt_tokens // 1000}k tokens，超时上限 {attempt_timeout} 秒）；"
                                    "超大上下文与深度思考可能较慢，可按 Ctrl+C 中断"
                                )
                                if event_callback is None:
                                    print(f"\n[⏳ {wait_message}]")
                                else:
                                    _notify_event(event_callback, "status", {"message": wait_message})
                                    yield ""
                        if future.cancelled():
                            raise KeyboardInterrupt("用户终止了对话。")
                        exc = future.exception()
                        if exc is not None:
                            raise exc
                        response_stream, inflight_holder = future.result()
                        if current_step is not None:
                            current_step.mark_streaming()
                    finally:
                        # 用户取消时避免 shutdown(wait=True) 阻塞到网络请求完成（最长 90s）
                        future.cancel()
                    if key_failover is not None:
                        # 粘性锚定到本次成功的 Key 并清除其冷却；
                        # 记账失败绝不影响请求成功本身
                        try:
                            key_failover.on_success(str(kwargs.get("api_key") or ""))
                        except Exception:
                            pass
                    break
                except Exception as api_err:
                    raise_if_cancelled()
                    if isinstance(api_err, RequestWaitGiveUpError):
                        raise
                    err_str = str(api_err).lower()
                    # 上下文窗口超限自动压缩历史记录并重试：
                    # 优先按异常类型判断，字符串匹配作为兜底且要求上下文相关关键词组合，
                    # 避免把 rate limit 等错误误判为超限
                    is_context_overflow = isinstance(api_err, _context_overflow_error())
                    if not is_context_overflow:
                        is_context_overflow = (
                            "prompt is too long" in err_str
                            or "context window" in err_str
                            or "context_length" in err_str
                            or (
                                "exceeds" in err_str
                                and ("context" in err_str or "token" in err_str or "length" in err_str)
                            )
                        )
                    if is_context_overflow:
                        if overflow_compress_attempts >= 1:
                            raise RuntimeError(
                                "历史压缩后仍超出模型上下文窗口，本轮请求已中止，"
                                "以避免反复重发超大载荷空耗额度。"
                                "建议 /new 开新会话，或移除超大附件/图片后重试。"
                            ) from api_err
                        overflow_compress_attempts += 1
                        if _is_ephemeral_subagent_turn(stream, parent_session, cache_scope):
                            if event_callback is not None:
                                _notify_event(event_callback, "status", {
                                    "message": "子代理上下文窗口超限，正在精简内部执行历史后重试。",
                                })
                            if not _compact_ephemeral_messages_in_place(messages):
                                raise RuntimeError(
                                    "子代理单步载荷已超出模型上下文窗口且无法继续精简，"
                                    "请缩小子任务范围或拆分为多个子代理后重试。"
                                ) from api_err
                            turn_snapshot = deepcopy(messages)
                            if attempt < max_api_retries - 1:
                                continue
                            raise api_err
                        try:
                            if event_callback is None:
                                print("\n[⚠️ 检测到上下文窗口超限，正在尝试压缩历史记录以缩减 Prompt 长度...]")
                            else:
                                _notify_event(event_callback, "status", {
                                    "message": "检测到上下文窗口超限，正在压缩历史记录。",
                                })
                            from core.memory_compressor import compress_session_memory
                            # 复用全局单例 manager（共享 SQLite 连接 + 锁），
                            # 避免每次新建 MemoryManager 导致连接泄漏
                            manager = app_context.get_manager()
                            # 子代理回合必须压缩自己的会话（parent_session），
                            # 不能误压主 UI 会话
                            session = parent_session or app_context.get_current_session()
                            compress_session_memory(
                                manager,
                                session,
                                api_model=model_name,
                                api_key=api_key,
                                api_url=api_base,
                                force=True,
                                output_callback=(
                                    (lambda msg: _notify_event(event_callback, "status", {"message": re.sub(r"<[^>]+>", "", msg)}))
                                    if event_callback is not None else None
                                ),
                            )
                            # 原地替换为压缩后历史：禁止重新绑定局部名，
                            # 否则调用方持有的旧列表会失联、取消回滚也会作用错对象
                            _refill_messages_after_compression(messages, manager, session)
                            if deepseek_ledger_enabled:
                                rotate_deepseek_generation = True
                            # 压缩已成功落库：同步回合快照，避免后续异常回滚把
                            # 内存恢复成未压缩状态（与 DB 分叉且下轮必再超限）
                            turn_snapshot = deepcopy(messages)
                            if attempt < max_api_retries - 1:
                                continue
                            raise api_err
                        except Exception as compress_err:
                            if event_callback is None:
                                from prompt_toolkit.formatted_text import HTML

                                safe_print(HTML("<ansiyellow>历史记录暂未完成压缩</ansiyellow>"))
                            else:
                                _notify_event(event_callback, "error", {
                                    "message": str(compress_err),
                                })

                    # 上游以 400 明确拒绝 cache_control：本轮剥离标记立即重发，
                    # 并按路由临时拉黑（TTL 到期自愈），避免整轮 5 次重试全部撞墙
                    if not cache_control_downgraded and _is_cache_control_rejection(api_err):
                        cache_control_downgraded = True
                        _disable_cache_control_route(model_name, api_base)
                        warn_msg = (
                            "上游拒绝 cache_control 参数 (400)，已剥离标记并降级重试；"
                            f"该路由的显式断点在 {int(_CACHE_CONTROL_REJECTION_TTL_SECONDS // 60)} 分钟内不再启用。"
                        )
                        if event_callback is None:
                            print(f"\n[⚠️ {warn_msg}]")
                        else:
                            _notify_event(event_callback, "status", {"message": warn_msg})
                        if attempt < max_api_retries - 1:
                            continue

                    # 上游以 400 明确拒绝 Kimi 缓存亲和键（prompt_cache_key /
                    # metadata.user_id）：本轮剥离亲和字段立即重发，并按路由
                    # 临时拉黑（TTL 到期自愈），与 cache_control 降级完全同构。
                    if not affinity_downgraded and _is_kimi_affinity_rejection(api_err):
                        affinity_downgraded = True
                        _disable_affinity_route(model_name, api_base)
                        _strip_cache_affinity_options(kwargs)
                        warn_msg = (
                            "上游拒绝缓存亲和参数 (400)，已剥离并降级重试；"
                            f"该路由的缓存亲和键在 {int(_CACHE_CONTROL_REJECTION_TTL_SECONDS // 60)} 分钟内不再启用。"
                        )
                        if event_callback is None:
                            print(f"\n[⚠️ {warn_msg}]")
                        else:
                            _notify_event(event_callback, "status", {"message": warn_msg})
                        if attempt < max_api_retries - 1:
                            continue

                    # 修复③：额度耗尽 / 鉴权失败 / 权限受限 / 模型不存在等确定性失败
                    # 重试无意义，只会反复空烧额度、拖长用户等待；命中即直接抛出，
                    # 走既有失败收尾（与五次重试耗尽后的路径一致）。分类失败保守回退
                    # 既有重试行为，不改变瞬时错误（429 等）的退避口径。
                    if _is_deterministic_api_failure(api_err):
                        # 多 Key 粘性故障转移：Key 类失败（额度/鉴权/权限）静默切换
                        # 到下一个未试过的 Key 重发——不发任何 UI 事件，仅 logger
                        # 留痕（绝不打印 Key 明文）；其余确定性失败维持 fail-fast；
                        # 全部 Key 试完 → 抛原错误走既有失败收尾。
                        if key_failover is not None and _is_key_related_failure(api_err):
                            current_key = str(kwargs.get("api_key") or "")
                            key_tried.add(current_key)
                            next_key = None
                            try:
                                next_key = key_failover.on_failure(current_key, key_tried)
                            except Exception:
                                next_key = None
                            if next_key:
                                kwargs["api_key"] = next_key
                                logger.info(
                                    "多 Key 静默切换：厂商 Key 不可用，"
                                    "已切换下一个 Key 重发（不打印 Key 值）"
                                )
                                continue
                        raise api_err

                    if attempt < max_api_retries - 1:
                        is_rate_limit, retry_after = is_rate_limit_error(api_err)
                        if is_rate_limit:
                            if retry_after is not None and retry_after > 0:
                                sleep_time = retry_after + (random.random() * 0.5)
                            else:
                                rate_limit_base_delay = 5.0
                                sleep_time = (rate_limit_base_delay * (2 ** attempt)) + (random.random() * 1.0)
                            retry_message = (
                                f"触发 API 速率限制，正在进行第 {attempt+1}/{max_api_retries} 次重试，"
                                f"等待 {sleep_time:.1f} 秒"
                            )
                        else:
                            sleep_time = (base_delay * (2 ** attempt)) + (random.random() * 0.5)
                            retry_message = (
                                f"API 暂未成功，正在进行第 {attempt+1}/{max_api_retries} 次重试，"
                                f"等待 {sleep_time:.1f} 秒"
                            )
                        if event_callback is None:
                            print(f"\n[⚠️ {retry_message}]")
                        else:
                            _notify_event(event_callback, "status", {"message": retry_message})
                        # 将 sleep 切成小段并检查取消事件，避免用户按 Ctrl+C 后长时间无响应
                        sleep_deadline = time.monotonic() + sleep_time
                        while time.monotonic() < sleep_deadline:
                            raise_if_cancelled()
                            remaining = sleep_deadline - time.monotonic()
                            time.sleep(min(0.1, remaining) if remaining > 0 else 0.1)
                    else:
                        raise api_err

            # 流式阶段「零产出 + 连接被对端回收」允许换连接重发一次：此时
            # 用户还没看到任何内容，重发与首次请求字节完全等价；一旦已产出
            # 可见内容，重发会造成重复，必须放弃并把异常交回上层兜底。
            stream_reconnect_used = False
            while True:
                stream_assembler = StreamAssembler(
                    keep_reasoning_fields=_supports_reasoning_fields(
                        model_name, api_base
                    ),
                    keep_thinking_blocks=(
                        str(model_name or "").split("/")[0].lower() == "anthropic"
                    ),
                    require_tool_call_reasoning=_requires_tool_call_reasoning(
                        model_name, api_base
                    ),
                )
                first_token_time = None
                try:
                    for chunk in _stream_chunks_with_idle_notice(
                        response_stream, event_callback, raise_if_cancelled,
                        inflight_holder=inflight_holder, cancel_event=cancel_event,
                    ):
                        raise_if_cancelled()
                        if chunk is None:
                            yield ""
                            continue
                        if first_token_time is None:
                            first_token_time = time.monotonic()
                        update = stream_assembler.consume(chunk)
                        if update.public_text:
                            yield update.public_text
                    break
                except Exception as stream_err:
                    if (
                        stream_reconnect_used
                        or prepared_request is None
                        or stream_assembler.has_visible_output
                        or not _is_retryable_stream_interruption(stream_err)
                    ):
                        raise
                    stream_reconnect_used = True
                    logger.warning(
                        "模型流式连接被对端回收且本轮零产出，换新连接重发一次：%s",
                        _sanitize_error_text(str(stream_err)),
                    )
                    _notify_event(event_callback, "status", {
                        "message": "模型流式连接被服务端回收（本轮尚未产出内容），正在换新连接重发一次。",
                    })
                    try:
                        response_stream.close()
                    except Exception:
                        pass
                    response_stream, inflight_holder = _request_stream(
                        prepared_request, cancel_event=cancel_event
                    )

            _tail_chunks = getattr(response_stream, "chunks", None)
            if _tail_chunks:
                for _tail_chunk in reversed(_tail_chunks):
                    _tail_usage = getattr(_tail_chunk, "usage", None)
                    if _tail_usage is not None:
                        stream_assembler.observe_usage(_tail_usage)
                        break

            assembled_response = stream_assembler.finish()
            if stream_assembler.finish_public_text:
                yield stream_assembler.finish_public_text

            finish_reason = assembled_response.finish_reason
            stream_usage = assembled_response.usage

            stream_end_time = time.monotonic()
            comp_tokens = 0
            if stream_usage is not None:
                try:
                    norm = _normalize_server_usage(stream_usage)
                    comp_tokens = int(norm.get("completion_tokens") or 0)
                except Exception:
                    comp_tokens = 0
            if comp_tokens == 0 and (assembled_response.content or assembled_response.reasoning_content):
                try:
                    full_len = len(assembled_response.content or "") + len(assembled_response.reasoning_content or "")
                    comp_tokens = max(1, int(full_len / 2))
                except Exception:
                    comp_tokens = 0

            step_start_time = (
                wait_started_at
                if wait_started_at is not None
                else (first_token_time or stream_end_time)
            )
            duration_s = max(round(stream_end_time - step_start_time, 2), 0.01)

            ttft_ms = (
                max(0, int(round((first_token_time - step_start_time) * 1000)))
                if first_token_time is not None
                else None
            )

            raw_output_duration = (
                (stream_end_time - first_token_time)
                if first_token_time is not None
                else 0.0
            )
            is_batch_delivery = (
                raw_output_duration < 0.15 and comp_tokens > 10
            ) or (
                raw_output_duration > 0 and (comp_tokens / raw_output_duration) > 1000.0
            )

            if is_batch_delivery or first_token_time is None:
                output_duration_s = duration_s
            else:
                output_duration_s = max(round(raw_output_duration, 2), 0.01)

            raw_speed = (
                (comp_tokens / output_duration_s)
                if (comp_tokens > 0 and output_duration_s > 0)
                else 0.0
            )
            # 物理上限防毛刺：真实大模型推理（即便是 Groq/Cerebras）极限不超过 ~1000 tok/s；
            # 若因极短网络抖动或单元测试 mock 导致除算溢出，平滑限幅至 1000.0 t/s，杜绝几万 t/s 异常显示
            token_speed = round(min(raw_speed, 1000.0), 1)

            latest_step_metrics = {
                "duration_s": duration_s,
                "output_duration_s": output_duration_s,
                "ttft_ms": ttft_ms,
                "completion_tokens": comp_tokens,
                "token_speed": token_speed,
            }
            _notify_event(event_callback, "step_metrics", latest_step_metrics)

            # 输出被模型长度限制截断：给出明确提示，避免用户以为任务已完成。
            if finish_reason == "length":
                truncated_notice = (
                    "\n[回复已被模型长度限制截断。如需完整结果，请尝试："
                    "1) 让模型分步骤输出；2) 换用输出上限更高的模型；"
                    "3) 在 config.json 中为当前模型增大 max_tokens。]"
                )
                _notify_event(event_callback, "notice", {"message": truncated_notice.strip()})

            if cache_scope == "main" and keepalive_interval:
                cache_keepalive_service.register_request(
                    kwargs,
                    interval=keepalive_interval,
                    telemetry={
                        "model_name": model_name,
                        "api_base": api_base,
                        "session_id": telemetry_session_id,
                    },
                    session_key=telemetry_session_id,
                )

            _persist_cache_request_diagnostic(request_diagnostic, stream_usage)
            if stream_usage is not None:
                _account_server_usage(
                    stream_usage,
                    model_name=model_name,
                    api_base=api_base,
                    session_id=telemetry_session_id,
                    cache_scope=cache_scope,
                    invalidation_reason=request_cache_reason,
                )

            if native_computer_capability is not None:
                if assembled_response.computer_call is None:
                    native_continuation_state = None
                    native_continuation_output = None
                else:
                    if assembled_response.tool_calls:
                        # computer_call 与普通 function call 混用：静默丢弃
                        # function call 会造成不可审计的副作用缺口，整批拒绝。
                        raise ComputerControlError(
                            "computer_provider_protocol_error",
                            "同一个 Provider response 同时出现 computer_call 与 "
                            "function call，混用批次拒绝执行",
                        )
                    native_target = (
                        native_selection.target
                        if native_selection is not None
                        else (
                            native_computer_capability.targets[0]
                            if len(native_computer_capability.targets) == 1
                            else None
                        )
                    )
                    native_call = native_computer_call_from_stream(
                        assembled_response.computer_call, target=native_target
                    )
                    if (
                        native_selection is not None
                        and native_selection.run_id
                        and not computer_run_id
                    ):
                        computer_controller = native_selection.controller
                        computer_owner = native_selection.owner
                        computer_run_id = native_selection.run_id
                    if not computer_run_id:
                        computer_controller = get_computer_control_service()
                        computer_owner = ComputerOwner(
                            session_id=str(parent_session or ""),
                            turn_id=str(parent_turn_id or ""),
                            session_label=str(parent_session or parent_turn_id or ""),
                            cancel_event=(
                                cancel_event
                                if cancel_event is not None
                                else threading.Event()
                            ),
                        )
                        computer_run_id = str(
                            computer_controller.start_run(
                                computer_owner,
                                native_computer_capability,
                                target=native_target,
                            ).run_id
                        )
                    native_result = execute_native_computer_call(
                        native_call,
                        BoundNativeController(
                            computer_controller, computer_run_id, computer_owner
                        ),
                    )
                    if native_result.status == "provider_safety_required":
                        # 设计 11.5：无已验证确认契约——不执行动作、不发起
                        # 后续 Step，run 留在 provider_safety_required 等用户
                        # 显式停止；绝不伪造 acknowledged safety checks，也
                        # 不回退 generic 绕过。check 原文不进入错误消息。
                        check_ids = ", ".join(
                            check.id for check in native_call.pending_safety_checks
                        )
                        raise ComputerControlError(
                            "computer_provider_safety_required",
                            "Provider 返回强制安全检查"
                            + (f"（check ids: {check_ids}）" if check_ids else "")
                            + "，未确认前不执行任何动作；请查看状态并停止",
                        )
                    if current_step is not None:
                        current_step.mark_assistant_ready()
                        current_step.mark_tools_running()
                    # 脱敏中间行投影（brief Step 5 逐字形状）：输入文字只存
                    # code point 数与 SHA-256，截图只存 screen hash/尺寸。
                    native_row = build_native_call_persistence_row(
                        computer_run_id, native_call, native_result.observation
                    )
                    if stream:
                        pending_persistence.append(("assistant", native_row))
                    flush_persistence()
                    if current_step is not None:
                        current_step.mark_tools_committed()
                        if turn + 1 < max_turns:
                            current_step.complete()
                    native_continuation_state = NativeContinuationState(
                        model=str(
                            prepared_request.kwargs.get("model") or model_name
                        ),
                        tools=tuple(
                            deepcopy(prepared_request.kwargs.get("tools") or ())
                        ),
                        instructions=_native_continuation_instructions(
                            prepared_request.kwargs
                        ),
                        options=deepcopy({
                            key: prepared_request.kwargs[key]
                            for key in NATIVE_CONTINUATION_OPTION_KEYS
                            if key in prepared_request.kwargs
                        }),
                        response_id=native_call.response_id,
                    )
                    native_continuation_output = build_computer_call_output(
                        native_call,
                        native_result.observation,
                        controller=computer_controller,
                    )
                    continue

            reasoning_content = stream_assembler.raw_reasoning_content
            public_content = assembled_response.content
            from core.turn_renderer import sanitize_protocol_text
            public_content = sanitize_protocol_text(public_content)

            raw_content_meaningful = _is_meaningful_assistant_content(public_content)
            # 促升前的原始正文快照：退化（复读）检测的指纹必须取这里的值。
            # 一旦走到下面的促升分支，public_content 会被改写成思考预览，
            # 此时再取指纹等于拿思考内容当正文指纹（口径错误，见下方退化检测块）。
            pre_promotion_content = public_content
            promoted_reasoning = False
            if (
                not assembled_response.tool_calls
                and not raw_content_meaningful
                and _is_meaningful_assistant_content(reasoning_content)
            ):
                reasoning_preview = reasoning_content.strip()
                if len(reasoning_preview) > PROMOTED_REASONING_PREVIEW_CHARS:
                    reasoning_preview = (
                        reasoning_preview[:PROMOTED_REASONING_PREVIEW_CHARS].rstrip()
                        + " …（思考内容过长，已截断）"
                    )
                public_content = reasoning_preview + "\n\n[以上为模型思考过程，未生成正式回复]"
                promoted_reasoning = True

            # 只允许公开正文写入记忆，避免把原生推理误当作长期事实。
            memory_tags = mk.extract_memory_tags(public_content)
            for tag in memory_tags:
                try:
                    mk.append_to_daily_log(tag)
                except Exception as log_err:
                    if event_callback is None:
                        print(f"\n[⚠️ 写入日记账记忆失败: {str(log_err)}]")
                    else:
                        _notify_event(event_callback, "error", {
                            "message": f"写入日记账记忆失败：{str(log_err)}",
                        })

            tool_calls_list = assembled_response.tool_calls
            current_tool_calls = tool_calls_list
            replay_reasoning_content = assembled_response.reasoning_content
            provider_thinking_blocks = assembled_response.thinking_blocks

            if not tool_calls_list:
                recent_content_fingerprints.append(
                    _degenerate_fingerprint(pre_promotion_content)
                )
                if _is_degenerate_repeat(recent_content_fingerprints):
                    if degenerate_nudge_count < _DEGENERATE_MAX_NUDGES:
                        degenerate_nudge_count += 1
                        _notify_event(event_callback, "notice", {
                            "message": "检测到模型连续多步复读相同内容，已要求其立即收敛并给出结论。",
                        })
                        # 与慢步熔断同一通道：同时写内存与持久化，并立即 flush，
                        # 否则下一轮重建历史时这条催促会丢失。
                        degenerate_nudge = {
                            "role": "user",
                            "content": SLOW_STEP_NUDGE_TEXT,
                        }
                        messages.append(degenerate_nudge)
                        pending_persistence.append(("user", degenerate_nudge))
                        flush_persistence()
                        progress_guard.observe(
                            _step_progress_observation(None, session_id=parent_session)
                        )
                        if current_step is not None:
                            current_step.mark_assistant_ready()
                            if turn + 1 < max_turns:
                                current_step.complete()
                        continue
                    _notify_event(event_callback, "error", {
                        "message": "模型连续多步复读相同内容且未收敛，已触发退化熔断结束本回合。",
                    })
                    if promoted_reasoning and public_content:
                        yield from _yield_content(public_content)
                    flush_persistence()
                    if current_step is not None:
                        current_step.mark_assistant_ready()
                        current_step.complete()
                    if harness_runtime is not None:
                        harness_runtime.complete()
                    computer_exit_kind = "completed"
                    return

            if (
                duration_s > SLOW_STEP_ABORT_SECONDS
                and not raw_content_meaningful
                and not tool_calls_list
            ):
                if slow_step_nudge_count < SLOW_STEP_MAX_NUDGES:
                    slow_step_nudge_count += 1
                    _notify_event(event_callback, "notice", {
                        "message": "检测到单步长时间无产出，已要求模型立即收敛输出结论。",
                    })
                    slow_step_nudge = {
                        "role": "user",
                        "content": SLOW_STEP_NUDGE_TEXT,
                    }
                    # 空的 assistant 消息（content 为 None）不入库也不留内存，只追加
                    # 强催促；催促使消息同样需要持久化，避免下一轮重建历史时上下文缺失。
                    messages.append(slow_step_nudge)
                    pending_persistence.append(("user", slow_step_nudge))
                    flush_persistence()
                    progress_guard.observe(
                        _step_progress_observation(None, session_id=parent_session)
                    )
                    if current_step is not None:
                        current_step.mark_assistant_ready()
                        if turn + 1 < max_turns:
                            current_step.complete()
                    continue
                # 达到催促上限仍空转：结束循环，返回已产出的内容，避免继续消耗。
                _notify_event(event_callback, "error", {
                    "message": "模型连续多步耗时过长且无任何产出，已触发慢步熔断结束本回合。",
                })
                if promoted_reasoning and public_content:
                    yield from _yield_content(public_content)
                flush_persistence()
                if current_step is not None:
                    current_step.mark_assistant_ready()
                    current_step.complete()
                if harness_runtime is not None:
                    harness_runtime.complete()
                computer_exit_kind = "completed"
                return


            message_to_append = {
                "role": "assistant",
                "content": public_content or None,
                "reasoning_content": replay_reasoning_content,
            }
            if provider_thinking_blocks is not None:
                message_to_append["thinking_blocks"] = provider_thinking_blocks
            if tool_calls_list:
                message_to_append["tool_calls"] = tool_calls_list
            messages.append(message_to_append)
            if current_step is not None:
                current_step.mark_assistant_ready()

            persisted_message = {
                "role": "assistant",
                "content": public_content or None,
                "reasoning_content": replay_reasoning_content,
            }
            if provider_thinking_blocks is not None:
                persisted_message["thinking_blocks"] = provider_thinking_blocks
            if tool_calls_list:
                # 设计 22.3/22.4（Task 16 隐私审计）：computer 工具调用的输入
                # 正文绝不落库——持久化副本换脱敏投影；Provider 请求侧的
                # message_to_append 保持原始参数（assistant tool_calls 与 tool
                # 结果必须配对原样重发），下一轮历史回放读到的是脱敏参数。
                persisted_message["tool_calls"] = _persisted_tool_calls(
                    tool_calls_list
                )
            persisted_message.update(_model_display_snapshot(latest_step_metrics))
            # 落库副本提取媒体清单（![..](..) / [video:..](..) 语法归档进内容寻址
            # 仓库）。只改落库副本：message_to_append 不动，Provider 请求字节零变化；
            # 基础设施错误（仓库不可写等）按契约直接向持久化路径传播，显式失败。
            if stream:
                from core.media_refs import attach_media_manifest
                from core.workspace_context import get_workdir
                attach_media_manifest(persisted_message, get_workdir())

            # 用"促升前"的原始正文判断：思考内容被促升为正文后不能算作有效结论，
            # 否则模型纯思考收尾时会跳过最终总结重试，用户什么都看不到。
            # 除"工具跑完却没给结论"外，"只输出思考、既没调工具也没给正文"同样是
            # 模型未完成本轮任务的表现（常见于模型本打算调工具却只输出了 reasoning），
            # 也纳入催促重试，避免把整段思考当作回合结果草草收尾。
            needs_final_summary = (
                not tool_calls_list
                and not raw_content_meaningful
                and (tool_activity_seen or promoted_reasoning)
            )
            # 只有真正要走重试分支时才跳过持久化（重试会 pop 掉这条消息）；
            # 重试机会用尽后的最终消息必须照常入库，否则内存与 DB 分叉。
            # 连续"只思考不产出"最多重试 FINAL_SUMMARY_MAX_RETRIES 次（共
            # FINAL_SUMMARY_MAX_RETRIES+1 次机会），避免长任务中模型一两轮
            # 欠佳就静默收尾，造成用户感知的"任务未执行完就结束"。
            will_retry_summary = (
                needs_final_summary
                and final_summary_retry_count < FINAL_SUMMARY_MAX_RETRIES
            )

            if stream and not will_retry_summary:
                pending_persistence.append(("assistant", persisted_message))

            if deepseek_ledger_enabled and not will_retry_summary:
                canonical_assistant, _, _ = _strip_media_and_merge(
                    [message_to_append], model_name, api_base
                )
                deepseek_ledger_manager.append_deepseek_prompt_messages(
                    active_deepseek_generation_id,
                    canonical_assistant,
                    expected_count=active_deepseek_message_count,
                )

            if not tool_calls_list:
                if will_retry_summary:
                    final_summary_retry_count += 1
                    length_truncated = finish_reason == "length"
                    if "max_completion_tokens" in kwargs:
                        _cur_max = int(kwargs.get("max_completion_tokens") or 8192)
                        kwargs["max_completion_tokens"] = min(_cur_max * 2, 32768)
                    else:
                        _cur_max = int(kwargs.get("max_tokens") or 8192)
                        kwargs["max_tokens"] = min(_cur_max * 2, 32768)
                    length_hint = (
                        "上一轮回复因输出长度限制被截断：请大幅压缩思考过程，"
                        "尽快开始输出正式内容。"
                        if length_truncated else ""
                    )
                    messages.pop()
                    if tool_activity_seen:
                        nudge_text = SUMMARY_NUDGE_TOOL_DONE_TEXT + length_hint
                    else:
                        nudge_text = SUMMARY_NUDGE_THINKING_ONLY_TEXT + length_hint
                    summary_request = {
                        "role": "user",
                        "content": nudge_text,
                    }
                    messages.append(summary_request)
                    # 该 user 催促消息也需要持久化，否则下一轮重建历史时上下文会缺失
                    pending_persistence.append(("user", summary_request))
                    flush_persistence()
                    progress_guard.observe(
                        _step_progress_observation(None, session_id=parent_session)
                    )
                    if current_step is not None and turn + 1 < max_turns:
                        current_step.complete()
                    # 内部兜底重试静默进行：底部状态栏已显示"正在干活..."，
                    # 这里不再发 status 通知暴露"模型未生成结论"的内部细节。
                    continue
                if (
                    verify_claim_nudge_count < VERIFY_CLAIM_MAX_NUDGES
                    and _claims_verification_without_evidence(
                        public_content, used_write_tools, used_command_tools
                    )
                ):
                    verify_claim_nudge_count += 1
                    _notify_event(event_callback, "notice", {
                        "message": "检测到验证声明但本回合未执行任何命令，已要求模型补充真实运行输出。",
                    })
                    claim_nudge = {
                        "role": "user",
                        "content": VERIFY_CLAIM_NUDGE_TEXT,
                    }
                    messages.append(claim_nudge)
                    # 催促消息同样需要持久化，否则下一轮重建历史时上下文会缺失
                    pending_persistence.append(("user", claim_nudge))
                    flush_persistence()
                    progress_guard.observe(
                        _step_progress_observation(None, session_id=parent_session)
                    )
                    if current_step is not None and turn + 1 < max_turns:
                        current_step.complete()
                    continue
                if allow_orchestration and parent_session and plan_continue_nudges < 2:
                    try:
                        from core.plan import get_default_plan_manager as _get_plan_for_nudge
                        from core.plan import (
                            clear_plan_auto_continue_suppression as _clear_nudge_suppress,
                            plan_auto_continue_suppressed as _nudge_suppressed,
                        )
                        _nudge_plan = _get_plan_for_nudge()
                        _nudge_ready = [
                            item for item in _nudge_plan.ready_items()
                            if item.get("kind") != "input_preparation"
                        ] if _nudge_plan.session_id == str(parent_session) else []
                        _nudge_running = any(
                            item.get("status") == "in_progress"
                            for item in _nudge_plan.snapshot()
                        ) if _nudge_plan.session_id == str(parent_session) else False
                        # 用户明确要求继续（计划项重新进入执行）或计划已清空时，
                        # 解除“用户取消”的续推抑制；否则跳过本次 nudge，避免把
                        # 刚被用户取消的任务复活
                        if _nudge_running or not _nudge_plan.snapshot():
                            _clear_nudge_suppress(parent_session)
                        _nudge_skip = _nudge_suppressed(parent_session)
                    except Exception:
                        _nudge_ready, _nudge_running, _nudge_skip = [], False, False
                    if _nudge_ready and not _nudge_running and not _nudge_skip:
                        plan_continue_nudges += 1
                        hints = ", ".join(
                            f"'{item['id']}'({item['subject']})" for item in _nudge_ready[:5]
                        )
                        nudge_request = {
                            "role": "user",
                            "content": PLAN_CONTINUE_NUDGE_TEXT.format(hints=hints),
                        }
                        messages.append(nudge_request)
                        pending_persistence.append(("user", nudge_request))
                        _notify_event(event_callback, "notice", {
                            "message": "计划仍有未执行项，已提醒模型继续执行。",
                        })
                        flush_persistence()
                        progress_guard.observe(
                            _step_progress_observation(None, session_id=parent_session)
                        )
                        if current_step is not None and turn + 1 < max_turns:
                            current_step.complete()
                        continue
                try:
                    from core.hooks.lifecycle import (
                        evaluate_stop,
                        get_default_hook_runner,
                        get_stop_continue_state,
                    )

                    _stop_decision = evaluate_stop(
                        get_stop_continue_state(),
                        get_default_hook_runner(),
                        session_id=parent_session,
                        turn_id=parent_turn_id,
                        response_preview=str(public_content or "")[:200],
                        tool_call_count=turn_tool_call_count,
                    )
                except Exception:
                    _stop_decision = None
                if (
                    _stop_decision is not None
                    and _stop_decision.get("continue")
                    and turn + 1 < max_turns
                ):
                    for _stop_msg in _stop_decision.get("messages") or ():
                        messages.append(_stop_msg)
                        pending_persistence.append(("user", _stop_msg))
                    flush_persistence()
                    progress_guard.observe(
                        _step_progress_observation(None, session_id=parent_session)
                    )
                    if current_step is not None and turn + 1 < max_turns:
                        current_step.complete()
                    continue
                if (
                    _stop_decision is not None
                    and _stop_decision.get("continue")
                    and turn + 1 >= max_turns
                ):
                    _notify_event(event_callback, "notice", {
                        "message": (
                            "Stop hook 请求继续执行，但已达本回合步数上限，"
                            "未能续跑（如需继续请开启新回合）。"
                        ),
                    })
                if _stop_decision is not None and _stop_decision.get("limit_hit"):
                    _notify_event(event_callback, "notice", {
                        "message": str(_stop_decision.get("warning") or ""),
                    })
                # 模型未调用工具且未生成任何正文：给出明确提示，避免 UI 在
                # "思考完成" 后陷入沉默（常见于某些模型只输出 reasoning 或空回复）。
                # 判断用促升前的原始正文（raw_content_meaningful）：促升兜底本身
                # 就说明模型没产出正式回复。重试耗尽后必须让用户明确感知回合未
                # 正常完成：发 error 事件（GUI 会写进对话流/状态栏），而不是仅靠
                # 静默 notice。
                if not raw_content_meaningful:
                    fallback_msg = "模型本轮未返回有效正文内容（可能只输出了思考过程或空回复），任务未正常完成。"
                    if reasoning_content and reasoning_content.strip():
                        fallback_msg += " 如需查看思考过程，可尝试换用支持推理展示的模型或重新提问。"
                    if final_summary_retry_count > 0:
                        _notify_event(event_callback, "error", {"message": fallback_msg})
                    else:
                        _notify_event(event_callback, "notice", {"message": fallback_msg})
                if promoted_reasoning and public_content:
                    yield from _yield_content(public_content)
                flush_persistence()
                if current_step is not None:
                    current_step.complete()
                if harness_runtime is not None:
                    harness_runtime.complete()
                computer_exit_kind = "completed"
                return

            if current_step is not None:
                current_step.mark_tools_running()

            # 输出被 max_tokens 截断时，写操作或截断的尾部参数绝不能执行；
            # 但对前面已经完整序列化且严格 read_only 的调用，先记为 safe_readonly_calls，
            # 复用下方同一份 batch_executor 安全执行完只读调用后合并截断错误。
            length_partial_calls = False
            safe_readonly_calls = []
            if finish_reason == "length":
                truncated_err = (
                    f"{TOOL_ERROR_PREFIX} 本次工具调用参数因输出长度限制被截断，未执行。"
                    "请把参数拆小（例如分多次 edit_file、缩短 content）后重新调用。"
                )
                last_tc_idx = len(tool_calls_list) - 1
                active_reg = tool_registry
                for tc_idx, tc in enumerate(tool_calls_list):
                    if tc_idx == last_tc_idx and len(tool_calls_list) > 1:
                        # 最后一个调用通常是被 max_tokens 截断的位置，跳过以免参数残缺
                        continue
                    fn = tc.get("function") or {}
                    t_name = str(fn.get("name") or "")
                    t_raw = str(fn.get("arguments") or "")
                    try:
                        parsed_args = json.loads(t_raw) if t_raw.strip() else {}
                    except (TypeError, ValueError):
                        parsed_args = None
                    try:
                        spec = active_reg.get(t_name)
                        is_ro = bool(getattr(spec, "read_only", False))
                    except KeyError:
                        is_ro = False
                    if is_ro and isinstance(parsed_args, dict):
                        safe_readonly_calls.append(tc)

                if not safe_readonly_calls:
                    for tc in tool_calls_list:
                        synth = {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": tc["function"]["name"],
                            "content": truncated_err,
                        }
                        messages.append(synth)
                        if stream:
                            pending_persistence.append(("tool", synth))
                    flush_persistence()
                    if current_step is not None:
                        current_step.mark_tools_committed()
                        if turn + 1 < max_turns:
                            current_step.complete()
                    _notify_event(event_callback, "notice", {
                        "message": "模型输出被长度限制截断，已拦截本批工具调用并要求模型拆小参数重试。",
                    })
                    progress_guard.observe(
                        _step_progress_observation(None, session_id=parent_session)
                    )
                    continue
                length_partial_calls = True

            tool_activity_seen = True
            turn_tool_call_count += len(tool_calls_list)
            step_used_write_tools, step_used_command_tools = _tool_call_activity_kinds(
                tool_calls_list
            )
            used_write_tools = used_write_tools or step_used_write_tools
            used_command_tools = used_command_tools or step_used_command_tools
            if event_callback is None:
                app_context.set_task_status('executing')
                app_context.invalidate_ui()
            else:
                _notify_event(event_callback, "status", {"state": "executing"})

            computer_batch_error = None
            try:
                validate_computer_tool_batch(tool_calls_list)
            except ComputerControlError as batch_error:
                computer_batch_error = batch_error
            rejected_computer_calls = {}
            if computer_batch_error is not None:
                rejected_computer_calls = {
                    tool_call["id"]: tool_call
                    for tool_call in tool_calls_list
                    if (tool_call.get("function") or {}).get("name")
                    == COMPUTER_ACTION_TOOL_NAME
                }
            if (
                computer_lease is None
                and allow_subagents
                and computer_capability is not None
                and computer_capability.enabled
                and computer_capability.protocol == ComputerProtocol.GENERIC.value
                and not rejected_computer_calls
                and any(
                    (tool_call.get("function") or {}).get("name")
                    == COMPUTER_ACTION_TOOL_NAME
                    for tool_call in tool_calls_list
                )
            ):
                computer_controller = get_computer_control_service()
                computer_owner = ComputerOwner(
                    session_id=str(parent_session or ""),
                    turn_id=str(parent_turn_id or ""),
                    session_label=str(parent_session or parent_turn_id or ""),
                    cancel_event=(
                        cancel_event
                        if cancel_event is not None
                        else threading.Event()
                    ),
                )
                try:
                    computer_run_id = str(
                        computer_controller.start_run(
                            computer_owner, computer_capability
                        ).run_id
                    )
                    lease_capability = computer_capability
                except ComputerControlError as start_error:
                    computer_run_id = ""
                    lease_capability = ComputerCapability(
                        enabled=False,
                        protocol=ComputerProtocol.NONE.value,
                        targets=(),
                        browser="",
                        settings_generation=computer_capability.settings_generation,
                        error_code=start_error.code,
                        reason=start_error.message,
                    )
                computer_lease = ComputerTurnLease(
                    lease_capability,
                    computer_owner,
                    computer_controller,
                    computer_run_id,
                )

            from core.harness.tool_pipeline import ToolBatchExecutor

            def _harness_journal_sink(kind, facts):
                """把工具事件的脱敏事实写进 harness journal（断点续跑依据）。

                只落哈希与元数据；任何异常都吞掉，绝不影响工具执行。
                """
                try:
                    journal = getattr(
                        getattr(current_step, "turn", None), "journal", None
                    )
                    if journal is None:
                        return
                    from core.harness.models import (
                        HarnessEventKind,
                        JournalEventDraft,
                    )

                    if kind == "tool_started":
                        event_kind = HarnessEventKind.TOOL_STARTED
                    elif kind == "tool_finished":
                        event_kind = HarnessEventKind.TOOL_FINISHED
                    else:
                        return
                    journal.append_event(
                        current_step.turn.turn_id,
                        JournalEventDraft(
                            kind=event_kind,
                            step_id=getattr(current_step, "step_id", None),
                            tool_name=facts.get("tool_name"),
                            call_ordinal=facts.get("call_ordinal"),
                            arguments_sha256=facts.get("arguments_sha256"),
                            result_sha256=facts.get("result_sha256"),
                            duration_ms=facts.get("duration_ms"),
                            result_status=facts.get("result_status"),
                        ),
                    )
                except Exception:
                    logger.warning(
                        "harness journal sink failed for %s", kind, exc_info=True
                    )

            tool_pipeline, tool_context = _build_tool_pipeline_and_context(
                event_callback,
                parent_session=parent_session,
                parent_turn_id=parent_turn_id,
                cancel_event=cancel_event,
                allow_subagents=allow_subagents,
                permission_owner=permission_owner,
                permission_source=permission_source,
                computer_lease=computer_lease,
                computer_native_selection=native_selection,
                computer_capability=computer_capability,
                journal_event_sink=(
                    _harness_journal_sink if current_step is not None else None
                ),
                harness_step=current_step,
            )
            tool_calls_by_id = {
                tool_call["id"]: tool_call for tool_call in tool_calls_list
            }

            def execute_decoded(decoded, context):
                if decoded.call_id in rejected_computer_calls:
                    return computer_error_tool_message(
                        rejected_computer_calls[decoded.call_id],
                        computer_batch_error,
                    )
                if _execute_single_tool_call is not _ORIGINAL_EXECUTE_SINGLE_TOOL_CALL:
                    return _execute_single_tool_call(
                        tool_calls_by_id[decoded.call_id],
                        event_callback=event_callback,
                        parent_session=parent_session,
                        parent_turn_id=parent_turn_id,
                        cancel_event=cancel_event,
                        allow_subagents=allow_subagents,
                        permission_owner=permission_owner,
                        permission_source=permission_source,
                    )
                return tool_pipeline.execute_decoded(decoded, context)

            batch_executor = ToolBatchExecutor(
                tool_pipeline,
                repeat_guard=repeat_call_guard,
                execute_decoded=execute_decoded,
                event_payload=_tool_event_payload,
                finished_event_payload=_tool_finished_event_payload,
                result_ok=_tool_result_ok,
            )
            records = []
            notify_tool_event = (
                lambda kind, payload: _notify_event(event_callback, kind, payload)
            )
            _calls_for_batch = safe_readonly_calls if length_partial_calls else tool_calls_list
            # 续跑打点：记录本轮计划执行的工具调用总数。生产路径不走
            # ToolBatchExecutor.execute()，必须在这里补一次，否则该列始终为空。
            if current_step is not None:
                try:
                    current_step.mark_tool_call_count(len(_calls_for_batch))
                except Exception:
                    logger.warning("mark tool call count failed", exc_info=True)
            for prepared_batch in batch_executor.prepare(
                _calls_for_batch,
                tool_context,
            ):
                batch_executor.begin_batch(
                    prepared_batch,
                    tool_context,
                    notify_tool_event,
                )
                yield ""
                records.extend(batch_executor.execute_batch(
                    prepared_batch,
                    tool_context,
                    notify_tool_event,
                ))
                yield ""
            records = tuple(sorted(
                records,
                key=lambda record: record.call_ordinal,
            ))

            if length_partial_calls:
                _executed_by_id = {
                    rec.message.get("tool_call_id"): rec.message
                    for rec in records
                    if rec.message.get("tool_call_id")
                }
                for tc in tool_calls_list:
                    msg_item = _executed_by_id.get(tc["id"]) or {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "content": truncated_err,
                    }
                    messages.append(msg_item)
                    if stream:
                        pending_persistence.append(("tool", msg_item))
                followups = [
                    msg for rec in records for msg in rec.followup_messages
                ]
                if followups:
                    messages.extend(followups)
                    if stream:
                        pending_persistence.extend(("user", fu) for fu in followups)
                flush_persistence()
                if current_step is not None:
                    current_step.mark_tools_committed()
                    if turn + 1 < max_turns:
                        current_step.complete()
                _notify_event(event_callback, "notice", {
                    "message": (
                        f"模型输出被长度限制截断：已执行 {len(_executed_by_id)} 个完整只读调用，"
                        "其余截断或写入调用已拦截重试。"
                    ),
                })
                tool_activity_seen = True
                continue

            substantive_op_count += _count_substantive_ops(tool_calls_list)

            # 已成功完成（status == "completed"）的 test/operate 子代理等同真实命令
            # 验证证据，避免主代理把测试或取证派发出去后仍触发验证声明误报。
            if not used_command_tools:
                _tc_by_id = {
                    tc.get("id"): tc for tc in tool_calls_list if isinstance(tc, dict) and tc.get("id")
                }
                for _rec in records:
                    _msg = _rec.message
                    if _msg.get("name") != "run_subagent" or not _tool_result_ok("run_subagent", _msg):
                        continue
                    _tc = _tc_by_id.get(_msg.get("tool_call_id")) or {}
                    try:
                        _sub_args = json.loads((_tc.get("function") or {}).get("arguments") or "{}")
                    except (TypeError, ValueError):
                        _sub_args = {}
                    _sub_type = str((_sub_args or {}).get("subagent_type") or "").strip().lower()
                    _sub_content = str(_msg.get("content") or "")
                    _is_foreground_completed = (
                        _sub_type in _SUBAGENT_COMMAND_EVIDENCE_TYPES
                        and not bool((_sub_args or {}).get("run_in_background"))
                        and not _sub_content.lstrip().startswith("{")
                    )
                    if _is_foreground_completed or _subagent_counts_as_command_evidence(
                        _sub_type, _sub_content
                    ):
                        used_command_tools = True
                        break

            results = [record.message for record in records]
            followup_messages = [
                message
                for record in records
                for message in record.followup_messages
            ]
            messages.extend(results)
            pending_tool_records = {
                record.message.get("tool_call_id"): record for record in records
            }
            if followup_messages:
                messages.extend(followup_messages)
            if stream:
                pending_persistence.extend(("tool", result) for result in results)
                if followup_messages:
                    pending_persistence.extend(
                        ("user", followup) for followup in followup_messages
                    )

            orchestration_injected_this_step = False
            if _should_nudge_orchestration(
                allow_subagents=allow_orchestration,
                count=substantive_op_count,
                session_id=parent_session,
                turn_id=parent_turn_id,
                already_sent=orchestration_nudge_sent,
            ):
                orchestration_nudge_sent = True
                orchestration_injected_this_step = True
                _register_orchestration_nudge(parent_turn_id)
                orchestration_request = {
                    "role": "user",
                    "content": _ORCHESTRATION_NUDGE_TEXT,
                }
                messages.append(orchestration_request)
                pending_persistence.append(("user", orchestration_request))
                # 静默注入：不发 notice 事件（UI 不弹提示卡片），只留 debug 日志
                logger.debug("已静默注入编排提醒（session=%s turn=%s）", parent_session, parent_turn_id)
                flush_persistence()
                progress_guard.observe(
                    _step_progress_observation(None, session_id=parent_session)
                )

            from core.harness.tool_pipeline import REPEAT_CALL_BLOCKED_TEXT

            step_repeat_blocked = any(
                REPEAT_CALL_BLOCKED_TEXT in str(record.message.get("content", "") or "")
                for record in records
            )
            step_failed_samples = [
                f"{record.message.get('name', 'tool')}: {str(record.message.get('content', '') or '')[:100]}"
                for record in records
                if not _tool_result_ok(record.message.get("name", ""), record.message)
            ]
            if records and len(step_failed_samples) == len(records):
                consecutive_error_steps += 1
            elif step_repeat_blocked:
                consecutive_error_steps += 1
            else:
                consecutive_error_steps = 0

            if (
                not orchestration_injected_this_step
                and turn + 1 < max_turns
                and loop_guard_count < 2
                and (turn - last_loop_guard_turn) >= 2
                and (
                    consecutive_error_steps >= 2
                    or step_repeat_blocked
                    or progress_guard.stalled_steps >= max(2, progress_guard.MAX_STALLED_STEPS - 2)
                )
            ):
                try:
                    from core.task_decision_hooks import (
                        build_loop_intervention_message,
                        decide_loop_intervention,
                    )

                    loop_decision = decide_loop_intervention(
                        turn_index=turn,
                        max_turns=max_turns,
                        consecutive_error_steps=consecutive_error_steps,
                        stalled_steps=progress_guard.stalled_steps,
                        repeat_blocked=step_repeat_blocked,
                        error_samples=step_failed_samples,
                    )
                    guidance_text = build_loop_intervention_message(loop_decision)
                except Exception:
                    guidance_text = None
                if guidance_text:
                    last_loop_guard_turn = turn
                    loop_guard_count += 1
                    guidance_msg = {"role": "user", "content": guidance_text}
                    messages.append(guidance_msg)
                    pending_persistence.append(("user", guidance_msg))
                    logger.info(
                        "loop_guard 采纳决策 [%s]（session=%s turn=%s）",
                        loop_decision,
                        parent_session,
                        turn + 1,
                    )

            if event_callback is None:
                app_context.set_task_status('executing')
                app_context.invalidate_ui()
            else:
                _notify_event(event_callback, "status", {"state": "executing"})

            flush_persistence()
            if current_step is not None:
                current_step.mark_tools_committed()
                if turn + 1 < max_turns:
                    current_step.complete()

            # 工具提交完成后的进展观察：后台任务指纹/计划快照/非观察类结果哈希/
            # 修改类提交标记任一变化即算真实进展，否则停滞计数 +1
            progress_guard.observe(
                _step_progress_observation(
                    records, tool_calls_by_id, session_id=parent_session
                )
            )

        # 达到轮次上限但模型尚未正常文本收尾：提示消息写入历史并在当前 Step 上
        # 原子提交，最后一个已完整提交的 Step 保持 completed，Turn 显式失败为
        # StepLimitExceeded——不回滚副作用、不自动续跑、不发 turn_finished
        msg = (
            f"{TOOL_ERROR_PREFIX} 单任务已达到模型轮次上限（{max_turns} 轮），任务未完成。"
            "已保留本轮代码、工具结果和测试记录；请发送“继续”开启新回合，"
            "或在系统设置中调整上限。"
        )
        limit_message = {"role": "assistant", "content": msg}
        messages.append(limit_message)
        if stream:
            persisted_limit_message = deepcopy(limit_message)
            persisted_limit_message.update(_model_display_snapshot())
            pending_persistence.append(("assistant", persisted_limit_message))
        flush_persistence()
        if current_step is not None:
            current_step.complete()
        if harness_runtime is not None:
            harness_runtime.fail("StepLimitExceeded")
        _notify_event(event_callback, "error", {"message": msg})
        # Harness 已进入 failed 终态，事件层必须同步发出
        # turn_error；否则外层会把正常 return 误判为 turn_finished。
        _notify_event(event_callback, "turn_error", {"message": msg})
        # 同无进展熔断：只走事件通道展示，不 yield 正文避免对话流重复渲染。
        return

    except KeyboardInterrupt:
        computer_exit_kind = "cancelled"
        if cancel_event is not None:
            cancel_event.set()
            abort_inflight_streams(cancel_event)
        # 关闭底层 HTTP 流：否则每次取消都会泄漏一个连接/套接字
        try:
            response_stream.close()
        except Exception:
            pass
        # 用户中断：先把本轮已产生的完整消息打捞落库（悬空的 tool_calls 组补
        # 取消占位结果），否则终止前已完成的助手输出/工具结果整批丢失，
        # 重开会话看不到任何终止前进度。
        try:
            salvage_persistence_on_cancel()
            flush_persistence()
        except Exception as salvage_error:
            if harness_runtime is not None:
                harness_runtime.fail(type(salvage_error).__name__)
            raise
        finally:
            # 安全回退删除此轮 Turn 添加的内存消息残影（DB 已保留打捞部分）
            rollback_turn_messages()
        if harness_runtime is not None:
            # 取消收尾 best-effort：cancel() 内 journal CAS 失败若抛出会顶替
            # 原始 KeyboardInterrupt，外层把"用户取消"渲染成 FAILED；残留的
            # 非终态 Turn 由崩溃恢复路径标 interrupted 收敛。
            try:
                harness_runtime.cancel()
            except Exception:
                logger.warning("用户取消的 harness 收尾失败（交由恢复路径收敛）", exc_info=True)
        raise
    except GeneratorExit:
        # GUI 消费端检测到取消后 break，生成器在 yield 点被 close：这条路径
        # 会绕过上面的 KeyboardInterrupt 分支，必须同样打捞落库再退出。
        computer_exit_kind = "cancelled"
        try:
            response_stream.close()
        except Exception:
            pass
        if cancel_event is not None:
            cancel_event.set()
            abort_inflight_streams(cancel_event)
        try:
            salvage_persistence_on_cancel()
            flush_persistence()
        except Exception as salvage_error:
            if harness_runtime is not None:
                harness_runtime.fail(type(salvage_error).__name__)
            raise
        if harness_runtime is not None:
            try:
                harness_runtime.cancel()
            except Exception:
                logger.warning("生成器取消的 harness 收尾失败（交由恢复路径收敛）", exc_info=True)
        raise
    except Exception as e:
        computer_exit_kind = "failed"
        try:
            response_stream.close()
        except Exception:
            pass
        # A1-02：连接中断等通用异常同样先把本 step 已生成的部分正文与待写
        # 队列打捞落库（带「[本轮流式中断]」标注）——否则用户所见正文重开
        # 会话即消失（DB 与所见分叉）。打捞为 best-effort：失败不掩盖原始
        # 异常的报告流程，仅记录 harness 失败原因。
        try:
            salvage_streaming_partial()
            salvage_persistence_on_cancel()
            flush_persistence()
        except Exception as salvage_error:
            if harness_runtime is not None:
                harness_runtime.fail(type(salvage_error).__name__)
        # 异常路径同样回滚本轮未持久化的内存消息：既避免留下带 tool_calls 却无
        # tool 响应的悬空 assistant 消息（下一轮 API 会 400），也保持内存历史与 DB 一致
        rollback_turn_messages()
        if harness_runtime is not None:
            harness_runtime.fail(type(e).__name__)
        if e is persistence_failure:
            raise
        if event_callback is not None:
            # 这条兜底只发事件、不 raise，上层不会再有 logger.exception，等于异常来源
            # 彻底丢失（WinError 10035 这类瞬时报错就是这样查不到是谁抛的）。此处落一条
            # 带堆栈的 warning：堆栈只含调用链与异常文本，不含请求头或凭证。
            logger.warning(
                "回合异常兜底（%s）：%s",
                type(e).__name__,
                _sanitize_error_text(str(e)),
                exc_info=True,
            )
            user_facing_msg = _format_user_facing_error(e)
            _notify_event(event_callback, "error", {
                "message": user_facing_msg,
            })
            _notify_event(event_callback, "turn_error", {
                "message": user_facing_msg,
            })
        else:
            from prompt_toolkit.formatted_text import HTML

            safe_print(HTML(f"<ansiyellow>{_current_agent_display_name()}暂未完成当前步骤</ansiyellow>"))
        return
    finally:
        # 注入了 system_prompt_extra 时，messages 是本次请求专用的新列表：循环里
        # 追加的 assistant/tool 消息仍要按原样回写到调用方共享的原始列表，保持既有
        # 语义不变（例如非流式 chat_with_model 依赖 messages[-1] 读取最终回复）。
        # 只回写"新出现的"元素，注入的 system 消息本身绝不写回调用方列表。
        if original_messages is not None:
            original_messages.extend(
                item
                for item in messages
                if item is not extra_injected_message
                and id(item) not in original_message_ids
            )
        # 电脑操控 run 收尾（Task 10，设计 14.2）：任何 Turn 终态二选一调用
        # complete_run/stop，不留 running 租约；无 run 时是幂等 no-op。
        _finalize_computer_run()


def __getattr__(name):
    if name == "_token_usage_listener":
        from core import llm_diagnostics
        return getattr(llm_diagnostics, "_token_usage_listener")
    if name == "TOOLS_SCHEMA":
        return get_tools_schema()
    if name == "mcp_manager":
        return _get_mcp_manager()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
