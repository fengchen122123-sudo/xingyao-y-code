import difflib
import json
import hashlib
import logging
import queue
import random
import re
import subprocess
import threading
import time
import uuid
from collections import deque
# Python 3.10 及以下 concurrent.futures.TimeoutError 不是内建 TimeoutError 的别名，
# 权限等待轮询必须同时捕获两者，否则等待期直接崩溃
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

# 电脑操控（Task 10，设计 9.3/19）：包级轻量依赖，绝不触碰键鼠/浏览器；
# controller 顶层也不 import core.llm，无循环导入。测试通过 monkeypatch
# llm.resolve_computer_capability / llm.get_computer_control_service 注入 fake。
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
from core.computer.settings import load_computer_settings
from core.computer.tool_adapter import (
    COMPUTER_ACTION_TOOL_NAME,
    RESERVED_COMPUTER_TOOL_NAMES,
    ComputerTurnLease,
)

# run 已终态的快照状态集合：Turn 收尾时据此跳过重复 complete/stop（幂等）。
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

# --- 上游错误分类（修复③：额度/鉴权类确定性失败 fail-fast 复用）---
from core.llm_error_mapper import map_llm_error

# 确定性失败类别：重试无意义（额度耗尽 / 鉴权失败 / 权限受限 / 模型不存在），
# 命中即 fail-fast 直接抛出，走既有失败收尾；瞬时类（rate_limit 等）保持重试。
_DETERMINISTIC_API_FAILURE_CATEGORIES = frozenset({
    "insufficient_quota",
    "auth_invalid",
    "permission_denied",
    "model_not_found",
})


def _is_deterministic_api_failure(exc):
    """判断异常是否属于"重试无意义"的确定性上游失败（纯函数，便于单测）。

    整体用 try/except 兜底：分类失败时返回 False（保守），不改变既有重试行为。
    """
    try:
        return map_llm_error(exc).category in _DETERMINISTIC_API_FAILURE_CATEGORIES
    except Exception:
        return False

# --- Re-exported from core.permission_terminal ---
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
    """Keep the historical permission injection point while returning rich decisions.

    Tests and embedders may replace ``check_permission_and_ask``. Their boolean result
    remains valid because ToolPipeline accepts both booleans and AuthorizationResult.
    """
    return check_permission_and_ask(
        function_name,
        arguments,
        structured=True,
        **context,
    )

# --- Re-exported from core.llm_diagnostics ---
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
    get_token_usage_snapshot,
    rename_cache_usage_session,
    session_token_usage,
    set_token_usage_listener,
    token_usage_lock,
)

# The registry is the canonical capability metadata source.
from core.tool_registry import build_default_registry, filter_reserved_tool_schemas
tool_registry = build_default_registry()


_mcp_first_wait_done = False


def _get_mcp_manager():
    """惰性获取 mcp_manager 单例：import core.mcp_client 会拉起 mcp 包全家桶
    （pydantic/httpx/anyio，冷启动约 1.1s），故延迟到首次实际使用时才加载。
    每次调用时读取属性，不做缓存，保证测试 monkeypatch core.mcp_client.mcp_manager 生效。"""
    from core.mcp_client import mcp_manager
    return mcp_manager


def _snapshot_tool_breakdown(request_tools):
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
        # 空组写 0（而非 '[]' 的 2），前端过滤 chars<=0 不会渲染 0 占比行
        _update_ctx_source(
            "system_tools",
            len(json.dumps(system_tools, ensure_ascii=False)) if system_tools else 0,
        )
        _update_ctx_source(
            "mcp_tools",
            len(json.dumps(mcp_tools, ensure_ascii=False)) if mcp_tools else 0,
        )
    except Exception:
        logger.debug("工具定义字符量统计失败（旁路，已忽略）", exc_info=True)


def get_tools_schema():
    """每次构建请求时新鲜获取工具 schema（registry 静态 + MCP 动态）。

    不做模块级一次性快照：启动时 MCP 连接失败不会导致本进程永久缺失
    MCP 工具 schema，且 mcp_client 为惰性连接，import 时不触发网络。

    进程内首次调用时同步等待 MCP 连接就绪（有界超时，空配置立即返回）：
    否则 MCP schema 会在第 N 轮突然出现在 tools 列表里，DeepSeek/Kimi
    前缀缓存从头部整体失效。后续调用恢复非阻塞，避免每轮都承担连接延迟。
    """
    global _mcp_first_wait_done
    if not _mcp_first_wait_done:
        _mcp_first_wait_done = True
        try:
            _get_mcp_manager()._ensure_connected()
        except Exception:
            pass
    # MCP schema 合并时过滤电脑操控保留名（设计 19.2）：
    # computer_action / computer_select_target 由固定 Harness 适配器提供，
    # MCP 不得占用同名工具；无保留名时 tools 字节与过滤前完全一致。
    return tool_registry.api_schemas() + filter_reserved_tool_schemas(
        _get_mcp_manager().get_schemas()
    )


def completion(**kwargs):
    """模型请求入口（自研 HTTPX 引擎唯一路径，可被测试 mocker.patch 拦截）。

    实际传输由 core.llm_transport._dispatch_completion 分派到
    core.llm_native_transport.native_completion。
    """
    return _dispatch_completion(**kwargs)


# --- Re-exported from core.harness.responses_adapter ---
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
    """按显式协议发起流请求；Responses 失败时禁止降级。

    返回 (stream, holder)：holder 登记在 cancel_event 名下的在飞注册表，
    供停止路径硬中断；由调用方在流消费结束后 unregister。
    """
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
        # 可关闭对象是底层原始流，不是外层映射生成器（生成器在执行中无法 close）
        raw_stream = _dispatch_responses(**prepared.kwargs)
        if holder is not None:
            holder.attach(raw_stream)
        return _responses_stream_as_chat_chunks(raw_stream), holder
    except Exception:
        # 请求未建立：登记无意义，立即摘除避免空句柄滞留注册表
        _unregister_inflight_stream(cancel_event, holder)
        raise


# estimate_tokens 结果缓存：token 估算整个历史开销可观（长会话消息多），
# 而 UI 状态栏、压缩判断等会在内容未变时反复调用。用 (模型, 消息数, 总字符数)
# 作为签名，未变化时直接复用上次结果。
_token_estimate_cache = {"sig": None, "value": 0}
# 前缀增量缓存：append-only 历史每轮只新增尾部消息，
# 记录前缀（消息数/字符总长/token 数），下一轮免全量 tokenize
_token_prefix_cache = {"model": None, "msg_count": 0, "char_len": 0, "value": 0}
_token_cache_lock = threading.RLock()


def _sanitize_error_text(text):
    """异常文本入 UI / 落库前脱敏。

    网关 / Provider 异常文本常携带完整请求 URL 与请求上下文（query 里的 api_key、
    Authorization: Bearer 头、URL userinfo 等），直接发 error/turn_error 事件会把
    凭据泄露到界面与会话记录。这里对 URL 凭据段与常见密钥模式做不可逆脱敏。
    """
    text = str(text or "")
    # URL userinfo：scheme://user:password@host → scheme://user:***@host
    text = re.sub(
        r"([a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)", r"\1***\2", text,
        flags=re.IGNORECASE,
    )
    # key=value / key: value 形式的敏感字段（含 query string 里的 api_key=...）
    text = re.sub(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|token|secret|password|passwd|authorization)\s*[=:]\s*)[^\s,;&\"']+",
        r"\1***",
        text,
    )
    # Authorization: Bearer xxx
    text = re.sub(r"(?i)\bBearer\s+[^\s\"',;]+", "Bearer ***", text)
    # 常见 provider 密钥前缀
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


def _fold_old_media_for_estimate(messages):
    """估算前把历史图片折叠为占位符（与请求时 _strip_media_and_merge 的规则一致）。

    base64 原图动辄几十万字符，按字符数估算会虚高几个数量级，导致携带大图片的
    会话反复误触发记忆压缩。仅最近一条带图用户消息保留原样（贴近真实请求形状），
    更早的一律折叠为短占位符。
    """
    last_media_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list) and any(
            isinstance(item, dict) and item.get("type") in ("image", "image_url")
            for item in content
        ):
            last_media_idx = idx
        break
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
    # 两份缓存包含互相关联的字段，必须在一个临界区内读取、计算并提交，
    # 否则并发模型会形成“模型 A 的签名 + 模型 B 的值”的混合快照。
    with _token_cache_lock:
        return _estimate_tokens_locked(model_name, messages)


def _estimate_tokens_locked(model_name, messages):
    try:
        # 签名必须含内容哈希：仅用 (模型, 消息数, 总字符数) 时，等长内容突变
        # （改写历史但长度不变）会命中旧缓存，返回过期的 token 估算
        parts = [str(m.get("content") or "") for m in messages]
        total_len = sum(len(p) for p in parts)
        sig = (str(model_name), len(messages), total_len, hash(tuple(parts)))
    except Exception:
        sig = None
    if sig is not None and _token_estimate_cache.get("sig") == sig:
        return _token_estimate_cache["value"]

    def _count(msgs):
        try:
            # 历史图片先折叠再计数：base64 字符量与图片真实 token 消耗无关
            return _token_counter(model_name, _fold_old_media_for_estimate(msgs))
        except Exception:
            # 如果模型不支持或报错，采用约 2 字符 / Token 的经验值进行兜底估算
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
            # 前缀未变：只对新追加的尾部消息计数，加上已缓存的前缀计数
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
    """假流式生成器：把完整文本逐字符 yield，模拟打字机效果"""
    for char in text:
        yield char


# 填充词词表（修复①「填充词判据收紧」）。取舍是“宁可漏判、不可误判”：
# 漏掉一个填充词只会少熔断一次，误判正常短回复则会触发下游护栏误熔断。
# 首版照方案给定元素，唯一例外是不收录“完成”——它是常见的合法最终短回复
# （仓库内多处用例即以 "完成" 作为模型收尾正文，属方案自身边界所言的
# “正常短回复不得被误判”），收录它会让正常收尾被判为空正文并误触发催促。
_FILLER_TOKENS = frozenset(
    {
        "好", "好的", "嗯", "哦", "噢", "ok", "okay", "行", "可以",
        "收到", "明白", "了解", "继续", "输出", "完毕",
        "yes", "no",
    }
)

# 与 _is_meaningful_assistant_content 同款的“空白/标点/Markdown 符号”噪声字符类，
# 剥离与切分共用同一口径，避免两处正则漂移。
# 相较原判据补入省略号“…”（U+2026），否则“收到……”这类“填充词+省略号”无法被剥离，
# 纯标点语义不变（仅标点仍由原判据判空）。
_ASSISTANT_NOISE_PATTERN = r"[\s`*_#>~\-\[\](){}|.,!?，。！？:：;；/\\…]+"


def _is_filler_only(text):
    """判断文本是否只由填充词构成（修复①判据收紧的辅助函数）。

    来历：原判定只剥离噪声字符，对 ``"好。"`` 剥完仍剩 ``"好"`` 而被判为有效正文，
    使得慢步熔断 / 总结催促 / 收尾报错三道护栏同时短路。此处要求：剥完噪声后，
    按标点切出的每个片段都命中 ``_FILLER_TOKENS``（比对前小写化），且残余长度 ≤6，
    才算“纯填充词”。宁可漏判（放过个别填充词），也不误判（把正常短回复当空正文）。
    """
    raw = text or ""
    stripped = re.sub(_ASSISTANT_NOISE_PATTERN, "", raw)
    # 残余为空属于“空正文”语义，交回原判据处理，本函数不判假（不改变空串行为）。
    if not stripped:
        return False
    # 残余过长，不可能只是短应答式填充词。
    if len(stripped) > 6:
        return False
    fragments = [seg for seg in re.split(_ASSISTANT_NOISE_PATTERN, raw) if seg]
    if not fragments:
        return False
    return all(seg.lower() in _FILLER_TOKENS for seg in fragments)


def _is_meaningful_assistant_content(text):
    """是否含有效正文：原判据为真，且不是“剥完只剩填充词”。

    函数名与签名保持不变（下游调用与回归用例依赖）。
    """
    if not bool(re.sub(_ASSISTANT_NOISE_PATTERN, "", text or "")):
        return False
    return not _is_filler_only(text)


# --- 退化（复读）检测（修复②）：流式装配层的"连续复读"护栏 -------------------
# 与修复①的分工：修复①管"单步只有填充词"，这里管"多步反复同一段短内容"。
# 两者都是为了解决同一类卡死——模型既不调工具也不给结论，只反复刷同样的话，
# 而 progress_guard 只看确定性状态、不读自然语言（有意设计），追不上这种模式。
_DEGENERATE_WINDOW = 3
# "连续相同"判据只对短内容生效：长正文逐位相同更像模型复述上下文，交给相似度判据。
_DEGENERATE_MAX_FINGERPRINT_CHARS = 20
_DEGENERATE_SIMILARITY_THRESHOLD = 0.9
# 退化收敛催促的次数上限：首次命中催促一次，再次命中直接熔断收尾。
_DEGENERATE_MAX_NUDGES = 1


def _degenerate_fingerprint(text):
    """退化指纹：剥离噪声字符（与修复①同一口径）后的字符串。

    复用 ``_ASSISTANT_NOISE_PATTERN``，避免两处正则各写一份造成口径漂移。
    """
    return re.sub(_ASSISTANT_NOISE_PATTERN, "", text or "")


def _fingerprint_similarity(left, right):
    """两个指纹的相似度（0~1）：取“编辑距离比例”与“字符集包含度”的较大者。

    只用标准库（difflib）实现，不引入第三方依赖。包含度是必要补充：中文短复读的
    指纹常只差一两个字（如 "好" vs "好的"），编辑距离比例只有 0.67，会漏判；包含度
    按 ``|A∩B| / min(|A|, |B|)`` 计算，能把“一方基本被另一方覆盖”的近似复读判出来。
    """
    if not left or not right:
        return 0.0
    edit_ratio = difflib.SequenceMatcher(None, left, right).ratio()
    left_set, right_set = set(left), set(right)
    containment = len(left_set & right_set) / min(len(left_set), len(right_set))
    return max(edit_ratio, containment)


def _is_degenerate_repeat(fingerprints):
    """最近若干步指纹是否构成“退化复读”（纯函数，便于单测）。

    判定口径（实施方案 §T2）：滑窗内连续 3 步指纹逐位相同且单步长度 ≤20 字符；
    或滑窗内指纹两两相似度 ≥0.9。空指纹（工具调用步无正文、无思考）不参与判定，
    避免把“连续只调工具”的正常步骤误判为复读。
    """
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
    """
    大模型通信网关：支持流式（stream=True）与非流式（stream=False，后台 Agent 自动消化）
    """
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
        # 非流式模式下，由包装函数在后台默默消耗生成器，完成所有工具调用与轮次迭代
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
    """在主 Agent 首次 Provider 请求前认领该会话最早的接管项。"""
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


# 入口层受控回合来源白名单：只有入口层（GUI/CLI 本地、LAN 手机端、飞书通道）能设置，
# 绝不来自用户消息或模型参数。非 local 的回合一律按「远程」处理（禁用电脑操控、
# 不解析 @{会话名}、不抢桌面当前会话），见 core/computer/capability.py 与
# core/agent_service.py 的 turn_source != "local" 分支。
# 新增来源时必须同时更新 gui_bridge.TURN_SOURCES（有测试断言两者一致）。
COMPUTER_TURN_SOURCES = ("local", "lan", "feishu")


def resolve_turn_computer_capability(
    model_name,
    api_base=None,
    model_id=None,
    turn_source="local",
):
    """Turn 起点组合解析电脑操控能力（Task 10 Step 3，设计 9.3）。

    按请求路由在 config 中查找模型条目，与请求实际使用的 base_url/protocol
    一起交给纯函数 resolve_computer_capability 冻结为不可变快照；同一 Turn
    的每个 Step 复用该快照，不做二次决策。turn_source 只接受入口层受控值
    local|lan|feishu（不得来自用户消息或模型参数）。设置快照读取失败按
    computer_invalid_config 拒绝（fail-closed），绝不中断普通对话。
    """
    from core.model_manager import get_model_config_by_litellm_model, get_model_protocol

    model = get_model_config_by_litellm_model(model_name, api_base)
    provider = {
        "protocol": get_model_protocol(model_name, api_base),
        "base_url": api_base,
    }
    try:
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
):
    """Yield ordered :class:`TurnEvent` snapshots for one model turn.

    The legacy ``chat_with_model`` API remains text-oriented. This adapter is
    the event-oriented entry point used by the CLI and is deliberately kept
    separate while the existing tool loop is migrated incrementally.

    computer_turn_source 是入口层给出的受控回合来源（local|lan|feishu）：LAN/mobile
    与飞书通道发起的 Turn 在 resolve_computer_capability 内固定禁用电脑操控。

    computer_capability（Task 11a F1 收敛）是调用方显式传入的 Turn 能力快照；
    未传时优先取同线程 prepare 阶段（agent_service.build_runtime_context）存入
    的 handoff，仍无才自行解析（兼容旧调用方）。三条路径合起来保证同一生产
    Turn 的 capability 只解析一次；子代理回合（allow_subagents=False）始终为
    None，不解析也不注入电脑操控。
    """
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
    # Duck-type 管理器（未实现 Journal 存储接口的旧测试替身/外部调用方）
    # 不进入 Harness Journal，退回 legacy add_messages 持久化路径；
    # 真实 MemoryManager 始终具备完整接口，照常走 Journal。
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
            # 生成器侧的任何回调事件都发生在已 yield 文本之后：
            # 若 parser 已悄悄越过 </think>（闭合标签本身不产生文本 part），
            # 这里统一补发 thinking_finished，保证每个思考块只收到一组事件
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
        # 只有真实主 Agent 回合允许认领；子 Agent 自身使用 allow_subagents=False，
        # 即使共享 parent_session 也不能窃取主回合的接管租约。认领与生成器构造
        # 都属于 Harness 准备阶段，必须位于异常收口范围内。
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
        # 生成器前置区（记忆注入/通知 drain/请求构造）抛出的异常会穿透内部
        # try 直接传播到这里：补发 turn_error，保证调用方能感知回合失败
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

# --- Re-exported from core.llm_dialects ---
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
    """Extract only complete provider blocks that are safe to round-trip."""
    return complete_thinking_blocks(delta)

# _notify_event 唯一实现位于 core/permission_terminal.py，经上方 re-export
# 进入本命名空间，此处不得保留同名副本。


def _stream_chunks_with_idle_notice(response_stream, event_callback, raise_if_cancelled, inflight_holder=None, cancel_event=None):
    """在守护线程中消费 LLM 流，主循环用带超时的队列读取。

    直接 for chunk in response_stream 时，首个 chunk 之后流中段 stalled 没有任何
    反馈，最长静默一整个请求超时（随载荷放大，封顶数百秒），用户无法区分
    “慢”与“卡死”。改为后台线程 pump + 主线程带 0.5s 超时的队列读取：
    空闲超阈值后按间隔上报等待状态，同时保持对取消事件的响应能力。
    注意：调用方提前放弃（取消/异常）时 pump 线程会继续读到流结束，线程为
    daemon；native 流 read 超时为 None
    （读超时由本看门狗全权管控），因此 give-up 路径必须先经 inflight_holder
    显式关流再抛出，否则 pump 会永久阻塞在 socket 读且 abort 再也够不到它。
    inflight_holder：本流在在飞注册表中的句柄，消费结束（无论正常/异常/取消）
    统一摘除；用户取消时流已被 abort_inflight_streams 关闭，pump 抛出的读错误
    需经 raise_if_cancelled 归一为用户取消，避免误入网络错误分支。
    """
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
                    # 连续空闲硬上限：连接已死透，继续等只会拖满整轮。
                    # 先经注册表句柄关掉底层流再抛出：native 流无读超时，
                    # 不关流会让 pump 线程永久阻塞且泄漏连接池名额。
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
                        # None 哨兵：消费侧转成空心跳 yield，实时冲刷积压的 status 事件
                        yield None
                continue
            idle_started_at = time.monotonic()
            last_notice_at = idle_started_at
            if not ok:
                if item is _END:
                    return
                # 用户取消触发的硬中断会把底层流关掉，pump 随之抛读错误；
                # 先走取消判定，保证归一为 KeyboardInterrupt 打捞路径而非网络错误
                raise_if_cancelled()
                raise item
            yield item
    finally:
        _unregister_inflight_stream(cancel_event, inflight_holder)


# --- Re-exported from core.llm_execution ---
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
    _canonical_arguments_signature,
    _claims_verification_without_evidence,
    _describe_images_with_secondary,
    _escape_control_chars_in_json_strings,
    _execute_single_tool_call,
    _get_imom_active_publish_root,
    _persisted_tool_calls,
    _raw_tool_call_arguments,
    _read_file_result_limit,
    _read_text_with_encoding_fallback,
    _refill_messages_after_compression,
    _repair_truncated_json,
    _sandbox_outside_guard,
    _show_file_diff,
    _step_progress_observation,
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

# ── 编排提醒：多步未建计划时的软提醒（每回合最多 1 次）────────────────

# 阈值口径：计数对象是“实质操作数”而非模型拆解出的子任务条数——前者由
# _count_substantive_ops 统计（write_file / edit_file / 非只读 execute_command
# 的调用数），与后者是近似映射（一步实质操作 ≈ 推进一条子任务），仅作运行时兜底
# 估计。阈值语义是“超过 3”（即 ≥ 4）才提醒，恰好 3 步及以内不提醒，与提醒文案及
# system_prompt.md / system_prompt_gui.md 的“子任务超过 3 条（即 ≥ 4 条）必须先用
# plan_write、3 条及以内直接执行”保持一致。
_ORCHESTRATION_NUDGE_THRESHOLD = 3
_ORCHESTRATION_COUNTED_TOOLS = frozenset({"write_file", "edit_file"})
_ORCHESTRATION_COMMAND_TOOL = "execute_command"
# 文案常量迁至 core.injection_markers（展示层过滤前缀与文案同源，防漂移）；
# 顶部以别名导入并保留 _ORCHESTRATION_NUDGE_TEXT 名称供既有引用使用。

# 慢步催促文案（core/tools.py 无该生成点；此处抽为模块级常量便于展示层过滤
# 前缀与生成点对账，防止文案漂移）。
SLOW_STEP_NUDGE_TEXT = (
    "你在最近一步耗时过长，且既没有调用任何工具、也没有输出任何"
    "正文内容，已出现明显的思考空转。请立即停止自检和复述思考过程，"
    "直接输出最终结论：任务已完成就给出中文结论，仍需操作就立即"
    "调用相应工具。禁止继续只输出思考内容。"
)
# 总结催促文案（工具已跑完却无结论）。
SUMMARY_NUDGE_TOOL_DONE_TEXT = (
    "工具已经执行完毕。现在必须只输出最终中文检查结论，"
    "说明发现的问题或下一步建议；不要调用工具，也不要只输出标点或 Markdown 标记。"
)
# 总结催促文案（只输出思考、既无工具也无正文）。
SUMMARY_NUDGE_THINKING_ONLY_TEXT = (
    "你刚才只输出了思考过程，既没有调用工具执行任何操作，"
    "也没有给出正式回复。请立即继续：需要执行操作就调用相应工具，"
    "任务已完成就直接输出中文结论；不要只输出思考内容。"
)
# 计划续推催促文案（{hints} 由调用处填充）。
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
    """登记某 turn_id 已注入编排提醒，并顺带清理过期条目（TTL 防无限增长）。"""
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
    """一批工具调用中的实质操作数：write_file / edit_file / 非只读 execute_command。

    execute_command 的只读判定与计划门禁同口径（classify_command_risk）；
    参数解析失败的调用按保守原则计入，空命令不计。
    """
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
    """是否注入编排提醒：主代理回合 + 实质操作超过阈值 + 无计划在途 + 未提醒过。

    阈值口径：count 必须"超过" _ORCHESTRATION_NUDGE_THRESHOLD（即 ≥ 4）才提醒，
    恰好 3 步及以内不提醒（与提醒文案及 system_prompt*.md 一致）。
    """
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
):
    """
    内部大模型驱动生成器：一律开启流式传输以对思考标签和工具链参数进行动态清洗和处理。

    computer_capability 是 Turn 起点冻结的电脑操控能力快照（Task 10）：
    同一 Turn 的每个 Step 复用，tools contract 不随设置中途变化漂移。
    """
    from core.model_manager import get_model_protocol

    request_protocol = get_model_protocol(model_name, api_base)
    if request_protocol is None:
        request_protocol = "anthropic" if str(model_name).startswith("anthropic/") else "openai"
    turn_snapshot = deepcopy(messages)
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
            # 作为独立 system 消息插入到最后一条 user 消息之前（而非改写稳定前缀）
            insert_at = len(messages)
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    insert_at = i
                    break
            messages.insert(insert_at, {"role": "system", "content": memory_section})

    tool_activity_seen = False
    final_summary_retry_count = 0
    # "只思考不产出"时的最终总结重试上限：连续多次欠佳仍尝试补救，
    # 避免长任务中一两轮失败就静默收尾（用户反馈"任务未执行完就结束"）。
    FINAL_SUMMARY_MAX_RETRIES = 2
    # 慢步熔断已触发的强催促次数（随本轮会话循环生命周期）。
    slow_step_nudge_count = 0
    # 退化（复读）检测：最近 3 步的正文指纹滑窗 + 已触发的收敛催促次数。
    # 与 slow_step_nudge_count 同生命周期（随回合重置），且只存本回合内存：
    # 不写 Journal、不入库、不落盘（与 read_signatures 同策略，避免污染缓存与历史）。
    recent_content_fingerprints = deque(maxlen=_DEGENERATE_WINDOW)
    degenerate_nudge_count = 0
    # D 方案：验证声明护栏已触发的提醒次数（随本轮会话循环生命周期）。
    verify_claim_nudge_count = 0
    # C8/C9：本回合工具调用总次数（Stop hook 输入字段；无 hooks 配置时零影响）。
    turn_tool_call_count = 0
    # 本回合是否出现过写入类/命令执行类工具（按工具名累计，只读不写请求）。
    used_write_tools = False
    used_command_tools = False
    # 重复工具调用抑制：同一签名成功 2 次后拦截，随本轮会话循环生命周期
    from core.harness.tool_pipeline import RepeatCallGuard

    repeat_call_guard = RepeatCallGuard()
    # 确定性无进展熔断：只比较后台任务/计划/工具结果哈希与修改类提交标记，
    # 连续 MAX_STALLED_STEPS 步无真实状态变化即终止回合，随本轮会话循环生命周期
    from core.harness.progress_guard import TurnProgressGuard

    progress_guard = TurnProgressGuard()
    # 电脑操控 Turn 状态（Task 10 接线点①）：capability 由入口层冻结传入；
    # lease/run 在模型首次真正调用 computer_action 时才建立（惰性 start_run，
    # 不让普通对话回合白白占用进程级排他租约），Turn 终态统一收尾。
    computer_controller = None
    computer_owner = None
    computer_run_id = ""
    computer_lease = None
    # Turn 收尾方式：completed=正常文本收尾（complete_run），cancelled/failed/
    # stopped（无进展熔断、Step 上限等）一律 stop，不留 running 租约。
    computer_exit_kind = "stopped"

    def _finalize_computer_run():
        """Turn 终态收尾（设计 14.2）：complete_run 或 stop 二选一，幂等。

        run 已被 controller 自行终结（如设置关闭触发 generation stop）时跳过；
        领域错误只留痕不掩盖回合的原始结局，也不让收尾异常打断调用方。
        Task 11a：原生 selector 建立的 target 租约同样在此收尾（native 与
        generic 的 capability 互斥，不会覆盖 generic 惰性 lease）。
        """
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
                # L1（2026-10-01 "接管—交还"修复）：用户接管态
                # （waiting_for_user）同样走"完成"路径终结并交还排他租约。
                # 转移表放行 waiting_for_user → completing 之前，这一步会抛
                # 非法状态转换并被吞成 warning，run 既无终态也无租约释放，
                # 下一回合被 computer_session_busy 挡死（2026-10-01 实机故障，
                # 日志 gui-runtime.log:11174/11519）。
                # retain 必须为 True、与普通收尾完全一致：外部 Edge/Chrome 上
                # 关闭 backend 会连用户此刻正在手动操作的浏览器窗口一起关掉，
                # 而接管场景恰恰正在用那个窗口；内置浏览器上保留会话也让下一
                # 回合免去 CDP 重连。面板页面属于 GUI 的 WebView2 控件，与 run
                # 是否保留无关。
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
        # 截图清除（2026-09-06 提速计划 Task 6）：回合终结后该会话所有电脑操控
        # 观察图立即折叠为占位符。provider_safety_required 的提前 return 不会
        # 走到这里（run 仍存活，模型恢复后还需要最后一张图）。
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
    # 编排提醒：本回合是否已注入、实质操作累计（每回合重置）
    orchestration_nudge_sent = False
    substantive_op_count = 0
    pending_persistence = []
    persistence_failure = None
    current_step = None
    current_tool_calls = []
    pending_tool_records = {}

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
        """取消时打捞待写队列：带 tool_calls 的 assistant 消息若缺对应 tool
        结果（工具未跑完就被终止），为每个缺失的 tool_call_id 合成取消占位
        结果。保证落库历史对下一轮 API 合法（悬空 tool_calls 会 400），
        同时保住终止前已完成的助手输出与工具结果，重开会话能看到进度。
        悬空 computer_action call 生成稳定 9 键 JSON 的 computer_cancelled
        结果（Task 10，设计 19.4），其余工具沿用通用取消占位文本。"""
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

    # 2. 注入后台子 Agent 回报数据 (XML task-notification)
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

    def rollback_turn_messages():
        """回滚本轮新增的消息残影；已 drain 的后台通知重新保留，避免丢失。"""
        messages[:] = turn_snapshot
        for notif in drained_notifications:
            messages.append({"role": "user", "content": notif})

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
    request_tools = [
        schema
        for schema in get_tools_schema()
        if allow_subagents
        or schema.get("function", {}).get("name")
        not in {"run_subagent", "plan_write", "plan_update"}
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
    # 【纯旁路】工具定义字符量快照（设计规格 §3.3；2026-09-23 拆分系统/MCP）：
    # 每步请求都会走到，重复更新无害；失败仅记 debug，绝不改变发送字节。
    _snapshot_tool_breakdown(request_tools)
    # 原生 OpenAI 协议（Task 11a，设计 11.1/11.2）：native tools 不进
    # request_tools（chat 格式），而是经 prepare_stream_request 的
    # native_computer_tools 参数追加在 flattened function tools 之后；
    # 双 target 尚未选择时固定 parallel_tool_calls=false，强制 selector
    # 单独完成一个 Step。selector 状态由固定 adapter 写入 target 租约。
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
    telemetry_session_id = _resolve_cache_session_identity(cache_session_id)
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
        # 占位：每次请求前都会在重试循环里用 _strip_media_and_merge 的结果覆盖，
        # 这里不再提前做一遍完整清洗（此前同一份数据每轮被处理两次，纯浪费 CPU）
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

    # 主模型不识图时，尝试用副模型先分析图片并替换为文字描述。
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

    # (pending_persistence and flush_persistence are now defined earlier)

    def raise_if_cancelled():
        if harness_runtime is not None:
            harness_runtime.heartbeat()
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt("用户终止了对话。")

    response_stream = None
    try:
        # 每个 Turn 只在首次 Provider 请求前读取一次上限并冻结到局部变量；
        # 运行中修改系统设置只影响后续 Turn
        from core.harness.turn_limits import load_turn_step_limit

        max_turns = load_turn_step_limit()
        if harness_runtime is not None:
            harness_runtime.mark_running()
        latest_step_metrics = None
        for turn in range(max_turns):
            raise_if_cancelled()
            # 无进展熔断：连续 MAX_STALLED_STEPS 步无确定性状态变化时，
            # 在创建新 Step（即发送下一次 Provider 请求）之前终止回合
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
                # 消息只走事件通道展示（GUI error 横幅 / CLI turn_error 红行），
                # 不再 yield 正文，避免同一文案在对话流里渲染两遍；
                # 持久化副本保证会话重载时仍可见。
                return
            if harness_runtime is not None:
                current_step = harness_runtime.begin_step()
            if turn > 0:
                # 轮次边界事件：GUI 流式渲染据此把每个 LLM 轮次拆成独立消息块，
                # 与历史重放（每轮一条 DB 记录）的结构对齐；CLI/子代理只认已知事件，自动忽略
                _notify_event(event_callback, "round_started", {"round": turn + 1})

            # API 调用指数退避与 Jitter 抖动重试
            response_stream = None
            inflight_holder = None
            max_api_retries = 5
            base_delay = 1.0
            # 超窗自动压缩只允许一次：压缩后仍超限说明单条消息/附件本身过大，
            # 继续重试只会反复重发超大载荷空烧额度
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

            for attempt in range(max_api_retries):
                raise_if_cancelled()
                try:
                    effective_request_tools = request_tools
                    # 先生成 provider 真正会收到的 canonical 消息，再写入 DeepSeek
                    # 账本。若先保存原始媒体/连续同角色消息，后续清洗会改变历史字节，
                    # 账本记录就不再等于实际请求前缀。
                    cleaned_current, folded_count, keep_media = _strip_media_and_merge(
                        messages, model_name, api_base
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
                        # Moonshot 推理模型要求所有带 tool_calls 的 assistant 消息
                        # 携带 reasoning_content；从数据库重建的历史消息可能已被剥掉，
                        # 这里补空格占位符，保证从数据库重建的历史消息也满足
                        # Moonshot 官方与经销商网关的该校验。
                        for _m in cleaned_messages:
                            if (
                                _m.get("role") == "assistant"
                                and _m.get("tool_calls")
                                and not _m.get("reasoning_content")
                            ):
                                _m["reasoning_content"] = " "
                    current_message_shape = _cache_message_fingerprints(cleaned_messages)
                    if current_message_shape != classified_message_shape:
                        # 覆盖指纹只进诊断记录（route_override_sha256），不塞进
                        # _provider_request_options 的返回值——后者直接交给
                        # litellm/native，多一个自定义键会触发未识别参数告警。
                        from core.route_overrides import (  # 局部导入，避免模块循环依赖
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
                            # 账本换代原因比形状差分更精确（形状差分只能笼统看出
                            # "上下文压缩或历史重写"）。非空即换代：request_cache_reason
                            # 同步覆写，语义保持一致——两者都算"非热启动"，
                            # 汇总侧 explicit_categories 也已把账本原因映射到既有分类。
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
                        # Task 2：同一行同时回答「客户端逻辑前缀变了吗」（上方打标前
                        # 构建的 sha256 / request_bytes）与「线上字节带几个显式标记」。
                        # 此处按打标后的真实载荷补测；_apply_cache_control 是原地改写，
                        # kwargs["messages"] 即最终上线字节。
                        (
                            request_diagnostic["cache_control_markers"],
                            request_diagnostic["request_bytes_final"],
                        ) = _cache_control_request_metrics(kwargs["messages"])
                    # 历史图片折叠属于正常节省行为，不再向用户弹提示打扰
                    # 超时随载荷放大：超大上下文 + 深度思考的首个 token 可能要几分钟，
                    # 90s 一刀切会让大载荷反复超时重发（每次重发输入都全额计费）
                    try:
                        _est_prompt_tokens = int(estimate_tokens(model_name, cleaned_messages) or 0)
                    except Exception:
                        _est_prompt_tokens = 0
                    attempt_timeout = min(
                        API_REQUEST_MAX_TIMEOUT_SECONDS,
                        API_REQUEST_TIMEOUT_SECONDS + max(0, _est_prompt_tokens) // 1500,
                    )
                    # 原生 native tools（Task 11a）：每个 Step 按当前 selector
                    # 状态重算（选择后 selector 从请求消失，computer 保留）；
                    # 双 target 能力固定 parallel_tool_calls=false。
                    if native_continuation_state is not None:
                        # Task 11b（设计 11.3）：原生续传 Step——input 只有
                        # computer_call_output，不重发消息历史；model/tools/
                        # instructions/options 经 turn_state 字节级重发，
                        # timeout 按本 Step 注入。
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
                    request_envelope = RequestEnvelope.capture(
                        prepared_request,
                        cache_scope=cache_scope,
                        request_reason=request_cache_reason,
                        attempt=attempt + 1,
                        deepseek_generation_id=active_deepseek_generation_id,
                        deepseek_message_count=active_deepseek_message_count,
                    )
                    if current_step is not None:
                        current_step.capture_request(request_envelope)
                    # 将同步阻塞的 completion 放到线程池中执行，使主循环可以响应取消事件。
                    # 在飞请求登记到 cancel_event 名下，停止路径可硬中断底层连接。
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
                                    # 空心跳唤醒外层适配层的 drain_public_events()：
                                    # status 事件只进 pending_events，若等待期不 yield，
                                    # 会积压到首个正文 chunk 才一次性冲刷（提示扎堆弹出）
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
                    break
                except Exception as api_err:
                    # 硬中断（用户取消关闭在飞流）会让请求线程抛连接错误；
                    # 先走取消判定，确保不误入下面的网络错误重试
                    raise_if_cancelled()
                    if isinstance(api_err, RequestWaitGiveUpError):
                        # 等待硬上限不是可重试错误：已等满上限时长，
                        # 重试只会再等一个上限，直接结束本轮
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
                            # 已压缩过一次仍然超限：单条消息/附件本身过大，继续重试只会
                            # 反复重发超大载荷空烧额度。直接失败并给出可操作的指引。
                            raise RuntimeError(
                                "历史压缩后仍超出模型上下文窗口，本轮请求已中止，"
                                "以避免反复重发超大载荷空耗额度。"
                                "建议 /new 开新会话，或移除超大附件/图片后重试。"
                            ) from api_err
                        overflow_compress_attempts += 1
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
                            # compress_session_memory 是正确的压缩函数（位于 memory_compressor），
                            # 旧的 mm.compress_session 是不存在的方法名，会抛 AttributeError 被吞掉
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
                                continue  # 跳回重试当前 API
                            # 已是最后一次重试，压缩后仍无法发出请求，抛出原始错误
                            # 而不是带着 response_stream=None 继续迭代导致 TypeError
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
                        raise api_err

                    if attempt < max_api_retries - 1:
                        # 速率限制（429 / TooManyRequests / RateLimitError）需要更长退避，
                        # 普通网络抖动用短退避即可。优先遵守上游 retry-after 头。
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
            for chunk in _stream_chunks_with_idle_notice(
                response_stream, event_callback, raise_if_cancelled,
                inflight_holder=inflight_holder, cancel_event=cancel_event,
            ):
                raise_if_cancelled()
                if chunk is None:
                    # 流中段等待心跳：空正文让外层适配层 drain_public_events()
                    # 实时冲刷积压的 status 事件（流挂起期间提示不再扎堆延迟）
                    yield ""
                    continue
                if first_token_time is None:
                    first_token_time = time.monotonic()
                update = stream_assembler.consume(chunk)
                if update.public_text:
                    yield update.public_text

            # 流式包装器在 finish 之后收到的最终 usage chunk 可能只落入
            # wrapper.chunks 而不再对外 yield（StopIteration 分支）。迭代中拿到的
            # 最后一个 usage 可能是不含缓存明细的中间态（prompt_tokens_details=None），
            # 导致 DeepSeek 等通道命中记账恒为 0。流结束后从 chunks 尾部补取最终 usage。
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

            # 步骤总耗时：自该次请求发出（wait_started_at）至最后一个 chunk 接收完成
            step_start_time = (
                wait_started_at
                if wait_started_at is not None
                else (first_token_time or stream_end_time)
            )
            duration_s = max(round(stream_end_time - step_start_time, 2), 0.01)

            # 首字延迟（TTFT）：自发起请求至首个 token 到达
            ttft_ms = (
                max(0, int(round((first_token_time - step_start_time) * 1000)))
                if first_token_time is not None
                else None
            )

            # 输出耗时与流速判定：
            # 1) 真实逐 chunk 流式：首 token 到末 chunk 之间的实际流式输出时长；
            # 2) 假流式/批式投递（如网关整包缓冲的 MockResponses 流）：
            #    首 token 与末 chunk 在 Python 内存中迭代仅耗费毫秒级（如 0.01s~0.03s），
            #    此时若直接除以 raw_output_duration 会算出几万 t/s 的荒谬除零毛刺。
            #    当流式输出时长极短（< 0.15s 且 token 较多）或速度算出来超出物理极限（> 1000 t/s）时，
            #    判定为批式投递，输出耗时自动取总耗时 duration_s，真实反映吞吐。
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
                # session_key 与会话身份对齐：多会话各自保活，互不顶替
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

            # 流结束：按最终 usage chunk 严格校验并统一记账一次。
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

            # Task 11b（设计 11.2/11.3）：原生 computer_call 消费——一个 Step
            # 对应一次模型决定 + 一次本地动作批次；执行后以 computer_call_output
            # 续传下一 Step。assembler 合并的重复终态载荷是"后者覆盖"，消费侧
            # 统一经 native_computer_call_from_stream 严格校验（parse 层拒绝
            # 冲突载荷）。最终文本或空响应则清空续传状态（previous_response_id
            # 不得跨响应复用），走下方普通收尾路径。
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
                    # 惰性建立 run（与 generic 语义一致）：首个原生 computer
                    # call 执行前建立；selector 已建立的租约直接复用。
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
                    # 续传材料只存 Turn 内存（NativeContinuationState 从产生
                    # computer_call 的真实请求 kwargs 捕获，字节级同序重发）。
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
                        native_call, native_result.observation
                    )
                    continue

            reasoning_content = stream_assembler.raw_reasoning_content
            public_content = assembled_response.content
            from core.turn_renderer import sanitize_protocol_text
            public_content = sanitize_protocol_text(public_content)

            # 兜底：模型只输出 thinking 但未生成正文时，把思考过程作为回复展示，
            # 避免 UI 在 "思考完成" 后出现空白，给用户造成 Agent 没有响应的错觉。
            # 注意：工具调用轮保持 content 为 None，不污染工具链签名。
            # 思考原文可能极长（数万字），直接全量促升会把大段内心独白倾倒给用户，
            # 因此只保留开头一段预览，其余截断。
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

            # 退化（复读）检测（修复②）：模型连续多步只重复同一段短正文
            # （"好。""嗯。"这类填充词或其近似变体）时，既不产出结论也不推进任务，
            # 前端气泡持续刷屏、状态栏长期停在"生成中"，用户视角等同卡死。
            # progress_guard 只比确定性状态、不读正文（有意设计），对这类失效不敏感，
            # 因此在这里按正文指纹滑窗补一道护栏：首次命中发 notice 并要求收敛，
            # 二次命中发 error 后走慢步熔断同一收尾路径结束回合。
            # 注意：正文指纹取 pre_promotion_content（促升前的原始正文快照）——
            # 上面的 reasoning 促升分支会把 public_content 改写成思考预览，若在促升后
            # 取指纹（曾经就是如此），退化检测会拿思考预览当正文指纹（口径错误），
            # 于是"连续几步只输出思考"被误判成正文复读，抢走促升兜底与总结催促的职责。
            # 口径修正：退化判定只保留正文指纹，思考指纹不再参与熔断。理由是思考内容
            # 在多步推理中天然高度相似（模型自检 / 复述思考），拿它做复读判据会误伤正常
            # 推理；而"只输出思考、无正文"的场景本就由既有的 reasoning 促升兜底与总结
            # 催促（FINAL_SUMMARY_MAX_RETRIES）护栏负责，并有专门测试守着。退化熔断因此
            # 只针对正文复读这一种故障模式——故障现场正是"每步只输出'好。''嗯。''OK。'
            # 这类填充词正文"。
            # 收紧（修复②）：判定只针对"空转步"——有工具调用说明模型正在推进任务，
            # 不属于"既不调工具也不给结论"的故障模式。若对有工具调用的步也判定，
            # 首次命中会 continue 直接丢弃该步真实 tool_calls（工具不执行、assistant
            # 消息不入历史），把"连续几步回相似短句但各自调用不同工具"的正常多步任务
            # 误杀。这里与慢步熔断的 `not tool_calls_list` 前置条件保持同一口径：
            # 只有无工具调用的步才累计指纹并参与判定。
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
                        # 复读本身无确定性状态变化：计入进展观察（停滞计数 +1）
                        progress_guard.observe(
                            _step_progress_observation(None, session_id=parent_session)
                        )
                        # Step 状态机要求 STREAMING → ASSISTANT_READY → COMPLETED，
                        # 此处尚未走常规 mark_assistant_ready 路径，先补迁移再进入下一轮。
                        if current_step is not None:
                            current_step.mark_assistant_ready()
                            if turn + 1 < max_turns:
                                current_step.complete()
                        continue
                    # 催促过仍未收敛：发 error 让用户可见（禁止静默收尾），并走慢步
                    # 熔断同一收尾路径结束循环、返回已产出的内容。
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

            # 慢步熔断（防止推理型小模型思考失控、静默空转、零产出）：
            # 单步耗时超过 SLOW_STEP_ABORT_SECONDS 且既未产出正文、也未发起工具
            # 调用时，说明模型在空转烧时间。首次触发先追加一条强催促（要求立即
            # 输出最终结论、禁止继续自检或复述思考）再进入下一轮；达到上限后再次
            # 触发则直接结束循环、返回已产出的内容，不再让它继续空转。
            # 未超过阈值时该分支恒不进入——对既有逻辑与稳定前缀字节零影响。
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
                    # 空转本身无确定性状态变化：计入进展观察（停滞计数 +1）
                    progress_guard.observe(
                        _step_progress_observation(None, session_id=parent_session)
                    )
                    # Step 状态机要求 STREAMING → ASSISTANT_READY → COMPLETED，
                    # 此处尚未走常规 mark_assistant_ready 路径，先补迁移再收尾。
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
            # 落库副本补充生成所用模型的显示元数据及性能指标（仅持久化，不发给 LLM）
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
                    # 上一轮因 max_tokens 截断（finish_reason == "length"）导致正文
                    # 未输出时，用相同上限重试几乎必然再次把预算全烧在思考上。
                    # 同样地，"只思考不产出"也说明思考已烧掉大量输出预算。两者都
                    # 临时翻倍输出上限（封顶 32768）并在催促文案中要求压缩思考，
                    # 提高重试成功率。
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
                    # 空 assistant 消息（content 为 None）不入库也不留内存：
                    # 留在历史里会让下一轮 API 报 400（Anthropic 拒绝空 content）
                    messages.pop()
                    if tool_activity_seen:
                        nudge_text = SUMMARY_NUDGE_TOOL_DONE_TEXT + length_hint
                    else:
                        # 模型只输出思考、未调用任何工具也未给出正文：催促它真正行动，
                        # 而不是把内心独白当成交付结果。
                        nudge_text = SUMMARY_NUDGE_THINKING_ONLY_TEXT + length_hint
                    summary_request = {
                        "role": "user",
                        "content": nudge_text,
                    }
                    messages.append(summary_request)
                    # 该 user 催促消息也需要持久化，否则下一轮重建历史时上下文会缺失
                    pending_persistence.append(("user", summary_request))
                    flush_persistence()
                    # 无工具执行的催促轮同样计入进展观察（无状态变化 → 停滞计数 +1）
                    progress_guard.observe(
                        _step_progress_observation(None, session_id=parent_session)
                    )
                    if current_step is not None and turn + 1 < max_turns:
                        current_step.complete()
                    # 内部兜底重试静默进行：底部状态栏已显示"正在干活..."，
                    # 这里不再发 status 通知暴露"模型未生成结论"的内部细节。
                    continue
                # D 方案：验证声明护栏——模型口头声称"测试通过/已验证"，但本回合
                # 既用过写入类工具、又完全没执行过命令（无验证证据）时，追加一次
                # 提醒要求它真实运行并贴出原始输出，避免"看起来有依据"的谎报收尾。
                # 未命中时该分支恒不进入，对既有请求字节与流程零影响。
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
                    # 催促本身无确定性状态变化：计入进展观察（停滞计数 +1）
                    progress_guard.observe(
                        _step_progress_observation(None, session_id=parent_session)
                    )
                    if current_step is not None and turn + 1 < max_turns:
                        current_step.complete()
                    continue
                # 计划仍有可执行项时不允许提前收尾：向模型注入续推消息，
                # 要求继续通过 run_subagent 完成剩余计划项（最多续推 2 次，
                # 避免模型坚持收尾时与续推消息无限打转）。
                if allow_subagents and parent_session and plan_continue_nudges < 2:
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
                        # 注入催促消息后继续下一轮（与下方 Stop-hook 续跑同模式：
                        # append + pending_persistence + flush_persistence）。
                        # v1.1.51 重构时曾丢失这两行 append，导致模型从未真正
                        # 收到催促、只有 UI 在空提醒——此处按 v1.1.25 原实现恢复。
                        messages.append(nudge_request)
                        pending_persistence.append(("user", nudge_request))
                        _notify_event(event_callback, "notice", {
                            "message": "计划仍有未执行项，已提醒模型继续执行。",
                        })
                        flush_persistence()
                        # 续推打转本身不产生进展：计划快照无变化时停滞计数 +1
                        progress_guard.observe(
                            _step_progress_observation(None, session_id=parent_session)
                        )
                        if current_step is not None and turn + 1 < max_turns:
                            current_step.complete()
                        continue
                # C8/C9（规格 01 §5.2/§5.5）：Stop hook——模型准备结束回合时询问
                # 用户配置的 Stop hook；请求续跑且未达上限（5 次）时注入上下文
                # 继续下一轮，超限强制结束并告警。无 hooks 配置时 evaluate_stop
                # 返回 None，整段零开销、行为与改造前完全一致。
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
                    # 审查修复：hook 请求续跑但已达回合步数上限——不再注入
                    # 上下文，显式告警（原实现静默丢弃，用户无从感知）。
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
                # 促升的思考正文此前只写入了历史、从未推送到 UI——用户会看到
                # 回合默默结束什么都没输出。此处补推给界面流式显示。
                if promoted_reasoning and public_content:
                    yield from _yield_content(public_content)
                flush_persistence()
                if current_step is not None:
                    current_step.complete()
                if harness_runtime is not None:
                    harness_runtime.complete()
                # 正常文本收尾：电脑操控 run 走 complete_run（Task 10，设计 14.2）
                computer_exit_kind = "completed"
                return

            if current_step is not None:
                current_step.mark_tools_running()

            # 输出被 max_tokens 截断时，工具参数几乎必然不完整（JSON 修复只能补全
            # 语法，无法找回被截断的内容）。此时绝不能执行任何工具——尤其是
            # write_file/edit_file 会把半截内容写进文件。改为向模型返回错误结果，
            # 让它拆小参数后重新调用。
            if finish_reason == "length":
                truncated_err = (
                    f"{TOOL_ERROR_PREFIX} 本次工具调用参数因输出长度限制被截断，未执行。"
                    "请把参数拆小（例如分多次 edit_file、缩短 content）后重新调用。"
                )
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
                # 截断拦截未执行任何工具：合成错误结果相同，无状态变化 → 停滞 +1
                progress_guard.observe(
                    _step_progress_observation(None, session_id=parent_session)
                )
                continue

            tool_activity_seen = True
            # C8/C9：累计本回合工具调用总次数（Stop hook 的 tool_call_count 输入）。
            turn_tool_call_count += len(tool_calls_list)
            # D 方案：累计本回合的工具类别（写入类/命令执行类），供收尾时的验证
            # 声明护栏判定；只看本轮已发的 tool_calls，不改动请求或执行流程。
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

            # 电脑操控批处理约束（Task 10 接线点②，设计 10.1）：收到 tool_calls
            # 之后、执行之前整批校验；多个 computer call 或与其他工具混用时本
            # Step 不执行任何电脑动作（零副作用），被拒 call 在 execute_decoded
            # 里合成稳定 9 键 JSON 错误结果，其余工具照常执行。
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
                # 惰性建立 Turn 电脑操控 lease（Task 10 接线点①）：模型首次真正
                # 调用 computer_action 时 start_run 取 run_id；失败（如被其他
                # 会话占用）时以带错误码的禁用 lease 继续本 Turn，tools contract
                # 不重算——模型调用 computer_action 会收到该稳定错误。
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
            for prepared_batch in batch_executor.prepare(
                tool_calls_list,
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

            # 编排提醒（软）：多步实质操作仍未建计划时拉一把，每回合最多一次。
            # 实质操作数在此累计；提醒本体的 append 推迟到全部 tool 结果（含
            # follow-up 观察消息）之后，见下方"工具结果全部入列后再追加"处。
            substantive_op_count += _count_substantive_ops(tool_calls_list)

            results = [record.message for record in records]
            # follow-up 观察 user 消息按 call ordinal 追加在全部 tool results
            # 之后（Task 10 接线点③，设计 19.3/19.4）：同一 Step 的既有提交
            # 边界内按序进入内存与持久化队列，不新增独立 SQLite 连接。
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

            # 工具结果全部入列后，再追加编排提醒：保证 messages 顺序为
            # assistant(tool_calls) → tool(...)（含 follow-up 观察消息）→ user(提醒)，
            # 满足 Anthropic「tool_use 后必须紧跟 tool_result」与 OpenAI「tool_calls
            # 必须被 tool 消息应答」的约束；pending_persistence 入队顺序与之一致。
            # 同一 turn 只注入一次：除生成器局部 orchestration_nudge_sent 外，还按
            # turn_id 做进程内去重（异常重入时不再重复注入）。
            if _should_nudge_orchestration(
                allow_subagents=allow_subagents,
                count=substantive_op_count,
                session_id=parent_session,
                turn_id=parent_turn_id,
                already_sent=orchestration_nudge_sent,
            ):
                orchestration_nudge_sent = True
                _register_orchestration_nudge(parent_turn_id)
                orchestration_request = {
                    "role": "user",
                    "content": _ORCHESTRATION_NUDGE_TEXT,
                }
                messages.append(orchestration_request)
                # 此处不重复加 if stream 守卫：flush_persistence() 内部已以
                # `if stream and pending_persistence` 整体兜底，非持久化会话
                # （stream=False，即 persist=False 的内部调用）下该 append 与随后的
                # flush 均为 no-op，行为等价；与 slow_step_nudge / summary_request /
                # claim_nudge 等既有 nudge 注入保持同一种写法（统一由 flush_persistence
                # 兜底，不逐点加守卫）。
                pending_persistence.append(("user", orchestration_request))
                # 静默注入：不发 notice 事件（UI 不弹提示卡片），只留 debug 日志
                logger.debug("已静默注入编排提醒（session=%s turn=%s）", parent_session, parent_turn_id)
                flush_persistence()
                progress_guard.observe(
                    _step_progress_observation(None, session_id=parent_session)
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
            # 深拷贝落库副本：上限提示同样绑定生成时所用模型，且不污染内存中的
            # limit_message（后者会进入发给 LLM 的 messages）
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
            harness_runtime.cancel()
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
            harness_runtime.cancel()
        raise
    except Exception as e:
        computer_exit_kind = "failed"
        try:
            response_stream.close()
        except Exception:
            pass
        # 异常路径同样回滚本轮未持久化的内存消息：既避免留下带 tool_calls 却无
        # tool 响应的悬空 assistant 消息（下一轮 API 会 400），也保持内存历史与 DB 一致
        rollback_turn_messages()
        if harness_runtime is not None:
            harness_runtime.fail(type(e).__name__)
        if e is persistence_failure:
            raise
        if event_callback is not None:
            user_facing_msg = _format_user_facing_error(e)
            _notify_event(event_callback, "error", {
                "message": user_facing_msg,
            })
            # 专用结束事件：chat_with_model_events 不再为异常回合发 turn_finished
            _notify_event(event_callback, "turn_error", {
                "message": user_facing_msg,
            })
        else:
            from prompt_toolkit.formatted_text import HTML

            safe_print(HTML(f"<ansiyellow>{_agent_display_name()}暂未完成当前步骤</ansiyellow>"))
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
    """PEP 562 模块级动态属性解析。

    - _token_usage_listener：set_token_usage_listener 运行期重绑的是
      core.llm_diagnostics 的模块全局，静态 re-export 会把导入时的 None 快照
      永久留在 core.llm 上误导内省；动态读取保证始终反映当前监听器。
    - mcp_manager / TOOLS_SCHEMA：兼容别名，惰性求值，避免 import
      core.llm 时就拉起 mcp 包全家桶（~1.1s）；不缓存到
      globals，保证外部属性访问始终反映当前值（测试 monkeypatch 友好）。
    """
    if name == "_token_usage_listener":
        from core import llm_diagnostics
        return getattr(llm_diagnostics, "_token_usage_listener")
    if name == "TOOLS_SCHEMA":
        return get_tools_schema()
    if name == "mcp_manager":
        return _get_mcp_manager()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
