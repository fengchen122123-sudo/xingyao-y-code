"""Interactive tool permission authorization and terminal prompts."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import TimeoutError as _FutureTimeoutError
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import core.app_context as app_context
from core import permission_grants, permission_store
from core.command_policy import (
    _session_rule_is_exact,
    classify_command_risk,
    command_matches_session_rule,
    parse_manual_command_rule,
    session_rule_for_command,
)
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
# safety 场景（JEV 决策）薄包装层：只做「allow → ask」的加严，纯函数判定不受污染
from core.permission_safety_gate import apply_decision_to_permission

# 工具结果中的错误哨兵前缀
TOOL_ERROR_PREFIX = "🚨"
ASK_USER_ERROR_PREFIX = "无法提问："
TOOL_INTERNAL_ERROR_PREFIX = "Tool execution error:"
PERMISSION_DENIED_MESSAGE = f"{TOOL_ERROR_PREFIX} 用户拒绝了执行该操作。"
PERMISSION_CANCELLED_MESSAGE = f"{TOOL_ERROR_PREFIX} 操作已取消。"
PLAN_MODE_DENIED_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 当前处于计划模式：只允许读取信息、执行明确的只读查询和制定计划，"
    "不允许执行修改或删除操作。请改用 plan_write 把操作步骤写入计划，"
    "并提示用户切换到「变更前确认」「自动编辑」或「完全访问」模式后再执行。"
)
PERMISSION_SYSTEM_ERROR_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 权限询问系统异常，操作未能完成。"
    f"这不是用户拒绝，请稍后重试或检查权限组件状态。"
)
PATH_VALIDATION_ERROR_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 无法验证路径是否位于当前工作区，已拒绝执行。"
)


class PermissionSystemError(Exception):
    """权限询问流程自身发生异常（如 UI 事件丢失、broker 状态不一致）。"""
    pass


@dataclass(frozen=True)
class AuthorizationResult:
    allowed: bool
    reason: str
    sandbox_bypass: bool = False
    cancelled: bool = False


def _notify_event(event_callback, kind, payload):
    if event_callback is not None:
        event_callback(kind, payload)


def _agent_display_name() -> str:
    try:
        from core.persona_settings import get_agent_display_name

        return get_agent_display_name()
    except Exception:
        return "星瑶"


# 兼容旧调用方的动态集合视图
session_allowed_commands = permission_grants.task_set_view("commands")
session_allowed_write_files = permission_grants.task_set_view("write_files")
session_allowed_edit_files = permission_grants.task_set_view("edit_files")
session_allowed_exact_commands = permission_grants.task_set_view("exact_commands")
_SESSION_GRANTS = permission_grants._TASK_GRANTS
_last_tracked_session = None
_raw_permission_lock = threading.Lock()
_session_whitelist_lock = permission_grants._GRANTS_LOCK
_gui_permission_callback = None



def clear_session_grants(session_id: str) -> None:
    """清空指定任务的授权桶（任务删除时调用）。"""
    permission_grants.clear_task(session_id)


def _grant_command_add(prefix: tuple[str, ...]) -> None:
    permission_grants.grant_command_rule(prefix)


def _grant_write_add(canonical_path: str) -> None:
    permission_grants.grant_file("write", canonical_path)


def _grant_edit_add(canonical_path: str) -> None:
    permission_grants.grant_file("edit", canonical_path)


def _grant_command_hit(command: str) -> bool:
    return permission_grants.matches_command(command)


def _grant_exact_command_add(command: str) -> None:
    permission_grants.grant_exact_command(command)


def _grant_exact_command_hit(command: str) -> bool:
    return permission_grants.matches_exact_command(command)


def _grant_write_hit(canonical_path: str) -> bool:
    return permission_grants.matches_file("write", canonical_path)


def _grant_edit_hit(canonical_path: str) -> bool:
    return permission_grants.matches_file("edit", canonical_path)


def _canonical_permission_path(value):
    try:
        from core.tools import _resolve_against_workdir

        return os.path.normcase(str(_resolve_against_workdir(value)))
    except (OSError, ValueError, TypeError):
        return ""


_plan_turn_lock = threading.Lock()
_plan_turn_started_at = {}

_PLAN_BYPASS_ERROR_PREFIX = "计划已创建：工具"
_PLAN_ROOT_CONTROL_TOOLS = frozenset({
    "plan_write",
    "plan_update",
    "plan_read",
    "plan_retry",
    "run_subagent",
    "ask_user",
})
_PLAN_READ_ONLY_TOOLS = frozenset({
    "read_file",
    "list_directory",
    "search_code",
    "find_definition",
    "glob_files",
    "fetch_url",
    "search_news",
    "get_weather",
    "manage_task",
})
_PLAN_BOUND_ORCHESTRATORS = frozenset({"run_subagent", "ask_user"})


def _mark_plan_turn(turn_id):
    if not turn_id:
        return
    now = time.monotonic()
    with _plan_turn_lock:
        _plan_turn_started_at[str(turn_id)] = now
        cutoff = now - 3600
        for stale_id, started_at in list(_plan_turn_started_at.items()):
            if started_at < cutoff:
                _plan_turn_started_at.pop(stale_id, None)


def _is_plan_turn(turn_id):
    if not turn_id:
        return False
    with _plan_turn_lock:
        return str(turn_id) in _plan_turn_started_at


def _has_pending_plan(session_id):
    if not session_id:
        return False
    from core.plan import get_default_plan_manager

    plan = get_default_plan_manager()
    if plan.session_id != str(session_id):
        return False
    return any(item.get("status") != "completed" for item in plan.snapshot())


def check_session_reset():
    global _last_tracked_session
    _last_tracked_session = permission_grants.current_task_id()


def _session_prefix_for_command(cmd):
    """兼容历史扩展名称，委托给结构化命令授权策略。"""
    return session_rule_for_command(cmd)


def _command_matches_prefix(cmd, prefix):
    """兼容历史扩展名称，使用完整 token 元素匹配。"""
    return command_matches_session_rule(cmd, prefix)


def _format_session_command_rule(rule):
    command = subprocess.list2cmdline(rule)
    if _session_rule_is_exact(rule):
        return f"精确规则：{command}"
    return f"子命令规则：{command}（允许后续参数）"


def safe_print(formatted_text):
    """线程安全地在 CLI 终端输出格式化文本，避免破坏屏幕布局"""
    from prompt_toolkit import print_formatted_text
    _app = app_context.get_app_instance()
    if _app and _app.loop:
        _app.loop.call_soon_threadsafe(lambda: print_formatted_text(formatted_text))
    else:
        try:
            print_formatted_text(formatted_text)
        except Exception:
            pass


def ask_in_raw_terminal(function_name, arguments, *, options=()):
    """
    纯文本原始终端兜底：只在确实没有活动 CLI App 时才被调用（例如管道重定向或早期初始化时）。
    不使用任何 ANSI 转义序列或 msvcrt 菜单，直接用 print/input 避免污染 CLI 屏幕。
    """
    mode_display = {
        "confirm": "每次确认",
        "accept_edits": "自动编辑",
        "plan": "计划确认",
        "full": "完全访问",
    }
    mode_tip = mode_display.get(app_context.get_permission_mode(), "确认")
    option_keys = {str(option.key) for option in options}
    allow_task = PermissionOptionKey.ALLOW_TASK.value in option_keys
    allow_task_sandbox = (
        PermissionOptionKey.ALLOW_TASK_SANDBOX.value in option_keys
    )

    print("")
    if function_name == "write_file":
        file_path = arguments.get("file_path", "未知文件")
        content = arguments.get("content", "")
        print(f"[写入文件] {_agent_display_name()}申请写入文件: {file_path}")
        lines = content.splitlines()
        if lines:
            preview = "\n".join(lines[:5])
            if len(lines) > 5:
                preview += f"\n   ... (共 {len(lines)} 行)"
            print(f"   [预览]:\n   {preview}")
    elif function_name == "edit_file":
        file_path = arguments.get("file_path", "未知文件")
        target = arguments.get("target_content", "")
        repl = arguments.get("replacement_content", "")
        print(f"[修改文件] {_agent_display_name()}申请修改文件: {file_path}")
        print(f"   [-] 计划删除: {target.strip()[:100]}")
        print(f"   [+] 计划替换: {repl.strip()[:100]}")
    elif function_name == "execute_command":
        cmd = arguments.get("command", "")
        shell_id = str(arguments.get("shell_id") or "").strip()
        bg = arguments.get("run_in_background", False)
        bg_str = " (后台运行)" if bg else ""
        print(f"[执行命令] {_agent_display_name()}申请执行命令{bg_str}: {cmd}")
        if shell_id:
            print(f"   [解释器显式覆盖]: {shell_id}")

    try:
        shell_override = (
            function_name == "execute_command"
            and bool(str(arguments.get("shell_id") or "").strip())
        )
        if shell_override:
            print("请输入 [y]es 同意本次, [n]o 拒绝（解释器覆盖不支持加入白名单）")
            res = input(f"批准该操作？(y/n, 模式:{mode_tip}, 默认 y): ").strip().lower()
        elif function_name == "sandbox_outside_access" and allow_task_sandbox:
            print("请输入 [y]es 同意本次, [n]o 拒绝, [a]lways 本任务内放行沙盒外访问")
            res = input(f"批准该操作？(y/n/a, 模式:{mode_tip}, 默认 y): ").strip().lower()
        elif function_name == "sandbox_outside_access":
            print("请输入 [y]es 同意本次, [n]o 拒绝")
            res = input(f"批准该操作？(y/n, 模式:{mode_tip}, 默认 y): ").strip().lower()
        elif allow_task:
            print("请输入 [y]es 同意本次, [n]o 拒绝, [a]lways 允许此类操作 (仅当前任务放行)")
            res = input(f"批准该操作？(y/n/a, 模式:{mode_tip}, 默认 y): ").strip().lower()
        else:
            print("请输入 [y]es 同意本次, [n]o 拒绝")
            res = input(f"批准该操作？(y/n, 模式:{mode_tip}, 默认 y): ").strip().lower()

        if shell_override:
            return res in ("", "y", "yes")

        if res in ("n", "no"):
            return False

        if res in ("a", "always", "y*"):
            if not (allow_task or allow_task_sandbox):
                return False
            if function_name == "execute_command":
                cmd = arguments.get("command", "")
                prefix = _session_prefix_for_command(cmd)
                if prefix is None:
                    print("[内联执行命令不做任务前缀放行，仅本次通过]")
                else:
                    _grant_command_add(prefix)
                    print(f"[当前任务已放行] {_format_session_command_rule(prefix)}")
            elif function_name == "write_file":
                file_path = arguments.get("file_path", "")
                if file_path:
                    _grant_write_add(_canonical_permission_path(file_path))
                    print(f"[当前任务已放行写入]：{file_path}")
            elif function_name == "edit_file":
                file_path = arguments.get("file_path", "")
                if file_path:
                    _grant_edit_add(_canonical_permission_path(file_path))
                    print(f"[当前任务已放行修改]：{file_path}")
            elif function_name == "sandbox_outside_access":
                from core.tools import set_session_sandbox_bypass

                set_session_sandbox_bypass(True)
                print("[当前任务已放行沙盒外访问]")
            return True

        return res in ("", "y", "yes")
    except (KeyboardInterrupt, EOFError):
        print("\n[操作已取消]")
        return False


_PLAN_INTERNAL_PERMISSION_TOOLS = frozenset({
    "plan_write",
    "plan_read",
    "plan_update",
    "plan_retry",
    "plan_finish",
    "ask_user",
})
_KNOWN_QUERY_TOOLS = frozenset({
    "read_file",
    "list_directory",
    "search_code",
    "find_definition",
    "glob_files",
    "fetch_url",
    "search_news",
    "get_weather",
})
_SENSITIVE_TOOL_NAMES = frozenset({
    "delete_file",
    "remove_file",
    "kill_process",
    "git_commit",
    "git_push",
})


def _permission_risk(function_name, arguments, tool_spec, path_access):
    if function_name == "sandbox_outside_access":
        return PermissionRisk.QUERY
    if function_name == "execute_command":
        if bool(str(arguments.get("shell_id") or "").strip()):
            return PermissionRisk.SENSITIVE_MUTATION
        return classify_command_risk(str(arguments.get("command", "")))
    if function_name == "manage_task":
        action = str(arguments.get("action", "")).strip().lower()
        return (
            PermissionRisk.SENSITIVE_MUTATION
            if action in {"kill", "stop", "terminate", "cancel"}
            else PermissionRisk.QUERY
        )
    if path_access == "credential_write" or function_name in _SENSITIVE_TOOL_NAMES:
        return PermissionRisk.SENSITIVE_MUTATION
    if function_name in _PLAN_INTERNAL_PERMISSION_TOOLS:
        return PermissionRisk.NORMAL_MUTATION
    if function_name == "run_subagent":
        return PermissionRisk.QUERY
    if function_name in _KNOWN_QUERY_TOOLS or bool(getattr(tool_spec, "read_only", False)):
        return PermissionRisk.QUERY
    if bool(getattr(tool_spec, "mutates_files", False)):
        return PermissionRisk.NORMAL_MUTATION
    if bool(getattr(tool_spec, "requires_permission", True)):
        return PermissionRisk.NORMAL_MUTATION
    return PermissionRisk.QUERY


def _permission_rule_facts(function_name, arguments):
    task_hit = False
    project_hit = False
    task_grant = None
    project_rule = None
    shell_override = (
        function_name == "execute_command"
        and bool(str(arguments.get("shell_id") or "").strip())
    )
    rules = permission_store.project_rules()

    if function_name == "execute_command":
        command = str(arguments.get("command", ""))
        if not shell_override:
            task_hit = permission_grants.matches_command(command)
            project_hit = any(
                command_matches_session_rule(command, prefix)
                for prefix in rules["commands"]
            )
            task_grant = ("command", command)
            try:
                parse_manual_command_rule(command)
            except ValueError:
                project_rule = None
            else:
                project_rule = ("command", command)
    elif function_name in {"write_file", "edit_file"}:
        kind = "write" if function_name == "write_file" else "edit"
        field = "write_files" if kind == "write" else "edit_files"
        canonical = _canonical_permission_path(arguments.get("file_path", ""))
        if canonical:
            task_hit = permission_grants.matches_file(kind, canonical)
            project_hit = canonical in rules[field]
            task_grant = (kind, canonical)
            project_rule = (kind, canonical)
    return task_hit, project_hit, task_grant, project_rule


def _permission_options(*, sandbox_request, credential_write, task_grant, project_rule):
    if credential_write:
        return (
            PermissionOption(PermissionOptionKey.ALLOW_ONCE.value, "允许本次"),
            PermissionOption(PermissionOptionKey.DENY.value, "拒绝"),
        )
    if sandbox_request:
        return (
            PermissionOption(PermissionOptionKey.ALLOW_ONCE.value, "允许本次"),
            PermissionOption(PermissionOptionKey.DENY.value, "拒绝"),
            PermissionOption(
                PermissionOptionKey.ALLOW_TASK_SANDBOX.value,
                "本任务内放行沙盒外访问",
            ),
        )
    options = [
        PermissionOption(PermissionOptionKey.ALLOW_ONCE.value, "允许本次"),
        PermissionOption(PermissionOptionKey.DENY.value, "拒绝"),
    ]
    if task_grant is not None:
        options.append(
            PermissionOption(
                PermissionOptionKey.ALLOW_TASK.value,
                "本任务同类操作放行",
            )
        )
    if project_rule is not None:
        options.append(
            PermissionOption(
                PermissionOptionKey.ALLOW_PROJECT.value,
                "加入项目白名单",
            )
        )
    return tuple(options)


def _get_active_tool_registry():
    """llm-first 解析活跃工具注册表。

    core.llm.tool_registry 是全局唯一活跃实例（插件/MCP 动态注册都落在它
    上面）；core.tool_registry 模块没有 snake_case 单例导出，直连导入恒
    ImportError，不得作为兜底来源。取不到时返回 None，由调用方走兜底 spec。
    """
    try:
        import core.llm as _llm
        return getattr(_llm, "tool_registry", None)
    except Exception:
        return None


def _llm_first(name, fallback):
    """llm-first 解析入口层绑定。

    core.llm 保留兼容入口，测试/外部调用方 patch core.llm.<name> 必须生效；
    解析失败回落本模块实现。
    """
    try:
        import core.llm as _llm
        fn = getattr(_llm, name, None)
        if fn is not None:
            return fn
    except Exception:
        pass
    return fallback


def _get_permission_broker():
    """llm-first 解析权限询问代理（测试/外部 patch core.llm.permission_broker 生效）。"""
    return _llm_first("permission_broker", permission_broker)


def _wait_permission_option(
    function_name,
    arguments,
    options,
    *,
    event_callback,
    permission_owner,
    parent_turn_id,
    permission_source,
    cancel_event,
):
    _notify_event(event_callback, "permission_requested", {
        "function_name": function_name,
        "arguments": dict(arguments),
        "source": permission_source,
    })

    broker = _get_permission_broker()
    if not broker.has_prompt_handlers():
        gui_callback = app_context.get_permission_callback()
        if gui_callback is not None:
            return (
                PermissionOptionKey.ALLOW_ONCE.value
                if gui_callback(function_name, arguments)
                else PermissionOptionKey.DENY.value
            )
        app = app_context.get_app_instance()
        if (not app or not app.loop) and app_context.get_ui_flavor() != "gui":
            with _raw_permission_lock:
                return (
                    PermissionOptionKey.ALLOW_ONCE.value
                    if ask_in_raw_terminal(function_name, arguments, options=options)
                    else PermissionOptionKey.DENY.value
                )

    owner_id = str(permission_owner or parent_turn_id or threading.current_thread().name)
    turn_id = str(parent_turn_id or owner_id)
    request = broker.enqueue(
        function_name,
        arguments,
        owner_id,
        turn_id,
        permission_source,
        options=options,
    )
    try:
        while True:
            if request.future.done():
                choice_index = request.future.result()
                break
            if cancel_event is not None and cancel_event.is_set():
                broker.cancel_owner(owner_id)
                choice_index = CANCELLED_RESULT
                break
            try:
                choice_index = request.future.result(timeout=0.1)
                break
            except (TimeoutError, _FutureTimeoutError):
                continue
        if choice_index == CANCELLED_RESULT:
            return "cancelled"
        try:
            return request.option_keys[int(choice_index)]
        except (IndexError, TypeError, ValueError) as exc:
            raise PermissionSystemError("权限响应选项无效") from exc
    finally:
        if not request.future.done():
            broker.cancel_owner(owner_id)


def authorize_tool_call(
    function_name,
    arguments,
    *,
    tool_spec,
    outside_workspace=False,
    event_callback=None,
    permission_owner=None,
    parent_turn_id=None,
    permission_source="主代理",
    cancel_event=None,
):
    check_session_reset()
    sandbox_access_request = function_name == "sandbox_outside_access"
    effective_function_name = function_name
    effective_arguments = arguments
    if sandbox_access_request:
        effective_function_name = str(arguments.get("operation") or "").strip()
        if not effective_function_name:
            raise PermissionSystemError("沙箱外访问缺少有效的原始操作类型")
        path_value = arguments.get("path")
        effective_arguments = dict(arguments)
        if effective_function_name in {"read_file", "write_file", "edit_file"}:
            effective_arguments["file_path"] = path_value
        elif effective_function_name in {"list_directory", "directory_tree", "search_code", "find_definition", "glob_files"}:
            effective_arguments["dir_path"] = path_value
        else:
            effective_arguments.setdefault("path", path_value)
        try:
            registry = _get_active_tool_registry()
            effective_spec = registry.get(effective_function_name) if registry is not None else None
            if effective_spec is not None:
                tool_spec = effective_spec
        except Exception:
            pass
    else:
        path_value = arguments.get("file_path") or arguments.get("dir_path") or arguments.get("path")

    outside_hint = bool(outside_workspace or sandbox_access_request)
    path_access = "outside" if outside_hint else "inside"
    if path_value:
        try:
            from core.harness.tool_adapters import classify_path_access

            path_access = classify_path_access(
                effective_function_name,
                path_value,
                is_outside_workspace=lambda _path: outside_hint,
            )
        except Exception as exc:
            raise PermissionSystemError(f"路径权限分类失败: {exc}") from exc
        if outside_hint and path_access == "inside":
            path_access = "outside"

    risk = _permission_risk(
        effective_function_name, effective_arguments, tool_spec, path_access
    )
    plan_internal = effective_function_name in _PLAN_INTERNAL_PERMISSION_TOOLS
    task_hit, project_hit, task_grant, project_rule = _permission_rule_facts(
        effective_function_name,
        effective_arguments,
    )

    outside = path_access in {"outside", "credential_write"}
    try:
        from core.model_manager import load_config

        sandbox_restricted = not bool(
            load_config().get("allow_outside_workspace", False)
        )
    except Exception as exc:
        raise PermissionSystemError(f"读取沙盒配置失败: {exc}") from exc
    if path_access == "credential_write":
        sandbox_restricted = True
    from core.tools import is_session_sandbox_bypass_enabled

    credential_write = path_access == "credential_write"
    task_sandbox_bypass = (
        False if credential_write else is_session_sandbox_bypass_enabled()
    )
    facts = PermissionFacts(
        mode=app_context.get_permission_mode(),
        risk=risk,
        plan_internal=plan_internal,
        outside_workspace=outside,
        sandbox_restricted=sandbox_restricted,
        task_sandbox_bypass=task_sandbox_bypass,
        task_grant_hit=task_hit,
        project_rule_hit=project_hit,
    )
    decision = decide_permission(facts)
    # JEV safety 场景接入：高风险操作再问一次「直接执行是否安全」，
    # **唯一允许的变化是 allow → ask**（只能加严，不能放宽）。
    # 纯函数 decide_permission 保持无网络 IO，决策叠加在薄包装层完成；
    # JEV 关闭 / 影子 / 失败 / 低风险时该调用是纯本地空转，判定完全不变。
    decision = apply_decision_to_permission(
        facts,
        decision,
        command=(
            str(effective_arguments.get("command", ""))
            if effective_function_name == "execute_command"
            else effective_function_name
        ),
    )

    if decision.action is PermissionAction.ALLOW:
        _notify_event(event_callback, "permission_resolved", {
            "approved": True,
            "automatic": True,
            "reason": decision.reason,
        })
        return AuthorizationResult(True, decision.reason, sandbox_bypass=outside)
    if decision.action is PermissionAction.DENY:
        return AuthorizationResult(False, decision.reason)

    sandbox_request = decision.reason == "sandbox_boundary_requires_confirmation"
    options = _permission_options(
        sandbox_request=sandbox_request,
        credential_write=credential_write,
        task_grant=task_grant,
        project_rule=project_rule,
    )
    prompt_as_sandbox = sandbox_access_request or sandbox_request
    try:
        option_key = _llm_first(
            "_wait_permission_option", _wait_permission_option
        )(
            "sandbox_outside_access" if prompt_as_sandbox else function_name,
            (
                {
                    "path": str(path_value or ""),
                    "operation": effective_function_name,
                }
                if prompt_as_sandbox
                else arguments
            ),
            options,
            event_callback=event_callback,
            permission_owner=permission_owner,
            parent_turn_id=parent_turn_id,
            permission_source=permission_source,
            cancel_event=cancel_event,
        )
        if option_key == "cancelled":
            _notify_event(event_callback, "permission_resolved", {
                "approved": False,
                "cancelled": True,
            })
            return AuthorizationResult(False, "cancelled", cancelled=True)
        if option_key == PermissionOptionKey.DENY.value:
            _notify_event(event_callback, "permission_resolved", {"approved": False})
            return AuthorizationResult(False, "user_denied")
        if option_key == PermissionOptionKey.ALLOW_TASK.value:
            if task_grant is None:
                raise PermissionSystemError("当前操作不支持任务级授权")
            kind, value = task_grant
            if kind == "command":
                permission_grants.grant_command(value)
            else:
                permission_grants.grant_file(kind, value)
        elif option_key == PermissionOptionKey.ALLOW_PROJECT.value:
            if project_rule is None:
                raise PermissionSystemError("当前操作不能加入项目白名单")
            kind, value = project_rule
            mutation = permission_store.add_rule(kind, value)
            if mutation.status not in {"added", "exists"}:
                raise PermissionSystemError(
                    f"项目白名单写入失败: {mutation.status}"
                )
        elif option_key == PermissionOptionKey.ALLOW_TASK_SANDBOX.value:
            from core.tools import set_session_sandbox_bypass

            set_session_sandbox_bypass(True)
        elif option_key != PermissionOptionKey.ALLOW_ONCE.value:
            raise PermissionSystemError(f"未知权限选项: {option_key}")
    except PermissionSystemError:
        raise
    except Exception as exc:
        raise PermissionSystemError(str(exc)) from exc

    _notify_event(event_callback, "permission_resolved", {
        "approved": True,
        "option_key": option_key,
    })
    return AuthorizationResult(True, option_key, sandbox_bypass=outside)


def check_permission_and_ask(
    function_name,
    arguments,
    event_callback=None,
    permission_owner=None,
    parent_turn_id=None,
    permission_source="主代理",
    cancel_event=None,
    *,
    tool_spec=None,
    outside_workspace=None,
    structured=False,
):
    if tool_spec is None:
        fetched_spec = None
        registry = _get_active_tool_registry()
        if registry is not None:
            try:
                fetched_spec = registry.get(function_name)
            except Exception:
                fetched_spec = None
        tool_spec = fetched_spec if fetched_spec is not None else SimpleNamespace(
            name=function_name,
            read_only=function_name in _KNOWN_QUERY_TOOLS,
            mutates_files=function_name in {"write_file", "edit_file"},
            requires_permission=True,
        )
    outside = outside_workspace
    if outside is None:
        outside = function_name == "sandbox_outside_access"
        if function_name in {"read_file", "write_file", "edit_file"}:
            try:
                from core.tools import is_outside_workspace as _fallback_check
                outside = _llm_first("is_outside_workspace", _fallback_check)(
                    arguments.get("file_path", "")
                )
            except Exception:
                outside = False
    result = _llm_first("authorize_tool_call", authorize_tool_call)(
        function_name,
        arguments,
        tool_spec=tool_spec,
        outside_workspace=bool(outside),
        event_callback=event_callback,
        permission_owner=permission_owner,
        parent_turn_id=parent_turn_id,
        permission_source=permission_source,
        cancel_event=cancel_event,
    )
    return result if structured else result.allowed


def _pipeline_permission_checker(function_name, arguments, **context):
    return check_permission_and_ask(
        function_name,
        arguments,
        structured=True,
        **context,
    )
