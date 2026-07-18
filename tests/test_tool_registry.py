from core.tool_registry import build_default_registry


def test_default_registry_exposes_read_and_mutating_capabilities():
    registry = build_default_registry()

    assert registry.get("read_file").read_only is True
    assert registry.get("read_file").requires_permission is False
    assert registry.get("write_file").mutates_files is True
    assert registry.get("write_file").requires_permission is True


def test_registry_generates_existing_function_names():
    registry = build_default_registry()
    names = {
        schema["function"]["name"]
        for schema in registry.api_schemas()
    }

    assert {"read_file", "write_file", "edit_file", "execute_command"} <= names
    assert "glob_files" in names


def test_unknown_tool_returns_error_result():
    registry = build_default_registry()

    result = registry.execute("missing_tool")

    assert result.is_error is True
    assert "missing_tool" in result.content
