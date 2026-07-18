from core.tool_presenter import summarize_tool_finished, summarize_tool_started


def test_read_file_uses_star_yao_copy_without_source_content():
    started = summarize_tool_started("read_file", {"file_path": "core/llm.py"})
    finished = summarize_tool_finished(
        "read_file",
        {"file_path": "core/llm.py"},
        {"content": "line one\nline two\nline three"},
        ok=True,
    )

    assert started == "星瑶正在查看文件：core/llm.py"
    assert finished == "星瑶已查看文件：core/llm.py（3 行）"
    assert "line one" not in finished


def test_write_edit_and_command_copy_is_stable():
    assert summarize_tool_started(
        "write_file", {"file_path": "a.py"}
    ) == "星瑶正在写入文件：a.py"
    assert summarize_tool_started(
        "edit_file", {"file_path": "a.py"}
    ) == "星瑶正在修改文件：a.py"
    assert summarize_tool_started(
        "execute_command", {"command": "pytest -q"}
    ) == "星瑶正在执行命令：pytest -q"


def test_failed_tool_is_explicit():
    text = summarize_tool_finished(
        "execute_command",
        {"command": "pytest -q"},
        {"content": "exit 1"},
        ok=False,
    )

    assert text == "命令执行失败：pytest -q"
