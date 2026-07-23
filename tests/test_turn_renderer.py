from core.turn_renderer import TurnRenderer
from rich.text import Text


def test_renderer_does_not_leak_split_thinking_tags():
    renderer = TurnRenderer()

    renderer.consume("assistant_delta", {"text": "A <thi"})
    renderer.consume("assistant_delta", {"text": "nk>private</think>B"})

    output = renderer.finish()
    assert output == "A B"
    assert "<think>" not in output


def test_renderer_keeps_tool_results_in_requested_order():
    renderer = TurnRenderer()

    renderer.consume("tool_requested", {"id": "1", "name": "read_file", "preview": "a.py"})
    renderer.consume("tool_requested", {"id": "2", "name": "read_file", "preview": "b.py"})
    renderer.consume("tool_finished", {"id": "1", "name": "read_file", "ok": True})
    renderer.consume("tool_finished", {"id": "2", "name": "read_file", "ok": True})

    output = renderer.finish()
    assert output.index("a.py") < output.index("b.py")
    assert output.count("✓") == 2


def test_renderer_reports_neutral_status_once_without_raw_error():
    renderer = TurnRenderer()

    renderer.consume("error", {"message": "命令失败"})
    renderer.consume("error", {"message": "命令失败"})

    output = renderer.finish()
    assert output.count("星瑶暂未完成当前步骤") == 1
    assert "命令失败" not in output


def test_renderer_shows_structured_plan_status():
    renderer = TurnRenderer()
    renderer.consume("plan_updated", {
        "items": [
            {"id": "1", "subject": "检查代码", "status": "completed"},
            {"id": "2", "subject": "运行测试", "status": "in_progress"},
        ]
    })

    output = renderer.finish()
    assert "✓ 检查代码" in output
    assert "■ 运行测试" in output


def test_renderer_ignores_raw_tool_output_preview():
    renderer = TurnRenderer()

    renderer.consume("tool_finished", {
        "id": "1",
        "name": "list_directory",
        "ok": True,
        "summary": "星瑶已查看目录：.",
        "preview": ".",
        "output_preview": "main.py\ncore",
    })

    output = renderer.finish()
    assert "星瑶已查看目录" in output
    assert "main.py" not in output
    assert "core" not in output


def test_renderer_explains_markdown_only_final_answer_after_tools():
    renderer = TurnRenderer()

    renderer.consume("tool_finished", {"id": "1", "name": "read_file", "ok": True})
    renderer.consume("assistant_delta", {"text": "**\n)\n"})

    output = renderer.finish()
    assert "检查完成：已读取 1 个文件。模型未返回最终结论。" in output


def test_renderer_uses_distinct_ansi_colors_for_tool_feedback():
    renderer = TurnRenderer()

    requested = "".join(renderer.consume("tool_requested", {"id": "1", "name": "read_file"}))
    finished = "".join(renderer.consume("tool_finished", {
        "id": "1",
        "name": "read_file",
        "ok": True,
        "output_preview": "line one",
    }))
    failed = "".join(renderer.consume("error", {"message": "失败"}))

    assert "\x1b[32m" in requested
    assert "\x1b[32m" in finished
    assert "\x1b[33m" in failed
    assert "失败" not in Text.from_ansi(failed).plain


def test_renderer_locally_summarizes_missing_final_answer():
    renderer = TurnRenderer()
    renderer.consume("tool_finished", {
        "id": "1",
        "name": "read_file",
        "preview": "core/llm.py",
        "ok": True,
    })

    assert "检查完成：已读取 1 个文件。模型未返回最终结论。" in renderer.finish()


def test_renderer_buffers_reasoning_until_finished_and_buffers_answer():
    renderer = TurnRenderer()

    first = renderer.consume(
        "thinking_delta", {"text": "正在分析"}
    )
    second = renderer.consume(
        "thinking_delta", {"text": "下一步"}
    )
    thinking = "".join(renderer.consume("thinking_finished"))
    answer_delta = renderer.consume(
        "assistant_delta", {"text": "# 最终结论"}
    )

    assert first == []
    assert second == []
    assert thinking == ""
    assert "正在分析下一步" not in thinking
    assert answer_delta == ["# 最终结论"]
    assert renderer.assistant_text() == "# 最终结论"


def test_renderer_streams_complete_reasoning_sentences_before_finish():
    renderer = TurnRenderer()

    first = renderer.consume(
        "thinking_delta", {"text": "正在分析。还有"}
    )
    remaining = renderer.consume("thinking_finished")

    assert first == []
    assert remaining == []


def test_renderer_streams_public_solution_text_after_hidden_reasoning():
    renderer = TurnRenderer()
    renderer.consume("thinking_delta", {"text": "内部分析"})
    renderer.consume("thinking_finished")

    first = renderer.consume("assistant_delta", {"text": "第一步：定位问题。"})

    assert first == ["第一步：定位问题。"]
    assert "内部分析" not in renderer.finish()


def test_renderer_preserves_markdown_split_across_stream_chunks():
    renderer = TurnRenderer()

    renderer.consume("assistant_delta", {"text": "**重要"})
    renderer.consume("assistant_delta", {"text": "** 和 __次要__"})

    assert renderer.assistant_text() == "**重要** 和 __次要__"


def test_renderer_sanitizes_protocol_and_raw_tool_call_tags():
    renderer = TurnRenderer()

    renderer.consume("assistant_delta", {"text": "[Advisor consultation #1] [Advisor review]\n"})
    renderer.consume("assistant_delta", {"text": "<tool_call id=\"call_123\" name=\"edit_file\"> {\"file_path\": \"test.py\"} </tool_call>\n"})
    renderer.consume("assistant_delta", {"text": "修改已完成。"})

    finish_output = renderer.finish()
    assert "[Advisor" not in finish_output
    assert "<tool_call" not in finish_output
    assert "修改已完成。" in finish_output


def test_renderer_preserves_code_block_with_think_tags_inside():
    """代码块内的 <think> 标签应作为代码内容保留，不应被当作推理内容丢弃。"""
    renderer = TurnRenderer()
    text = (
        "Here's the code:\n\n"
        "```python\n"
        "# <think>\n"
        "print(1)\n"
        "# </think>\n"
        "```\n"
    )
    for ch in text:
        renderer.consume("assistant_delta", {"text": ch})

    output = renderer.assistant_text()
    assert "```python" in output
    assert "# <think>" in output
    assert "print(1)" in output
    assert "# </think>" in output
    assert "```" in output


def test_renderer_streamed_fenced_code_block_is_recognized():
    """逐字符流式输入时，``` 应被正确识别为代码块边界。"""
    renderer = TurnRenderer()
    chunks = [
        "See ",
        "```python\n",
        "print(1)\n",
        "```\n",
    ]
    for chunk in chunks:
        renderer.consume("assistant_delta", {"text": chunk})

    output = renderer.assistant_text()
    assert "```python" in output
    assert "print(1)" in output
    assert output.count("```") == 2
