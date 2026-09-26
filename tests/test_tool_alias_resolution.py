"""resolve_tool 别名容错回归。

背景：模型常写出训练语料里熟悉的工具名（Read/Bash/str_replace/task…）。
别名层把这些名字静默映射到规范工具，避免"未找到→整轮重试"的浪费。
解析顺序在注册表与 MCP 精确匹配之后兜底：真实工具（含同名 MCP）永不被
别名遮蔽；别名指向的规范名必须真实注册，未来改名时由
test_alias_targets_all_registered 兜底报错。
"""

import pytest

from core.harness.tool_contracts import TOOL_ALIASES, resolve_tool
from core.tool_protocol import ToolResult, ToolSpec
from core.tool_registry import ToolRegistry, build_default_registry


class _FakeMcpManager:
    def __init__(self, schemas=None):
        self._schemas = schemas or {}

    def get_schema(self, name):
        return self._schemas.get(name)


def _registry(*names):
    reg = ToolRegistry()
    for name in names:
        seen = {}
        spec = ToolSpec(
            name=name,
            description="",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            execute=lambda **kwargs: ToolResult(content=str(sorted(kwargs.items()))),
        )
        reg.register(spec)
        seen[name] = spec
    return reg


def test_alias_resolves_to_canonical_registry_tool():
    resolved = resolve_tool("read", _registry("read_file"), _FakeMcpManager())
    assert resolved.source == "registry"
    assert resolved.spec.name == "read_file"
    assert resolved.execute(path="a.txt").content == "[('path', 'a.txt')]"


def test_alias_matching_is_case_and_hyphen_insensitive():
    reg = _registry("execute_command", "edit_file")
    assert resolve_tool("Bash", reg, _FakeMcpManager()).spec.name == "execute_command"
    assert resolve_tool("str-replace", reg, _FakeMcpManager()).spec.name == "edit_file"


def test_registry_exact_name_wins_over_alias_entry():
    # 若未来把某个别名升级成正式工具（如注册了名为 read 的工具），精确名优先。
    resolved = resolve_tool("read", _registry("read", "read_file"), _FakeMcpManager())
    assert resolved.spec.name == "read"


def test_mcp_exact_name_shadows_alias():
    # MCP 服务器提供了名为 bash 的真实工具时，必须走 MCP 而不是被别名劫持。
    mcp = _FakeMcpManager({
        "bash": {"function": {"description": "mcp bash", "parameters": {"type": "object"}}},
    })
    resolved = resolve_tool("bash", _registry("execute_command"), mcp)
    assert resolved.source == "mcp"
    assert resolved.spec.name == "bash"


def test_alias_with_unrecognized_arguments_fails_loud():
    """别名命中但参数全不被识别：必须显式报错，而不是静默吞参后用默认值执行。"""
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="list_directory",
        description="",
        input_schema={"type": "object", "properties": {
            "dir_path": {"type": "string"},
            "max_entries": {"type": "integer"},
        }},
        execute=lambda **kwargs: ToolResult(content=f"listed {kwargs.get('dir_path')}"),
    ))
    resolved = resolve_tool("ls", reg, _FakeMcpManager())

    result = resolved.execute(path="/foo")
    assert result.is_error
    assert "list_directory" in result.content
    assert "dir_path" in result.content

    # 参数名对得上 / bare 调用：照常执行，默认参数语义由工具自身负责
    assert resolved.execute(dir_path="/foo").content == "listed /foo"
    assert resolved.execute().content == "listed None"


def test_alias_partial_argument_match_still_executes():
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="run_subagent",
        description="",
        input_schema={"type": "object", "properties": {"description": {"type": "string"}}},
        execute=lambda **kwargs: ToolResult(content=str(sorted(kwargs.items()))),
    ))
    resolved = resolve_tool("task", reg, _FakeMcpManager())
    # description 被识别、subagent_type 被严格过滤丢弃：部分匹配放行（既有行为）
    assert resolved.execute(description="x", subagent_type="y").content == "[('description', 'x')]"


def test_unknown_name_still_raises_key_error():
    with pytest.raises(KeyError):
        resolve_tool("definitely_not_a_tool", _registry("read_file"), _FakeMcpManager())


def test_alias_targets_all_registered():
    # 别名指向的规范名必须都在默认注册表里；任何工具改名/删除时此测试报错，
    # 提醒同步更新 TOOL_ALIASES，避免别名静默失效退回"未找到"。
    registry = build_default_registry()
    registered = {spec.name for spec in registry.all()}
    missing = {canonical for canonical in TOOL_ALIASES.values() if canonical not in registered}
    assert not missing, f"TOOL_ALIASES 指向未注册工具: {sorted(missing)}"
