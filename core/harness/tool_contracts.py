"""Strict, source-aware contracts for built-in, plugin and MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from core.tool_protocol import ToolResult, ToolSpec
from core.tool_registry import ToolRegistry, _INTERNAL_ARG_KEYS

# 模型侧高频写出的常见工具别名 → 注册表规范名。只收语义等价的映射，
# 宁缺毋滥：错误的别名会静默调用错工具，比一次"未找到→整轮重试"更糟。
# 别名命中但模型传参全部不被规范工具 schema 识别时，参数形状守卫返回
# 带规范名与合法参数名的显式错误——而不是静默吞参后用默认参数执行出
# 错误结果；缺必需参数时 execute_strict 抛 TypeError 同样带出规范名，
# 两种报错都比"未找到工具"更可学习。
TOOL_ALIASES: dict[str, str] = {
    # read_file
    "read": "read_file",
    "cat": "read_file",
    "view": "read_file",
    "open": "read_file",
    "file_read": "read_file",
    "readfile": "read_file",
    # write_file
    "write": "write_file",
    "create_file": "write_file",
    "new_file": "write_file",
    "file_write": "write_file",
    "writefile": "write_file",
    # edit_file
    "edit": "edit_file",
    "file_edit": "edit_file",
    "replace": "edit_file",
    "str_replace": "edit_file",
    "str_replace_editor": "edit_file",
    # execute_command
    "bash": "execute_command",
    "sh": "execute_command",
    "shell": "execute_command",
    "terminal": "execute_command",
    "exec": "execute_command",
    "run": "execute_command",
    "command": "execute_command",
    "run_command": "execute_command",
    "run_shell_command": "execute_command",
    # search_code
    "grep": "search_code",
    "search_in_files": "search_code",
    "find_in_files": "search_code",
    "search_text": "search_code",
    "code_search": "search_code",
    # glob_files
    "glob": "glob_files",
    "find_files": "glob_files",
    "file_glob": "glob_files",
    # list_directory
    "ls": "list_directory",
    "dir": "list_directory",
    "list_dir": "list_directory",
    "list_files": "list_directory",
    # fetch_url
    "fetch": "fetch_url",
    "web_fetch": "fetch_url",
    "webfetch": "fetch_url",
    "http_get": "fetch_url",
    "url_fetch": "fetch_url",
    "open_url": "fetch_url",
    # run_subagent
    "task": "run_subagent",
    "agent": "run_subagent",
    "subagent": "run_subagent",
    "spawn_agent": "run_subagent",
    # manage_task
    "todo": "manage_task",
    "todoread": "manage_task",
    "todowrite": "manage_task",
    "todo_manager": "manage_task",
    "task_manager": "manage_task",
    # find_definition
    "goto_definition": "find_definition",
    "definition": "find_definition",
    # ask_user
    "ask": "ask_user",
    "ask_question": "ask_user",
    "clarify": "ask_user",
    "request_user_feedback": "ask_user",
    "user_feedback": "ask_user",
    # get_weather / search_news
    "weather": "get_weather",
    "news": "search_news",
    "news_search": "search_news",
}


def _normalize_alias_key(name) -> str | None:
    if not isinstance(name, str):
        return None
    return name.strip().lower().replace("-", "_")


def _alias_arguments_recognized(spec: ToolSpec, arguments: dict) -> bool:
    """别名命中的参数形状守卫：模型参数是否至少有一个被规范工具识别。

    别名只映射工具名、不映射参数约定——模型按幻觉工具的参数表传参时
    （如 ls 传 path），严格过滤会静默丢弃全部参数，带默认值的工具
    （list_directory 的 dir_path、get_weather 的 city）会以默认值"成功"
    执行出错误结果。此时必须显式报错交给模型自纠。bare 调用（无参）与
    部分匹配照常放行；调度层注入的内部键（cancel_event 等）不参与判定。
    """
    props = (spec.input_schema or {}).get("properties")
    if not isinstance(props, dict) or not props:
        return True
    model_args = [key for key in arguments if key not in _INTERNAL_ARG_KEYS]
    return not model_args or any(key in props for key in model_args)


def _execute_alias_strict(registry: ToolRegistry, alias_name, spec: ToolSpec,
                          arguments: dict) -> ToolResult:
    """别名执行入口：形状守卫失败时返回可学习的显式错误，否则照常严格执行。"""
    if not _alias_arguments_recognized(spec, arguments):
        props = (spec.input_schema or {}).get("properties") or {}
        unsupported = sorted(
            key for key in arguments if key not in props and key not in _INTERNAL_ARG_KEYS
        )
        return ToolResult(
            content=(
                f"🚨 工具 {alias_name} 是 {spec.name} 的别名，但参数形状不匹配："
                f"不支持的键 {unsupported}。请改用规范名 {spec.name}，"
                f"其参数为：{', '.join(str(k) for k in props) or '（无）'}。"
            ),
            is_error=True,
        )
    return registry.execute_strict(spec.name, **arguments)


@dataclass(frozen=True)
class ResolvedTool:
    spec: ToolSpec
    source: Literal["registry", "mcp"]
    execute: Callable[..., ToolResult]


def _mcp_executor(mcp_manager, name: str) -> Callable[..., ToolResult]:
    def execute(**arguments):
        return ToolResult(content=mcp_manager.call_tool_sync(name, arguments))

    return execute


def resolve_tool(name, registry: ToolRegistry, mcp_manager) -> ResolvedTool:
    """Resolve registry tools before MCP tools without touching MCP state.

    解析顺序：注册表精确名 → MCP 精确名 → 别名映射回注册表 → 报错。
    别名兜底放在最后：任何真实存在的工具（含 MCP）都不会被别名遮蔽。
    """
    try:
        spec = registry.get(name)
    except KeyError:
        pass
    else:
        return ResolvedTool(
            spec=spec,
            source="registry",
            execute=lambda **arguments: registry.execute_strict(spec.name, **arguments),
        )

    schema = mcp_manager.get_schema(name)
    if schema is not None:
        function = schema.get("function", {})
        spec = ToolSpec(
            name=name,
            description=function.get("description", ""),
            input_schema=function.get("parameters", {"type": "object"}),
            execute=_mcp_executor(mcp_manager, name),
            read_only=False,
            mutates_files=False,
            requires_permission=False,
            cancellable=True,
            parallel_safe=False,
            # MCP 是外部工具，行为不可知：保守归 system（03 规格 3.3），
            # v1 仅作契约元数据，不改变既有权限与调度判定。
            side_effect_scope="system",
        )
        return ResolvedTool(spec=spec, source="mcp", execute=spec.execute)

    key = _normalize_alias_key(name)
    canonical = TOOL_ALIASES.get(key) if key else None
    if canonical:
        try:
            spec = registry.get(canonical)
        except KeyError:
            spec = None
        if spec is not None:
            return ResolvedTool(
                spec=spec,
                source="registry",
                execute=lambda **arguments: _execute_alias_strict(
                    registry, name, spec, arguments
                ),
            )

    raise KeyError(f"unknown tool: {name}")
