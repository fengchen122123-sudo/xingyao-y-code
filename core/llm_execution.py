from __future__ import annotations

import hashlib
import json
import logging
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
    multi_edit,
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
    if isinstance(tool_call, dict):
        return str(((tool_call.get("function") or {}).get("name")) or "")
    function = getattr(tool_call, "function", None)
    return str(getattr(function, "name", "") or "")


def _persisted_tool_calls(tool_calls):
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
    if function_name in ("read_file", "write_file", "edit_file", "multi_edit", "get_outline"):
        preview = arguments.get("file_path", "")
    elif function_name in ("find_definition", "find_references"):
        preview = arguments.get("symbol", "")
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
    ok = _tool_result_ok(function_name, result)
    return summarize_tool_finished(function_name, arguments, result, ok)


# tool_finished 事件里携带的完整工具结果上限（字符数）：
# GUI 需要展开查看原始输出，但不能让单条事件把 IPC 通道撑爆。
TOOL_RESULT_EVENT_MAX_CHARS = 4000

PROMOTED_REASONING_PREVIEW_CHARS = 600

SLOW_STEP_ABORT_SECONDS = 60.0
SLOW_STEP_MAX_NUDGES = 1

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
CLAIM_NEGATION_PREFIXES = ("未", "没", "无", "尚未", "不能", "是否", "并非", "不算")
# 验证声明护栏允许的提醒次数上限：达到上限后不再介入（避免与模型反复声明打转）。
VERIFY_CLAIM_MAX_NUDGES = 1
# 命中时追加的提醒文案（模块级常量便于测试引用，避免文案漂移）。
VERIFY_CLAIM_NUDGE_TEXT = (
    "你声称已完成验证，但本回合没有执行任何命令。"
    "请立即用工具真实运行验证并贴出原始输出；若未验证，请如实说明未验证。"
)
COMMAND_TOOL_NAME_MARKERS = (
    "execute_command",
    "run_command",
    "shell",
    "bash",
    "terminal",
    "exec",
)


def _tool_call_activity_kinds(tool_calls):
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


# 成功完成的这些角色子代理视为「真实执行了命令」：test 跑测试、operate 跑技能
# 脚本与内网取证。计划门禁下主 Agent 不能自己调 execute_command，这两类角色是
# 主要执行者；只认 test 会让 operate 跑出来的结果被误判成「未执行命令」。
_SUBAGENT_COMMAND_EVIDENCE_TYPES = frozenset({"test", "operate"})

_SUBAGENT_STATUS_COMPLETED = "completed"


def _subagent_counts_as_command_evidence(subagent_type, result_content):
    """子代理结果能否作为「本回合真实执行过命令」的证据。

    只认命令型角色（test/operate）且结果为 status=completed；解析失败、
    非对象、非 completed 一律不算，绝不把模糊内容当成通过证据。
    """
    role = str(subagent_type or "").strip().lower()
    if role not in _SUBAGENT_COMMAND_EVIDENCE_TYPES:
        return False
    try:
        payload = json.loads(str(result_content if result_content is not None else "{}"))
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("status") == _SUBAGENT_STATUS_COMPLETED


def _claims_verification_without_evidence(
    final_text, used_write_tools, used_command_tools
):
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
            if not any(neg in window for neg in CLAIM_NEGATION_PREFIXES) and following != "率":
                return True
            start = idx + 1
    return False


def _tool_result_text(result):
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
        if function_name == "run_subagent" and content.startswith((
            "子代理执行失败（",
            "子代理执行超时（",
            "子代理执行已取消（",
        )):
            return False
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
        stable_result = {"status": "completed", "result": content}
    return [
        "run_subagent",
        _canonical_arguments_signature("run_subagent", tool_call),
        stable_result,
    ]


def _step_progress_observation(records, tool_calls_by_id=None, session_id=None):
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
        if content == REPEAT_CALL_BLOCKED_TEXT:
            continue
        ok = _tool_result_ok(name, message)
        if ok and name in {"write_file", "edit_file", "multi_edit"}:
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
    computer_capability=None,
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
        multi_edit=_adapter("multi_edit", multi_edit),
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
        computer_capability=computer_capability,
        hook_runner=get_default_hook_runner(),
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

_TOOL_RESULT_MAX_CHARS = 24000
_TOOL_RESULT_LIMITS = {
    "execute_command": 16000,
    "fetch_url": 16000,
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
    hint = (
        "[当前主模型和副模型均不支持图片输入或图片分析失败，无法处理图片。"
        "请直接回复用户：无法分析图片，不要调用任何工具。]"
    )
    if user_text:
        return f"{user_text}\n\n{hint}"
    return hint


def _describe_images_with_secondary(messages, main_model_name, event_callback=None, cancel_event=None):
    if _supports_vision_llm_first(main_model_name):
        return

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
    system_blocks = [
        m for m in messages if isinstance(m, dict) and m.get("role") == "system"
    ]
    refreshed = manager.get_history_for_llm(session)
    refreshed = [
        m for m in refreshed if not (isinstance(m, dict) and m.get("role") == "system")
    ]
    messages[:] = system_blocks + refreshed


def _is_ephemeral_subagent_turn(persist, parent_session, cache_scope) -> bool:
    """Return True for non-persisted subagent turns that must not touch main session history."""
    return (
        not bool(persist)
        and not parent_session
        and str(cache_scope or "").startswith("subagent:")
    )


def _compact_ephemeral_messages_in_place(messages) -> bool:
    """Shrink an ephemeral subagent message list in memory while keeping tool pairing valid."""
    if not isinstance(messages, list) or len(messages) <= 2:
        return False

    from core.tool_result_maintenance import elide_tool_result, snip_tool_result

    def _total_chars(items) -> int:
        return sum(len(str(item)) for item in items)

    before_chars = _total_chars(messages)
    head_end = 0
    for idx, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            head_end = idx + 1
            break
    if head_end == 0 or head_end >= len(messages):
        return False

    # 保护最后一轮完整交互（含其 tool_calls 与对应的 tool 结果），避免截断当前推理现场
    tail_start = max(head_end, len(messages) - 4)
    while tail_start > head_end and isinstance(messages[tail_start], dict):
        if messages[tail_start].get("role") == "tool":
            tail_start -= 1
            continue
        break

    # Pass 1：精简保护区之外的旧 tool 结果文本
    for idx in range(head_end, tail_start):
        msg = messages[idx]
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= 800:
            continue
        elided = elide_tool_result(str(msg.get("name") or "tool"), content)
        if elided is not None:
            msg["content"] = elided
        else:
            msg["content"] = content[:600] + "\n...[历史工具输出已精简]..."

    if _total_chars(messages) <= int(before_chars * 0.75):
        return True

    # Pass 2：按完整轮次（assistant + 后续 tool 消息）丢弃最早的中间执行记录
    middle = messages[head_end:tail_start]
    if middle:
        units = []
        i = 0
        while i < len(middle):
            unit = [middle[i]]
            if isinstance(middle[i], dict) and middle[i].get("role") == "assistant":
                j = i + 1
                while (
                    j < len(middle)
                    and isinstance(middle[j], dict)
                    and middle[j].get("role") == "tool"
                ):
                    unit.append(middle[j])
                    j += 1
                i = j
            else:
                i += 1
            units.append(unit)
        # 优先仅保留最后 1 个中间单元（若不止 1 个单元，则丢掉较早单元并注入折叠提示）
        kept_units = units[-1:] if len(units) > 1 else []
        flattened_kept = [msg for unit in kept_units for msg in unit]
        notice = {
            "role": "user",
            "content": (
                "[系统提示：较早的中间工具调用记录已自动折叠以释放上下文窗口。"
                "请直接基于保留的最近结果收敛并输出最终结论，避免继续大规模读取。]"
            ),
        }
        messages[:] = (
            messages[:head_end]
            + [notice]
            + flattened_kept
            + messages[tail_start:]
        )

    if _total_chars(messages) >= before_chars:
        # Pass 3：若仅靠最新一轮的超大工具输出就撑爆上下文，用 snip 保留首尾关键信息
        for msg in messages[head_end:]:
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or len(content) <= 4000:
                continue
            snipped = snip_tool_result(str(msg.get("name") or "tool"), content)
            if snipped is not None and len(snipped) < len(content):
                msg["content"] = snipped
            elif len(content) > 6000:
                msg["content"] = (
                    content[:3000]
                    + "\n...[单步工具输出过长，中段已精简以适配上下文窗口]...\n"
                    + content[-2000:]
                )

    return _total_chars(messages) < before_chars
