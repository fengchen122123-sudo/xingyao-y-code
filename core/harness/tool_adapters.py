"""Fixed adapters for the model-visible tool execution boundary."""

from __future__ import annotations

import inspect
import json
import logging
import os
from copy import copy
from contextlib import nullcontext
from typing import Callable

from core.harness.tool_contracts import ResolvedTool
from core.tool_protocol import ToolResult
from core.tools import (
    edit_file as _edit_file,
    is_outside_workspace as _is_outside_workspace,
    manage_task as _manage_task,
    read_file as _read_file,
    run_subagent as _run_subagent,
    sandbox_bypass as _sandbox_bypass,
    write_file as _write_file,
)


logger = logging.getLogger(__name__)

TOOL_ERROR_PREFIX = "🚨"
PERMISSION_CANCELLED_MESSAGE = f"{TOOL_ERROR_PREFIX} 操作已取消。"
PERMISSION_SYSTEM_ERROR_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 权限询问系统异常，操作未能完成。"
    f"这不是用户拒绝，请稍后重试或检查权限组件状态。"
)
PATH_VALIDATION_ERROR_MESSAGE = (
    f"{TOOL_ERROR_PREFIX} 无法验证路径是否位于当前工作区，已拒绝执行。"
)


def _as_tool_result(value) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    return ToolResult(content=str(value) if value is not None else "")


def _read_text_with_encoding_fallback(file_path):
    with open(file_path, "rb") as handle:
        raw = handle.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def _notify_event(event_callback, kind, payload):
    if event_callback is not None:
        event_callback(kind, payload)


def _safe_print(formatted_text):
    from prompt_toolkit import print_formatted_text
    import core.app_context as app_context

    app = app_context.get_app_instance()
    if app and app.loop:
        app.loop.call_soon_threadsafe(lambda: print_formatted_text(formatted_text))
        return
    try:
        print_formatted_text(formatted_text)
    except Exception:
        pass


def _show_file_diff(
    file_path,
    old_content,
    new_content,
    event_callback,
    panel_title,
    truncated_note,
    tool_call_id="",
):
    """Render the existing write/edit diff without changing its UI payload."""
    try:
        from core.diff_viewer import generate_diff, render_diff_table
        from prompt_toolkit.formatted_text import ANSI
        from rich.console import Console
        from rich.panel import Panel

        diff_text = generate_diff(old_content, new_content, file_path)
        if not diff_text:
            return
        lines = diff_text.split("\n")
        max_lines = 25
        display_text = (
            "\n".join(lines[:max_lines])
            + f"\n... (省略 {len(lines) - max_lines} 行，{truncated_note})"
            if len(lines) > max_lines
            else diff_text
        )
        panel = Panel(
            render_diff_table(display_text),
            title=panel_title,
            border_style="green",
            expand=False,
        )
        rich_console = Console(force_terminal=True, color_system="truecolor")
        with rich_console.capture() as capture:
            rich_console.print(panel)
        diff_output = capture.get()
        if event_callback is None:
            _safe_print(ANSI(diff_output))
        else:
            _notify_event(event_callback, "diff_ready", {
                "tool_call_id": str(tool_call_id or ""),
                "file_path": file_path,
                "text": diff_output,
                "diff_text": display_text,
            })
    except Exception as diff_error:
        logger.warning("渲染文件 diff 失败 (%s): %s", file_path, diff_error)


def _get_imom_active_publish_root():
    """Read the active imom publish root using the established on-disk format."""
    try:
        store_file = os.path.join(os.path.expanduser("~"), ".ycode", "imom_projects.json")
        with open(store_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        active = data.get("active")
        projects = data.get("projects", {})
        if active and active in projects:
            return projects[active].get("publish_root", "")
        if len(projects) == 1:
            return next(iter(projects.values())).get("publish_root", "")
    except Exception:
        pass
    return ""


def _sandbox_outside_guard(
    function_name,
    path_value,
    event_callback,
    permission_owner,
    parent_turn_id,
    permission_source,
    cancel_event,
    *,
    permission_checker,
    is_outside_workspace=_is_outside_workspace,
    sandbox_bypass=_sandbox_bypass,
    permission_error_type=Exception,
):
    """Return the existing one-shot bypass context or a model-visible denial."""
    if not path_value:
        return None, None
    try:
        outside = is_outside_workspace(path_value)
    except Exception:
        return None, PATH_VALIDATION_ERROR_MESSAGE
    if not outside:
        return None, None

    from core import tools as tools_module

    # ~/.ycode 整个数据目录读取豁免（用户要求：不管什么权限模式/沙盒状态，
    # agent 都能读取自家运行数据）。凭据文件（config.json / mcp.json，含 .bak）
    # 例外：读取同样放行（诊断配置问题的常见需求），但写入必须逐次询问——
    # 改写 mcp.json 可注入恶意 MCP server，改坏 config.json 会让应用不可用。
    credential_write = False
    try:
        from core.memory_kairos import is_ycode_data_path

        if is_ycode_data_path(path_value):
            basename = os.path.basename(str(path_value)).lower()
            if basename not in {"config.json", "config.json.bak", "mcp.json", "mcp.json.bak"}:
                return sandbox_bypass(), None
            if function_name not in {"write_file", "edit_file"}:
                return sandbox_bypass(), None
            credential_write = True
    except Exception:
        pass
    # 沙盒开关（/sandbox on / GUI 沙盒解除，allow_outside_workspace=True）打开时：
    # 项目外路径读写直接放行，不再逐次询问（用户选定语义）。
    # 凭据写入不受开关影响，始终进入下方询问流程。
    if not credential_write:
        try:
            from core.model_manager import load_config

            if bool(load_config().get("allow_outside_workspace", False)):
                return sandbox_bypass(), None
        except Exception:
            pass
    if function_name == "read_file":
        try:
            from core.skills import is_trusted_read_path

            if is_trusted_read_path(str(path_value)):
                return None, None
        except Exception:
            pass
    if getattr(tools_module._sandbox_tls, "depth", 0) > 0:
        return None, None

    try:
        approved = permission_checker(
            "sandbox_outside_access",
            {"path": str(path_value), "operation": function_name},
            event_callback=event_callback,
            permission_owner=permission_owner,
            parent_turn_id=parent_turn_id,
            permission_source=permission_source,
            cancel_event=cancel_event,
        )
    except permission_error_type:
        return None, PERMISSION_SYSTEM_ERROR_MESSAGE
    if approved:
        return sandbox_bypass(), None
    if cancel_event is not None and cancel_event.is_set():
        return None, PERMISSION_CANCELLED_MESSAGE
    return None, "🚨 用户拒绝了访问沙盒外路径的请求。沙盒外路径访问需要用户确认（可选择本会话内放行，或用 /sandbox on 长期开放）。"


class ToolAdapterSet:
    """Named adapters for special tools and strict registry/MCP execution."""

    def __init__(
        self,
        *,
        read_file=_read_file,
        write_file=_write_file,
        edit_file=_edit_file,
        run_subagent=_run_subagent,
        manage_task=_manage_task,
        show_file_diff=_show_file_diff,
        get_imom_active_publish_root=_get_imom_active_publish_root,
        sandbox_guard=None,
        sandbox_bypass=_sandbox_bypass,
        read_text=_read_text_with_encoding_fallback,
        notify_event=_notify_event,
        mark_plan_turn=lambda _turn_id: None,
        registry_execute_override=None,
        is_outside_workspace=_is_outside_workspace,
        safe_print=_safe_print,
        tool_call_id="",
        permission_error_type=(),
    ):
        self.read_file = read_file
        self.write_file = write_file
        self.edit_file = edit_file
        self.run_subagent = run_subagent
        self.manage_task = manage_task
        self.show_file_diff = show_file_diff
        self.get_imom_active_publish_root = get_imom_active_publish_root
        self.sandbox_guard = sandbox_guard
        self.sandbox_bypass = sandbox_bypass
        self.read_text = read_text
        self.notify_event = notify_event
        self.mark_plan_turn = mark_plan_turn
        self.registry_execute_override = registry_execute_override
        self.is_outside_workspace = is_outside_workspace
        self.safe_print = safe_print
        self.tool_call_id = tool_call_id
        self.permission_error_type = permission_error_type

    def for_call(self, tool_call_id):
        adapter = copy(self)
        adapter.tool_call_id = str(tool_call_id or "")
        return adapter

    def announce(self, name, arguments, event_callback):
        if event_callback is not None:
            return
        friendly = f"⚙️ 星瑶正在后台悄悄使用工具: {name} 哒～"
        if name == "read_file":
            friendly = f"🔍 星瑶正在读取文件：{arguments.get('file_path', '未知文件')}"
        elif name == "write_file":
            friendly = f"✍️ 星瑶正在写入文件：{arguments.get('file_path', '未知文件')}"
        elif name == "list_directory":
            friendly = f"📂 星瑶正在查看目录：{arguments.get('dir_path', '当前目录')}"
        elif name == "execute_command":
            command = arguments.get("command", "...")
            if len(command) > 20:
                command = command[:20] + "..."
            friendly = f"💻 星瑶正在执行命令：{command}"
        elif name == "search_code":
            friendly = f"🔍 星瑶正在全局查找: {arguments.get('query', '')}"
        elif name == "find_definition":
            friendly = f"🔍 星瑶正在查找符号定义: {arguments.get('symbol', '')}"
        elif name == "search_news":
            friendly = f"🌐 星瑶正在联网搜索：{arguments.get('query', '')}"
        elif name == "fetch_url":
            friendly = f"🌐 星瑶正在访问网址：{arguments.get('url', '')}"
        elif name == "get_weather":
            friendly = f"🌤 星瑶正在查询天气：{arguments.get('city', '') or '自动定位'}"
        from prompt_toolkit.formatted_text import HTML

        safe = friendly.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.safe_print(HTML(f"<ansigreen>[ {safe} ]</ansigreen>"))

    @staticmethod
    def _uses_override(candidate, original) -> bool:
        return candidate is not None and candidate is not original

    def _execute_builtin(self, resolved_tool, candidate, original, arguments):
        try:
            if self._uses_override(candidate, original):
                return _as_tool_result(candidate(**arguments))
            return _as_tool_result(resolved_tool.execute(**arguments))
        except Exception as error:
            return ToolResult(
                f"{TOOL_ERROR_PREFIX} 调用工具 {resolved_tool.spec.name} 时发生系统内部错误：{error}",
                is_error=True,
            )

    def _guard(self, name, path, context):
        if self.sandbox_guard is None:
            return _sandbox_outside_guard(
                name,
                path,
                context.event_callback,
                context.permission_owner,
                context.parent_turn_id,
                context.permission_source,
                context.cancel_event,
                permission_checker=context.permission_checker,
                is_outside_workspace=self.is_outside_workspace,
                sandbox_bypass=self.sandbox_bypass,
                permission_error_type=self.permission_error_type,
            )
        return self.sandbox_guard(
            name,
            path,
            context.event_callback,
            context.permission_owner,
            context.parent_turn_id,
            context.permission_source,
            context.cancel_event,
        )

    def execute_read_file(self, resolved_tool, arguments, context) -> ToolResult:
        bypass, error = self._guard("read_file", arguments.get("file_path"), context)
        if error:
            return ToolResult(error, is_error=True)
        with bypass if bypass is not None else nullcontext():
            return self._execute_builtin(resolved_tool, self.read_file, _read_file, arguments)

    def execute_write_file(self, resolved_tool, arguments, context) -> ToolResult:
        file_path = arguments.get("file_path", "")
        new_content = arguments.get("content", "")
        bypass, error = self._guard("write_file", file_path, context)
        if error:
            return ToolResult(error, is_error=True)
        with bypass if bypass is not None else self.sandbox_bypass():
            old_content = ""
            if os.path.exists(file_path):
                try:
                    old_content = self.read_text(file_path)
                except OSError as read_error:
                    logger.warning("读取旧文件内容失败 (%s): %s", file_path, read_error)
            write_result = self._execute_builtin(
                resolved_tool, self.write_file, _write_file, arguments
            ).to_message_content()
        if isinstance(write_result, str) and write_result.startswith(TOOL_ERROR_PREFIX):
            return ToolResult(write_result, is_error=True)

        from core.memory import set_undo_state

        set_undo_state(file_path, old_content)
        self.show_file_diff(
            file_path,
            old_content,
            new_content,
            context.event_callback,
            panel_title=f"📝 [bold green]已修改文件: {file_path}[/bold green]",
            truncated_note="已成功写入文件",
            tool_call_id=self.tool_call_id,
        )
        return ToolResult(f"已成功将内容写入文件：{file_path}，并已向用户展示 Diff。")

    def execute_edit_file(self, resolved_tool, arguments, context) -> ToolResult:
        file_path = arguments.get("file_path")
        target = arguments.get("target_content")
        replacement = arguments.get("replacement_content")
        bypass, error = self._guard("edit_file", file_path, context)
        if error:
            return ToolResult(error, is_error=True)
        max_target_len = 1500
        if target and len(target) > max_target_len:
            return ToolResult(
                f"{TOOL_ERROR_PREFIX} edit_file 的 target_content 过长（{len(target)} 字符），"
                f"超过 {max_target_len} 字符限制，容易在传输过程中被截断。请将本次编辑拆分为多个"
                f"不超过 {max_target_len} 字符的小段后重试；如需整体重写，请改用 write_file。",
                is_error=True,
            )
        if not os.path.isfile(file_path or ""):
            return ToolResult(
                f"{TOOL_ERROR_PREFIX} edit_file 目标文件不存在：{file_path}。"
                "edit_file 只能修改已有文件；创建新文件请改用 write_file。",
                is_error=True,
            )
        with bypass if bypass is not None else self.sandbox_bypass():
            try:
                old_content = self.read_text(file_path)
            except OSError as read_error:
                logger.warning("读取旧文件内容失败 (%s): %s", file_path, read_error)
                old_content = ""
            call_arguments = {
                "file_path": file_path,
                "target_content": target,
                "replacement_content": replacement,
                "replace_all": bool(arguments.get("replace_all", False)),
                "start_line": arguments.get("start_line"),
                "end_line": arguments.get("end_line"),
            }
            result = self._execute_builtin(
                resolved_tool, self.edit_file, _edit_file, call_arguments
            ).to_message_content()
            if not result.startswith(TOOL_ERROR_PREFIX):
                try:
                    new_content = self.read_text(file_path)
                except OSError as read_error:
                    logger.warning("读取新文件内容失败 (%s): %s", file_path, read_error)
                    new_content = ""
                self.show_file_diff(
                    file_path,
                    old_content,
                    new_content,
                    context.event_callback,
                    panel_title=f"📝 [bold green]局部修改文件: {file_path}[/bold green]",
                    truncated_note="已局部替换并修改成功",
                    tool_call_id=self.tool_call_id,
                )
        return ToolResult(result, is_error=result.startswith(TOOL_ERROR_PREFIX))

    def execute_run_subagent(self, resolved_tool, arguments, context) -> ToolResult:
        if not context.allow_subagents:
            return ToolResult(f"{TOOL_ERROR_PREFIX} 子代理不能启动其他子代理。", is_error=True)
        result = self.run_subagent(
            task_description=arguments.get("task_description", ""),
            subagent_type=arguments.get("subagent_type", "general"),
            plan_item_id=arguments.get("plan_item_id", ""),
            run_in_background=arguments.get("run_in_background", False),
            timeout_seconds=arguments.get("timeout_seconds"),
            parent_session=context.parent_session,
            parent_turn_id=context.parent_turn_id,
            parent_cancel_event=context.cancel_event,
            event_callback=context.event_callback,
        )
        return _as_tool_result(result)

    def execute_ask_user(self, resolved_tool, arguments, context) -> ToolResult:
        from core.ask_user import ask_user, normalize_question_request

        plan_item_id = str(arguments.get("plan_item_id", "") or "").strip()
        scheduler = None
        try:
            question, choices = normalize_question_request(
                arguments.get("question", ""), arguments.get("options")
            )
        except ValueError as error:
            return ToolResult(str(error), is_error=True)

        if plan_item_id:
            from core.task_scheduler import get_default_scheduler

            scheduler = get_default_scheduler()
            snapshot = scheduler.plan.snapshot()
            plan_item = next((item for item in snapshot if item["id"] == plan_item_id), None)
            input_items = [item for item in snapshot if item.get("kind") == "input_preparation"]
            hints = ""
            if input_items:
                ids = ", ".join(
                    f"'{item['id']}'({item['subject']})" for item in input_items[:5]
                )
                hints = f" 当前 plan 中的 input_preparation 项: [{ids}]。"
            if plan_item is None:
                return ToolResult(json.dumps({
                    "status": "blocked",
                    "plan_item_id": plan_item_id,
                    "error": f"用户输入准备项 '{plan_item_id}' 不存在。{hints}",
                }, ensure_ascii=False), is_error=True)
            if plan_item.get("kind") != "input_preparation":
                return ToolResult(
                    f"无法提问：plan_item_id='{plan_item_id}' 的 kind 是 "
                    f"'{plan_item.get('kind', 'unknown')}'，不是 input_preparation。{hints}",
                    is_error=True,
                )
            if not scheduler.start_item(
                plan_item_id, owner="ask_user", event_callback=context.event_callback
            ):
                return ToolResult(json.dumps({
                    "status": "blocked",
                    "plan_item_id": plan_item_id,
                    "error": "计划项尚未满足依赖或当前计划不可执行。",
                }, ensure_ascii=False), is_error=True)

        try:
            result = ask_user(
                question,
                choices,
                owner_id=context.permission_owner,
                parent_turn_id=context.parent_turn_id,
                source=context.permission_source,
                cancel_event=context.cancel_event,
            )
            if scheduler is not None:
                try:
                    payload = json.loads(str(result))
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict) and payload.get("status") == "cancelled":
                    scheduler.cancel(plan_item_id, "用户取消提问")
                elif str(result).startswith("无法") or str(result).startswith(TOOL_ERROR_PREFIX):
                    scheduler.fail(plan_item_id, str(result))
                elif context.cancel_event is not None and context.cancel_event.is_set():
                    scheduler.cancel(plan_item_id, "用户取消提问")
                    result = json.dumps({
                        "status": "cancelled",
                        "plan_item_id": plan_item_id,
                        "error": "用户取消提问",
                    }, ensure_ascii=False)
                else:
                    scheduler.complete(plan_item_id, str(result))
                self.notify_event(context.event_callback, "plan_updated", {
                    "items": scheduler.plan.snapshot(),
                    "live_only": True,
                })
            return _as_tool_result(result)
        except Exception as error:
            if scheduler is not None:
                scheduler.fail(plan_item_id, str(error))
            raise

    def execute_manage_task(self, resolved_tool, arguments, context) -> ToolResult:
        extras = {
            "event_callback": context.event_callback,
            "permission_owner": context.permission_owner,
            "cancel_event": context.cancel_event,
        }
        supported = {
            key: value
            for key, value in extras.items()
            if key in inspect.signature(self.manage_task).parameters
        }
        return _as_tool_result(self.manage_task(
            action=arguments.get("action"),
            task_id=arguments.get("task_id"),
            **supported,
        ))

    def execute_registry_tool(self, resolved_tool, arguments, context) -> ToolResult:
        name = resolved_tool.spec.name
        guard_path = None
        if name in {"list_directory", "search_code", "find_definition", "glob_files"}:
            guard_path = arguments.get("dir_path", ".")
        elif name == "imom_register_project":
            guard_path = arguments.get("hint_path") or arguments.get("publish_root") or ""
        elif name.startswith("imom_"):
            guard_path = self.get_imom_active_publish_root()

        bypass, error = self._guard(name, guard_path, context)
        if error:
            return ToolResult(error, is_error=True)
        call_arguments = dict(arguments)
        if context.cancel_event is not None and name in {
            "execute_command", "search_code", "glob_files", "fetch_url", "get_weather", "search_news"
        }:
            call_arguments["cancel_event"] = context.cancel_event
        with bypass if bypass is not None else nullcontext():
            try:
                if self.registry_execute_override is not None:
                    result = _as_tool_result(
                        self.registry_execute_override(name, **call_arguments)
                    )
                else:
                    result = _as_tool_result(resolved_tool.execute(**call_arguments))
            except Exception as error:
                return ToolResult(
                    f"{TOOL_ERROR_PREFIX} 调用工具 {name} 时发生系统内部错误：{error}",
                    is_error=True,
                )

        if name in {"plan_write", "plan_update"}:
            if name == "plan_write" and result.to_message_content().strip() == "计划已更新。":
                self.mark_plan_turn(context.parent_turn_id)
            from core.plan import get_default_plan_manager

            self.notify_event(context.event_callback, "plan_updated", {
                "items": get_default_plan_manager().snapshot(),
                "live_only": True,
            })
        return result

    def execute_mcp_tool(self, resolved_tool, arguments, context) -> ToolResult:
        try:
            return _as_tool_result(resolved_tool.execute(**arguments))
        except Exception as error:
            return ToolResult(
                f"{TOOL_ERROR_PREFIX} 调用工具 {resolved_tool.spec.name} 时发生系统内部错误：{error}",
                is_error=True,
            )

    def execute(self, resolved_tool, arguments, context) -> ToolResult:
        name = resolved_tool.spec.name
        if name == "read_file":
            return self.execute_read_file(resolved_tool, arguments, context)
        if name == "write_file":
            return self.execute_write_file(resolved_tool, arguments, context)
        if name == "edit_file":
            return self.execute_edit_file(resolved_tool, arguments, context)
        if name == "run_subagent":
            return self.execute_run_subagent(resolved_tool, arguments, context)
        if name == "ask_user":
            return self.execute_ask_user(resolved_tool, arguments, context)
        if name == "manage_task":
            return self.execute_manage_task(resolved_tool, arguments, context)
        if resolved_tool.source == "mcp":
            return self.execute_mcp_tool(resolved_tool, arguments, context)
        return self.execute_registry_tool(resolved_tool, arguments, context)
