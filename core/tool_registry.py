"""Capability registry for the built-in tools."""

from __future__ import annotations

from typing import Any, Iterable

from core.tool_protocol import ToolResult, ToolSpec


class ToolRegistry:
    def __init__(self, specs: Iterable[ToolSpec] = ()):
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if not spec.name:
            raise ValueError("tool name must not be empty")
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def all(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def api_schemas(self) -> list[dict[str, Any]]:
        return [spec.api_schema() for spec in self._specs.values()]

    def execute(self, name: str, **arguments: Any) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(content=f"Unknown tool: {name}", is_error=True)
        try:
            result = spec.execute(**arguments)
            if isinstance(result, ToolResult):
                return result
            return ToolResult(content=str(result) if result is not None else "")
        except Exception as exc:
            return ToolResult(content=f"Tool execution error: {exc}", is_error=True)


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def _adapt(function):
    def execute(**arguments):
        return function(**arguments)
    return execute


def build_default_registry() -> ToolRegistry:
    from core import tools

    read_specs = [
        ToolSpec(
            name="read_file",
            description="读取指定文件的文本内容。",
            input_schema=_schema({"file_path": {"type": "string"}}, ["file_path"]),
            execute=_adapt(tools.read_file),
            read_only=True,
            requires_permission=False,
            activity_description="读取文件",
        ),
        ToolSpec(
            name="list_directory",
            description="列出目录内容。",
            input_schema=_schema({"dir_path": {"type": "string", "default": "."}}),
            execute=_adapt(tools.list_directory),
            read_only=True,
            requires_permission=False,
            activity_description="查看目录",
        ),
        ToolSpec(
            name="search_code",
            description="在项目中搜索代码内容。",
            input_schema=_schema({
                "query": {"type": "string"},
                "dir_path": {"type": "string", "default": "."},
            }, ["query"]),
            execute=_adapt(tools.search_code),
            read_only=True,
            requires_permission=False,
            activity_description="搜索代码",
        ),
        ToolSpec(
            name="search_news",
            description="搜索互联网新闻。",
            input_schema=_schema({
                "query": {"type": "string"},
                "num_results": {"type": "integer", "default": 5},
            }, ["query"]),
            execute=_adapt(tools.search_news),
            read_only=True,
            requires_permission=False,
            activity_description="联网搜索",
        ),
        ToolSpec(
            name="glob_files",
            description="按 glob 模式查找文件。",
            input_schema=_schema({
                "pattern": {"type": "string"},
                "dir_path": {"type": "string", "default": "."},
            }, ["pattern"]),
            execute=_adapt(tools.glob_files),
            read_only=True,
            requires_permission=False,
            activity_description="匹配文件",
        ),
    ]

    mutating_specs = [
        ToolSpec(
            name="write_file",
            description="创建或完整写入文件。",
            input_schema=_schema({
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            }, ["file_path", "content"]),
            execute=_adapt(tools.write_file),
            mutates_files=True,
            activity_description="写入文件",
        ),
        ToolSpec(
            name="edit_file",
            description="以精确字符串替换的方式修改文件。",
            input_schema=_schema({
                "file_path": {"type": "string"},
                "target_content": {"type": "string"},
                "replacement_content": {"type": "string"},
            }, ["file_path", "target_content", "replacement_content"]),
            execute=_adapt(tools.edit_file),
            mutates_files=True,
            activity_description="修改文件",
        ),
        ToolSpec(
            name="execute_command",
            description="执行本地命令。",
            input_schema=_schema({
                "command": {"type": "string"},
                "run_in_background": {"type": "boolean", "default": False},
            }, ["command"]),
            execute=_adapt(tools.execute_command),
            activity_description="执行命令",
        ),
        ToolSpec(
            name="manage_task",
            description="查看或终止后台任务。",
            input_schema=_schema({
                "action": {"type": "string"},
                "task_id": {"type": "string"},
            }, ["action"]),
            execute=_adapt(tools.manage_task),
            activity_description="管理后台任务",
        ),
        ToolSpec(
            name="run_subagent",
            description="启动后台辅助 Agent。",
            input_schema=_schema({"task_description": {"type": "string"}}, ["task_description"]),
            execute=_adapt(tools.run_subagent),
            activity_description="启动子 Agent",
        ),
    ]
    return ToolRegistry([*read_specs, *mutating_specs])
