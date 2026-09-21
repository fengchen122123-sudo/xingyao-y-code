"""Strict, source-aware contracts for built-in, plugin and MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from core.tool_protocol import ToolResult, ToolSpec
from core.tool_registry import ToolRegistry


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
    """Resolve registry tools before MCP tools without touching MCP state."""
    try:
        spec = registry.get(name)
    except KeyError:
        schema = mcp_manager.get_schema(name)
        if schema is None:
            raise KeyError(f"unknown tool: {name}")
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
    return ResolvedTool(
        spec=spec,
        source="registry",
        execute=lambda **arguments: registry.execute_strict(name, **arguments),
    )
