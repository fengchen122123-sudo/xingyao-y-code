from core.tool_protocol import ToolResult, ToolSpec


def test_tool_spec_defaults_to_serial_execution():
    spec = ToolSpec(
        name="demo",
        description="demo",
        input_schema={},
        execute=lambda: None,
    )

    assert spec.parallel_safe is False


def test_tool_result_keeps_error_diff_and_changed_files():
    result = ToolResult(
        content="failed",
        is_error=True,
        changed_files=["x.py"],
        diff="@@ -1 +1 @@",
    )

    assert result.is_error is True
    assert result.changed_files == ["x.py"]
    assert result.diff == "@@ -1 +1 @@"


def test_tool_result_message_content_is_model_facing_text():
    result = ToolResult(content="ok", metadata={"duration_ms": 12})

    assert result.to_message_content() == "ok"
