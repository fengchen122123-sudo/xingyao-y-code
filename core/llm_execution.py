"""Tool parsing, JSON repair, pipeline context preparation, and vision fallback."""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from core.command_policy import classify_command_risk
from core.computer.provider_generic import computer_tool_arguments_projection
from core.harness.progress_guard import ProgressObservation
from core.harness.tool_adapters import ToolAdapterSet
from core.computer.tool_adapter import RESERVED_COMPUTER_TOOL_NAMES
from core.harness.tool_pipeline import (
    REPEAT_CALL_BLOCKED_TEXT,
    ToolExecutionContext,
    ToolPipeline,
    _escape_control_chars_in_json_strings,
    _repair_truncated_json,
)
from core.llm_dialects import (
    _provider_cache_affinity_options,
    _supports_vision_llm_first,
)
from core.llm_transport import (
    API_REQUEST_TIMEOUT_SECONDS,
    _dispatch_completion,
    _submit_daemon_call,
)
from core.model_manager import get_secondary_model, get_secondary_roles, load_config
from core.permission_policy import PermissionRisk
from core.permission_terminal import (
    ASK_USER_ERROR_PREFIX,
    TOOL_ERROR_PREFIX,
    TOOL_INTERNAL_ERROR_PREFIX,
    PermissionSystemError,
    _PLAN_BYPASS_ERROR_PREFIX,
    _PLAN_READ_ONLY_TOOLS,
    _has_pending_plan,
    _is_plan_turn,
    _mark_plan_turn,
    _notify_event,
    _pipeline_permission_checker,
    check_permission_and_ask,
    safe_print,
)
from core.tool_presenter import (
    build_tool_display,
    summarize_tool_finished,
    summarize_tool_started,
)
from core.tool_registry import ToolRegistry, build_default_registry

# 仅作 core.llm.tool_registry 不可用时的兜底（见 _get_active_tool_registry），
# 延迟构建：import 本模块不再额外实例化第三份注册表。
_tool_registry_fallback = None


def _get_fallback_tool_registry():
    global _tool_registry_fallback
    if _tool_registry_fallback is None:
        _tool_registry_fallback = build_default_registry()
    return _tool_registry_fallback
from core.tools import (
    edit_file,
    is_outside_workspace,
    manage_task,
    read_file,
    run_subagent,
    sandbox_bypass,
    write_file,
)

logger = logging.getLogger(__name__)


def _get_mcp_manager():
    from core.mcp_client import mcp_manager
    return mcp_manager


def _read_text_with_encoding_fallback(file_path):
    encodings = ('utf-8', 'gbk', 'latin-1')
    for enc in encodings:
        try:
            with open(file_path, 'r', encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()

def _tool_call_arguments(tc):
    raw_arguments = tc["function"].get("arguments", "{}")
    if not isinstance(raw_arguments, str):
        raise ValueError("工具参数 JSON 必须是字符串")

    try:
        arguments = json.loads(raw_arguments)
    except ValueError as direct_error:
        try:
            arguments = json.loads(
                _escape_control_chars_in_json_strings(raw_arguments)
            )
        except ValueError:
            try:
                arguments = json.loads(_repair_truncated_json(raw_arguments))
            except ValueError as repair_error:
                raise ValueError(
                    f"工具参数 JSON 解析失败：{repair_error}"
                ) from direct_error

    if not isinstance(arguments, dict):
        raise ValueError("工具参数 JSON 必须是对象")
    return arguments


def _tool_call_name(tool_call) -> str:
    """dict / 属性两种 tool_call 形态统一取 function.name。"""
    if isinstance(tool_call, dict):
        return str(((tool_call.get("function") or {}).get("name")) or "")
    function = getattr(tool_call, "function", None)
    return str(getattr(function, "name", "") or "")


def _persisted_tool_calls(tool_calls):
    """落库 tool_calls 的脱敏副本（设计 22.3/22.4，Task 16 隐私审计）。

    computer 保留名的参数只留 computer_tool_arguments_projection 摘要
    （输入正文/组合键/URL 只存 code point 数与 SHA-256），其余工具原样。
    Provider 请求侧的 message_to_append 不经本函数——assistant tool_calls
    与 tool 结果必须原样配对重发（协议要求），脱敏只作用于持久化副本。
    """
    projected = []
    for tool_call in tool_calls:
        name = _tool_call_name(tool_call)
        if name not in RESERVED_COMPUTER_TOOL_NAMES:
            projected.append(tool_call)
            continue
        sanitized = computer_tool_arguments_projection(
            name, _raw_tool_call_arguments(tool_call)
        )
        if isinstance(tool_call, dict):
            copy_of_call = dict(tool_call)
            function = dict(copy_of_call.get("function") or {})
            function["arguments"] = json.dumps(
                sanitized, ensure_ascii=False, sort_keys=True
            )
            copy_of_call["function"] = function
            projected.append(copy_of_call)
        else:
            # 属性形态（桥接层透传）：绝不让原始参数落库
            projected.append({
                "id": getattr(tool_call, "id", None),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(
                        sanitized, ensure_ascii=False, sort_keys=True
                    ),
                },
            })
    return projected


def _raw_tool_call_arguments(tool_call):
    """取 tool_call 的原始 arguments（JSON 字符串或已解析 dict），绝不解析失败。"""
    if isinstance(tool_call, dict):
        return (tool_call.get("function") or {}).get("arguments", "")
    function = getattr(tool_call, "function", None)
    return getattr(function, "arguments", "")


def _tool_event_parts(tool_call):
    if isinstance(tool_call, dict):
        return (
            tool_call.get("id", ""),
            tool_call["function"]["name"],
            _tool_call_arguments(tool_call),
        )
    return tool_call.call_id, tool_call.name, tool_call.arguments


def _tool_event_payload_from_parts(call_id, function_name, arguments):
    if function_name in RESERVED_COMPUTER_TOOL_NAMES:
        # 设计 22.3（Task 16 隐私审计）：输入动作正文不进 GUI/CLI trace——
        # computer 保留名工具的事件参数只携带脱敏投影（kind/target + 长度/
        # 哈希），preview 恒空；summary/display 用空参数生成，绝不拼接原文。
        return {
            "id": call_id,
            "name": function_name,
            "preview": "",
            "arguments": computer_tool_arguments_projection(
                function_name, arguments
            ),
            "summary": summarize_tool_started(function_name, {}),
            "display": build_tool_display(function_name, {}),
        }
    preview = ""
    if function_name in ("read_file", "write_file", "edit_file"):
        preview = arguments.get("file_path", "")
    elif function_name in ("search_code", "search_news"):
        preview = arguments.get("query", "")
    elif function_name == "fetch_url":
        preview = arguments.get("url", "")
    elif function_name == "get_weather":
        preview = arguments.get("city", "") or "自动定位"
    elif function_name == "execute_command":
        preview = arguments.get("command", "")
        # 长命令（尤其 PowerShell / curl / python -c）预览截短，避免刷屏
        if preview and len(preview) > 40:
            preview = preview[:37] + "..."
    elif function_name == "glob_files":
        preview = arguments.get("pattern", "")
    elif function_name == "list_directory":
        preview = arguments.get("dir_path", ".")
    elif function_name == "run_subagent":
        preview = arguments.get("task_description", "")
    return {
        "id": call_id,
        "name": function_name,
        "preview": str(preview)[:120],
        "arguments": arguments,
        "summary": summarize_tool_started(function_name, arguments),
        "display": build_tool_display(function_name, arguments),
    }


def _tool_event_payload(tool_call):
    return _tool_event_payload_from_parts(*_tool_event_parts(tool_call))


def _tool_finished_event_payload(tool_call, result, duration_ms):
    call_id, function_name, arguments = _tool_event_parts(tool_call)
    ok = _tool_result_ok(function_name, result)
    return {
        **_tool_event_payload_from_parts(call_id, function_name, arguments),
        "ok": ok,
        "summary": _tool_output_preview(function_name, arguments, result),
        "result": _tool_result_text(result),
        "duration_ms": max(0, int(duration_ms)),
        "display": build_tool_display(function_name, arguments, result, ok=ok),
    }


def _tool_output_preview(function_name, arguments, result):
    """Return a stable terminal summary without exposing raw tool output."""
    ok = _tool_result_ok(function_name, result)
    return summarize_tool_finished(function_name, arguments, result, ok)


# tool_finished 事件里携带的完整工具结果上限（字符数）：
# GUI 需要展开查看原始输出，但不能让单条事件把 IPC 通道撑爆。
TOOL_RESULT_EVENT_MAX_CHARS = 4000

# 模型只输出思考未生成正文时，促升为公开回复的思考预览长度上限（字符数）。
# 思考原文可能长达数万字，全量展示会把大段内心独白倾倒给用户。
PROMOTED_REASONING_PREVIEW_CHARS = 600

# 慢步熔断阈值（秒）：单步耗时超过该值且既未产出正文、也未发起工具调用，
# 判为推理型小模型思考失控/静默空转，先强催促一次、再触发即结束本回合。
# 阈值取 60s 而非更激进的值：思考型大模型在长上下文中单步思考 30~60s 属正常，
# 阈值过低会把正常深度思考误判为空转（neohorse-1-9b 实测失控为 46.9s，60s 仍可捕获）。
SLOW_STEP_ABORT_SECONDS = 60.0
# 慢步熔断允许的强催促次数上限：达到上限后再次触发直接结束循环、返回已有内容。
SLOW_STEP_MAX_NUDGES = 1

# ─── D 方案：验证声明检测护栏 ────────────────────────────────────────
# 触发条件：本回合调用过写入类工具（名称含 write/edit）、却一次命令执行类工具都没
# 调用（名称含 execute_command/run_command/shell/bash/terminal/exec），而收尾正文却
# 声称"测试通过/已验证"。此时模型是在口头交付一份从未真实运行过的结论（典型的
# "谎报验证"），追加一条 user 提醒要求它立刻跑命令并贴出原始输出。
# 只命中这些强表述：刻意不匹配"已确认/已核对"等弱表述，避免干扰正常收尾。
VERIFY_CLAIM_MARKERS = (
    "测试通过",
    "运行通过",
    "已通过",
    "已验证通过",
    "已验证完成",
    "跑通了",
    "全部通过",
    "all tests passed",
    "tests passed",
)
# 命中否定过滤（评审建议1）：标记前 4 个字符内出现这些词，视为否定/疑问语境，
# 不算验证声明（例如"没有全部通过""是否已通过""尚未通过"）。
CLAIM_NEGATION_PREFIXES = ("未", "没", "无", "尚未", "不能", "是否", "并非", "不算")
# 验证声明护栏允许的提醒次数上限：达到上限后不再介入（避免与模型反复声明打转）。
VERIFY_CLAIM_MAX_NUDGES = 1
# 命中时追加的提醒文案（模块级常量便于测试引用，避免文案漂移）。
VERIFY_CLAIM_NUDGE_TEXT = (
    "你声称已完成验证，但本回合没有执行任何命令。"
    "请立即用工具真实运行验证并贴出原始输出；若未验证，请如实说明未验证。"
)
# 命令执行类工具名标记：按工具名粗分类，不做精确白名单（新增同类工具自动覆盖）。
COMMAND_TOOL_NAME_MARKERS = (
    "execute_command",
    "run_command",
    "shell",
    "bash",
    "terminal",
    "exec",
)


def _tool_call_activity_kinds(tool_calls):
    """按工具名粗分一批工具调用里是否出现写入类/命令执行类工具。

    返回 (used_write_tools, used_command_tools)。纯函数、无副作用：只看名称
    子串（写入类：write/edit；命令执行类：COMMAND_TOOL_NAME_MARKERS），不解析
    参数、不看执行结果，因此对既有请求字节与流程零影响。
    """
    used_write_tools = False
    used_command_tools = False
    for call in tool_calls or ():
        function = call.get("function") if isinstance(call, dict) else None
        name = str((function or {}).get("name") or "").lower()
        if not name:
            continue
        if "write" in name or "edit" in name:
            used_write_tools = True
        if any(marker in name for marker in COMMAND_TOOL_NAME_MARKERS):
            used_command_tools = True
    return used_write_tools, used_command_tools


def _claims_verification_without_evidence(
    final_text, used_write_tools, used_command_tools
):
    """收尾正文是否"口头声称已验证、却没有命令执行证据"。

    当且仅当：本回合用过写入类工具、没用过命令执行类工具、且正文命中
    VERIFY_CLAIM_MARKERS 任一标记时返回 True。文本为空/空白返回 False；
    只命中强表述（"测试通过/已验证…"），"已确认/已核对"等弱表述刻意不匹配；
    否定/疑问语境（标记前 4 字内含 CLAIM_NEGATION_PREFIXES）与"通过率"不算命中。

    已知边界（评审记录，属有意的低成本取舍）：本函数只按工具**名称**分类，
    不检查命令的执行结果，因此模型跑一条无关或失败的命令即可让护栏失效；
    父回合把验证委托给 run_subagent 时也不算命令证据。护栏只做一次提醒、
    不做强拦截，故接受该边界。
    """
    if not used_write_tools or used_command_tools:
        return False
    text = str(final_text or "")
    if not text.strip():
        return False
    lowered = text.lower()
    for marker in VERIFY_CLAIM_MARKERS:
        haystack = lowered if marker.isascii() else text
        needle = marker.lower() if marker.isascii() else marker
        start = 0
        while True:
            idx = haystack.find(needle, start)
            if idx < 0:
                break
            window = text[max(0, idx - 4):idx]
            following = text[idx + len(marker):idx + len(marker) + 1]
            # 否定/疑问语境（如"没有全部通过""是否已通过"）与"通过率"这类
            # 非声明用法不算验证声明。
            if not any(neg in window for neg in CLAIM_NEGATION_PREFIXES) and following != "率":
                return True
            start = idx + 1
    return False


def _tool_result_text(result):
    """把工具消息正文取出并截断，作为 tool_finished 事件的 result 字段。

    这是附加字段：CLI 只读 summary，多出的 result 对它无副作用。
    """
    try:
        content = result.get("content", "") if isinstance(result, dict) else result
    except Exception:
        content = ""
    text = str(content or "")
    if len(text) > TOOL_RESULT_EVENT_MAX_CHARS:
        return text[:TOOL_RESULT_EVENT_MAX_CHARS] + "...（已截断）"
    return text


def _tool_result_ok(function_name, result):
    content = str(result.get("content", "") or "")
    if function_name in {"run_subagent", "ask_user"}:
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and payload.get("status"):
            return payload["status"] in {"running", "completed", "selected"}
    return not content.startswith((
        TOOL_ERROR_PREFIX,
        ASK_USER_ERROR_PREFIX,
        TOOL_INTERNAL_ERROR_PREFIX,
        _PLAN_BYPASS_ERROR_PREFIX,
        "计划执行约束：",
        "计划配置错误：",
        "计划运行状态由真实调度器管理",
        "新建计划项必须是 pending",
    ))


# 计划控制工具的结果正文不作为进展信号：真实状态转换已由计划 canonical
# 快照指纹单独捕获。否则模型只需在 plan_update/plan_retry/plan_finish 等返回
# 文案之间来回切换，就会令 tool_results_sha256 每步变化并永久绕过无进展熔断。
# run_subagent 需要区分计划绑定与无计划调用，不能在这里全局忽略，详见
# _unplanned_subagent_progress_part。
_PLAN_STATE_RESULT_TOOLS = frozenset({
    "plan_write",
    "plan_update",
    "plan_read",
    "plan_retry",
    "plan_resume_takeover",
    "plan_finish",
    "ask_user",
})


def _canonical_arguments_signature(name, tool_call):
    """读取签名 = tool name + canonical arguments 的 SHA-256（仅存 Turn 内存）。"""
    raw = ""
    if tool_call:
        raw = str((tool_call.get("function") or {}).get("arguments") or "")
    try:
        canonical = json.dumps(
            json.loads(raw or "{}"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        canonical = raw
    return hashlib.sha256(f"{name}:{canonical}".encode("utf-8")).hexdigest()


def _unplanned_subagent_progress_part(message, tool_call):
    """返回无计划子任务的稳定进展材料；计划绑定调用返回 None。

    参数签名区分模型实际派发的不同工作。结果只保留稳定的 status/result，
    明确排除 task_id、耗时与 error 等运行噪声；计划绑定调用则完全交给计划
    快照指纹判定，不能靠改描述或动态任务 id 重置停滞计数。
    """
    raw_arguments = ""
    if tool_call:
        raw_arguments = str(
            (tool_call.get("function") or {}).get("arguments") or ""
        )
    try:
        arguments = json.loads(raw_arguments or "{}")
    except (TypeError, ValueError):
        arguments = {}
    if isinstance(arguments, dict) and str(
        arguments.get("plan_item_id", "") or ""
    ).strip():
        return None
    if not _tool_result_ok("run_subagent", message):
        return None

    content = str(message.get("content", "") or "")
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        stable_result = {"status": str(payload.get("status", "") or "")}
        if "result" in payload:
            stable_result["result"] = payload.get("result")
    else:
        # 前台 completed 路径直接返回子代理正文；正文就是实际成果信号。
        stable_result = {"status": "completed", "result": content}
    return [
        "run_subagent",
        _canonical_arguments_signature("run_subagent", tool_call),
        stable_result,
    ]


def _step_progress_observation(records, tool_calls_by_id=None, session_id=None):
    """构造本 Step 的确定性进展观察（供 TurnProgressGuard 判定）。

    只聚合确定性状态：后台任务指纹、计划 canonical 快照指纹、
    非观察类工具结果哈希、修改类提交标记；不读取模型自然语言。
    附加本步只读工具签名，供 guard 做有界（MAX_UNIQUE_READS）读取进展
    判定（所有会话通用，普通只读调查链不再被误判停滞）；签名不写 Journal。
    """
    from core.harness.progress_guard import ProgressObservation
    from core.harness.tool_pipeline import (
        _PLAN_READ_ONLY_TOOLS,
        REPEAT_CALL_BLOCKED_TEXT,
    )
    from core.plan import plan_state_fingerprint
    from core.task_manager import background_tasks_fingerprint

    result_parts = []
    mutation_committed = False
    read_signatures = []
    for record in records or ():
        message = getattr(record, "message", None) or {}
        name = str(message.get("name", "") or "")
        content = str(message.get("content", "") or "")
        # RepeatCallGuard 拦截结果是固定文案，不代表任何业务状态变化
        if content == REPEAT_CALL_BLOCKED_TEXT:
            continue
        ok = _tool_result_ok(name, message)
        if ok and name in {"write_file", "edit_file"}:
            mutation_committed = True
        elif ok and name == "execute_command" and tool_calls_by_id:
            command = ""
            tool_call = tool_calls_by_id.get(message.get("tool_call_id")) or {}
            try:
                command = str(
                    json.loads(tool_call.get("function", {}).get("arguments") or "{}")
                    .get("command", "")
                )
            except (TypeError, ValueError):
                command = ""
            if command:
                from core.command_policy import classify_command_risk
                from core.permission_policy import PermissionRisk

                if classify_command_risk(command) is not PermissionRisk.QUERY:
                    mutation_committed = True
        if ok and name in _PLAN_READ_ONLY_TOOLS and tool_calls_by_id is not None:
            read_signatures.append(
                _canonical_arguments_signature(
                    name, tool_calls_by_id.get(message.get("tool_call_id"))
                )
            )
        if name == "run_subagent":
            progress_part = _unplanned_subagent_progress_part(
                message,
                (tool_calls_by_id or {}).get(message.get("tool_call_id")),
            )
            if progress_part is not None:
                result_parts.append(progress_part)
            continue
        if name not in _PLAN_READ_ONLY_TOOLS and name not in _PLAN_STATE_RESULT_TOOLS:
            result_parts.append([name, content])

    canonical = json.dumps(
        result_parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return ProgressObservation(
        task_snapshots_sha256=background_tasks_fingerprint(),
        plan_snapshot_sha256=plan_state_fingerprint(session_id),
        tool_results_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        mutation_committed=mutation_committed,
        read_signatures=tuple(read_signatures),
    )


# JSON 参数修复/控制字符转义与 harness 执行管线共用同一实现（单份定义在
# core.harness.tool_pipeline，顶部统一导入复用，避免双份实现行为漂移）。
def _show_file_diff(
    file_path,
    old_content,
    new_content,
    event_callback,
    panel_title,
    truncated_note,
    tool_call_id="",
):
    """Compatibility wrapper for the extracted diff adapter."""
    from core.harness.tool_adapters import _show_file_diff as _render_file_diff

    return _render_file_diff(
        file_path,
        old_content,
        new_content,
        event_callback,
        panel_title,
        truncated_note,
        tool_call_id=tool_call_id,
    )

def _get_imom_active_publish_root():
    """Compatibility wrapper for the extracted imom adapter."""
    from core.harness.tool_adapters import _get_imom_active_publish_root as _get_root

    return _get_root()


def _get_active_tool_registry():
    try:
        import core.llm as llm
        return getattr(llm, "tool_registry", None) or _get_fallback_tool_registry()
    except Exception:
        return _get_fallback_tool_registry()


def _get_pipeline_permission_checker():
    try:
        import core.llm as llm
        return getattr(llm, "_pipeline_permission_checker", _pipeline_permission_checker)
    except Exception:
        return _pipeline_permission_checker


def _get_permission_checker():
    try:
        import core.llm as llm
        return getattr(llm, "check_permission_and_ask", check_permission_and_ask)
    except Exception:
        return check_permission_and_ask


def _llm_first(name, fallback):
    """llm-first 解析入口层绑定（测试/外部 patch core.llm.<name> 必须生效）。"""
    try:
        import core.llm as llm
        return getattr(llm, name, fallback)
    except Exception:
        return fallback


def _sandbox_outside_guard(
    function_name,
    path_value,
    event_callback,
    permission_owner,
    parent_turn_id,
    permission_source,
    cancel_event,
    **kwargs,
):
    """Compatibility wrapper for the extracted sandbox adapter."""
    from core.harness.tool_adapters import _sandbox_outside_guard as _guard

    return _guard(
        function_name,
        path_value,
        event_callback,
        permission_owner,
        parent_turn_id,
        permission_source,
        cancel_event,
        permission_checker=kwargs.get("permission_checker") or _get_permission_checker(),
        is_outside_workspace=kwargs.get(
            "is_outside_workspace",
            _llm_first("is_outside_workspace", is_outside_workspace),
        ),
        sandbox_bypass=kwargs.get("sandbox_bypass", sandbox_bypass),
        permission_error_type=kwargs.get("permission_error_type", PermissionSystemError),
    )

def _build_tool_pipeline_and_context(
    event_callback=None,
    *,
    parent_session=None,
    parent_turn_id=None,
    cancel_event=None,
    allow_subagents=True,
    permission_owner=None,
    permission_source="主代理",
    computer_lease=None,
    computer_native_selection=None,
    journal_event_sink=None,
    harness_step=None,
):
    from core.harness.tool_adapters import ToolAdapterSet
    from core.harness.tool_pipeline import ToolExecutionContext, ToolPipeline
    from core.hooks.lifecycle import get_default_hook_runner
    from core.tool_registry import ToolRegistry

    active_tool_registry = _get_active_tool_registry()
    active_checker = _get_pipeline_permission_checker()
    bound_execute = active_tool_registry.execute
    registry_execute_override = (
        bound_execute
        if getattr(bound_execute, "__func__", None) is not ToolRegistry.execute
        else None
    )
    def _adapter(name, fallback):
        try:
            import core.llm as llm
            return getattr(llm, name, fallback)
        except Exception:
            return fallback

    adapters = ToolAdapterSet(
        read_file=_adapter("read_file", read_file),
        write_file=_adapter("write_file", write_file),
        edit_file=_adapter("edit_file", edit_file),
        run_subagent=_adapter("run_subagent", run_subagent),
        manage_task=_adapter("manage_task", manage_task),
        show_file_diff=_adapter("_show_file_diff", _show_file_diff),
        get_imom_active_publish_root=_adapter("_get_imom_active_publish_root", _get_imom_active_publish_root),
        sandbox_guard=_adapter("_sandbox_outside_guard", _sandbox_outside_guard),
        sandbox_bypass=_adapter("sandbox_bypass", sandbox_bypass),
        read_text=_adapter("_read_text_with_encoding_fallback", _read_text_with_encoding_fallback),
        notify_event=_adapter("_notify_event", _notify_event),
        mark_plan_turn=_adapter("_mark_plan_turn", _mark_plan_turn),
        registry_execute_override=registry_execute_override,
        is_outside_workspace=_adapter("is_outside_workspace", is_outside_workspace),
        safe_print=_adapter("safe_print", safe_print),
    )
    context = ToolExecutionContext(
        event_callback=event_callback,
        parent_session=parent_session,
        parent_turn_id=parent_turn_id,
        cancel_event=cancel_event,
        allow_subagents=allow_subagents,
        permission_owner=permission_owner,
        permission_source=permission_source,
        permission_checker=active_checker,
        registry=active_tool_registry,
        mcp_manager=_adapter("_get_mcp_manager", _get_mcp_manager)(),
        computer_lease=computer_lease,
        computer_native_selection=computer_native_selection,
        # C6/C7 接线：注入默认 HookRunner（进程内单例）。无 hooks 配置时
        # runner 各层快速短路，工具链行为与字节保持改造前完全一致（H-T16）。
        hook_runner=get_default_hook_runner(),
        # 续跑记录（S1-b）：由调用方（core/llm.py 的 turn/step 作用域）注入。
        # 默认 None 时既不写 journal 也不写 tool_call_count，行为与改造前一致。
        journal_event_sink=journal_event_sink,
        harness_step=harness_step,
    )
    pipeline = ToolPipeline(
        adapters,
        permission_error_type=PermissionSystemError,
        truncate_result=_truncate_tool_result_for_context,
        plan_turn_checker=_is_plan_turn,
        pending_plan_checker=_has_pending_plan,
    )
    return pipeline, context


def _execute_single_tool_call(
    tc,
    event_callback=None,
    *,
    parent_session=None,
    parent_turn_id=None,
    cancel_event=None,
    allow_subagents=True,
    permission_owner=None,
    permission_source="主代理",
):
    """Compatibility wrapper around the extracted deterministic pipeline."""
    pipeline, context = _build_tool_pipeline_and_context(
        event_callback,
        parent_session=parent_session,
        parent_turn_id=parent_turn_id,
        cancel_event=cancel_event,
        allow_subagents=allow_subagents,
        permission_owner=permission_owner,
        permission_source=permission_source,
    )
    return pipeline.execute_one(tc, context)


_ORIGINAL_EXECUTE_SINGLE_TOOL_CALL = _execute_single_tool_call

# 旧测试和外部调用方可能仍从 core.llm 导入该内部兼容名；实现只保留在
# 固定 Harness 管线中，避免形成第二套重复调用判定逻辑。
from core.harness.tool_pipeline import RepeatCallGuard as _RepeatCallGuard

# ─── 工具结果入上下文前的截断 ────────────────────────────────────
# 这是最大的 token 泄漏点：一次 fetch_url 可以塞进 512KB（≈13 万 token）原文，
# 一次 pip install 的 stdout 可以是几 MB。截断只影响送进模型上下文/落库的内容，
# 不影响工具本身的执行结果与终端展示。
_TOOL_RESULT_MAX_CHARS = 24000            # 默认上限（约 8K token）
_TOOL_RESULT_LIMITS = {
    "execute_command": 16000,             # 保留头尾：报错信息几乎都在结尾
    "fetch_url": 16000,                   # 保留头部：正文开头信息密度最高
    "search_news": 8000,
}
_TOOL_RESULT_TAIL_TOOLS = {"execute_command"}
# read_file 自带行数/字节上限，且编辑流程依赖完整内容，默认不做二次截断。
# 但大文件全文读入是前缀缓存条目膨胀的最大来源（单个结果可达数万 token），
# 在按容量驱逐的网关上更容易被挤掉、失效后的重建成本也更高。可在 config.json
# 顶层设 "read_file_result_max_chars": 24000 启用截断（保头尾），中间锚点丢失
# 可能影响 edit 流程，故默认保持不限。
_TOOL_RESULT_EXEMPT = {"read_file"}


def _read_file_result_limit():
    """read_file 结果入上下文的字符上限；0/缺省 = 不限。配置读取失败时静默不限。"""
    try:
        from core.model_manager import load_config
        return int(load_config().get("read_file_result_max_chars", 0) or 0)
    except Exception:
        return 0


def _truncate_tool_result_for_context(function_name, text):
    if function_name in _TOOL_RESULT_EXEMPT:
        limit = _read_file_result_limit() if function_name == "read_file" else 0
        if not limit or len(text) <= limit:
            return text
        head_len = limit // 2
        tail = text[-(limit - head_len):]
        return (
            f"{text[:head_len]}\n"
            f"…[文件过长，中间省略 {len(text) - limit} 字符。"
            f"如需中间内容请按行号范围重新 read_file]…\n{tail}"
        )
    limit = _TOOL_RESULT_LIMITS.get(function_name, _TOOL_RESULT_MAX_CHARS)
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    if function_name in _TOOL_RESULT_TAIL_TOOLS:
        head_len = limit // 4
        head = text[:head_len]
        tail = text[-(limit - head_len):]
        return f"{head}\n…[输出过长，中间省略 {dropped} 字符]…\n{tail}"
    return (
        f"{text[:limit]}\n"
        f"…[结果过长，已截断 {dropped} 字符。如需后续内容请用更精确的参数重新调用]"
    )

def _call_vision_model_for_content(secondary, content, cancel_event=None, max_retries=3):
    """调用支持视觉的副模型，传入完整 content（含图片），返回回复文本；失败返回空字符串。"""
    kwargs = {
        "model": secondary["litellm_model"],
        "messages": [{"role": "user", "content": content}],
        "api_key": secondary["api_key"],
        "api_base": secondary["base_url"],
        "max_tokens": 2048,
        "timeout": API_REQUEST_TIMEOUT_SECONDS,
    }
    kwargs.update(_provider_cache_affinity_options(
        secondary["litellm_model"],
        secondary["base_url"],
        cache_scope="secondary:vision",
        tool_contract="no-tools",
    ))

    warning_logger = logging.getLogger("py.warnings")

    for attempt in range(max_retries):
        try:
            future = _submit_daemon_call(_dispatch_completion, **kwargs)
            try:
                while not future.done():
                    if cancel_event is not None and cancel_event.is_set():
                        future.cancel()
                        return ""
                    try:
                        future.result(timeout=0.1)
                    except Exception:
                        pass
                exc = future.exception()
                if exc is not None:
                    raise exc
                resp = future.result()
            finally:
                future.cancel()

            if resp and getattr(resp, "choices", None):
                c = resp.choices[0].message.content
                if c is not None:
                    text = str(c).strip()
                    if text:
                        return text
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
        except Exception as e:
            warning_logger.warning(
                "副模型图片分析失败 (attempt %d/%d): %s %s",
                attempt + 1, max_retries, type(e).__name__, e,
            )
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
    return ""


def _vision_fallback_text(user_text=""):
    """图片分析失败时替换用的提示文本，明确让主模型直接回复用户。"""
    hint = (
        "[当前主模型和副模型均不支持图片输入或图片分析失败，无法处理图片。"
        "请直接回复用户：无法分析图片，不要调用任何工具。]"
    )
    if user_text:
        return f"{user_text}\n\n{hint}"
    return hint


def _describe_images_with_secondary(messages, main_model_name, event_callback=None, cancel_event=None):
    """主模型不识图时，直接用副模型分析本轮最新图片并替换为文字描述/回答。"""
    if _supports_vision_llm_first(main_model_name):
        return

    # 只处理最近一条用户消息中的图片；历史图片即使还在上下文里也不再重复分析
    target_msg = None
    for msg in reversed(messages):
        if msg.get("role") == "user":
            target_msg = msg
            break
    if target_msg is None:
        return

    content = target_msg.get("content")
    if not isinstance(content, list):
        return
    image_items = [
        item for item in content
        if isinstance(item, dict) and item.get("type") in ("image", "image_url")
    ]
    if not image_items:
        return
    text_items = [
        item for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]

    user_text = " ".join(t.get("text", "") for t in text_items).strip()

    from core.model_manager import get_secondary_model, get_secondary_roles
    roles = get_secondary_roles()
    secondary = get_secondary_model() if roles.get("vision") else None

    if not secondary or not _supports_vision_llm_first(secondary["litellm_model"]):
        _notify_event(event_callback, "notice", {
            "message": "当前主模型不支持图片输入，且副模型无法分析图片，已将图片替换为占位说明。",
        })
        target_msg["content"] = _vision_fallback_text(user_text)
        return

    _notify_event(event_callback, "notice", {
        "message": f"主模型不支持图片输入，正在使用副模型 {secondary['name']} 分析图片...",
    })

    # 把用户的问题也一起给副模型，让副模型直接作答
    if user_text:
        vision_prompt = f"请基于图片的视觉内容回答以下问题：{user_text}"
    else:
        vision_prompt = (
            "请以视觉助手身份理解这张图片：先判断图片类型（如截图/海报/照片/图表/界面截图等），"
            "再说明图片的主题、关键视觉元素和传达的信息；"
            "如果图片包含文字，提炼文字核心要点而非逐字复制。"
        )
    sec_content = [{"type": "text", "text": vision_prompt}] + image_items

    answer = _call_vision_model_for_content(secondary, sec_content, cancel_event=cancel_event)
    if answer:
        if user_text:
            target_msg["content"] = f"{user_text}\n\n[图片分析结果]\n{answer}"
        else:
            target_msg["content"] = f"[图片分析结果]\n{answer}"
    else:
        target_msg["content"] = _vision_fallback_text(user_text)


def _refill_messages_after_compression(messages, manager, session):
    """上下文超限强制压缩后，原地用压缩后的 DB 历史重建请求消息列表。

    历史必须走 get_history_for_llm：剔除刚被摘要排除的消息（水位之后才是
    活跃区）、挂载记忆摘要并修复 tool_call 配对。此前误用全量 get_history
    ——刚被压缩排除的消息被原样塞回，重试载荷不降反升，兜底路径必然再次
    超限并报出误导性的「压缩后仍超窗」错误，且记忆摘要挂载整条丢失。
    首部系统提示词（稳定前缀 + 会话内注入块）每轮现组、不落库，从原
    列表完整保留并前置；DB 中旧格式遗留的 system 上下文块与
    prepare_turn_messages 的清理口径一致，重读时整体丢弃。
    """
    system_blocks = [
        m for m in messages if isinstance(m, dict) and m.get("role") == "system"
    ]
    refreshed = manager.get_history_for_llm(session)
    refreshed = [
        m for m in refreshed if not (isinstance(m, dict) and m.get("role") == "system")
    ]
    messages[:] = system_blocks + refreshed

