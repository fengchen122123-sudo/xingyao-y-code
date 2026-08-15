import pytest

from core.harness.tool_contracts import resolve_tool
from core.tool_protocol import ToolResult, ToolSpec
from core.tool_registry import ToolRegistry


class FakeMcpManager:
    def __init__(self, schemas):
        self.schemas = schemas
        self.calls = []

    def get_schema(self, name):
        return next(
            (
                schema
                for schema in self.schemas
                if schema.get("function", {}).get("name") == name
            ),
            None,
        )

    def call_tool_sync(self, name, arguments):
        self.calls.append((name, arguments))
        return "fixed result"


def test_registry_tool_preserves_plugin_metadata():
    spec = ToolSpec(
        name="plugin_tool", description="plugin", input_schema={"type": "object"},
        execute=lambda: ToolResult("ok"), requires_permission=True, parallel_safe=True,
    )
    registry = ToolRegistry([spec])
    resolved = resolve_tool("plugin_tool", registry, FakeMcpManager([]))
    assert resolved.spec is spec
    assert resolved.source == "registry"


def test_registry_resolved_executor_uses_strict_schema_filtering():
    seen = {}

    def execute(**arguments):
        seen.update(arguments)
        return "ok"

    registry = ToolRegistry([ToolSpec(
        name="plugin_tool",
        description="plugin",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        execute=execute,
    )])
    resolved = resolve_tool("plugin_tool", registry, FakeMcpManager([]))

    result = resolved.execute(value="kept", invented="dropped")

    assert result.content == "ok"
    assert seen == {"value": "kept"}


def test_mcp_tool_gets_explicit_nonparallel_contract():
    schema = {"type": "function", "function": {
        "name": "remote", "description": "remote", "parameters": {"type": "object"},
    }}
    resolved = resolve_tool("remote", ToolRegistry(), FakeMcpManager([schema]))
    assert resolved.source == "mcp"
    assert resolved.spec.read_only is False
    assert resolved.spec.requires_permission is False
    assert resolved.spec.parallel_safe is False


def test_unknown_tool_fails_contract_resolution():
    with pytest.raises(KeyError, match="unknown tool"):
        resolve_tool("missing", ToolRegistry(), FakeMcpManager([]))


def test_execute_strict_does_not_convert_executor_exception():
    def fail():
        raise ValueError("boom")

    registry = ToolRegistry([ToolSpec(
        name="fail", description="fail", input_schema={"type": "object"}, execute=fail,
    )])
    with pytest.raises(ValueError, match="boom"):
        registry.execute_strict("fail")
