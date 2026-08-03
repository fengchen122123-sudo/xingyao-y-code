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
    # 简洁模式不逐条显示工具行，但收尾摘要会统计读取的文件数
    assert "已读取 2 个文件" in output


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
            {"id": "3", "subject": "等待前置任务", "status": "blocked"},
        ]
    })

    output = Text.from_ansi(renderer.finish()).plain
    assert "✓ 检查代码" in output
    assert "■ 运行测试" in output
    # 回归：旧版 status_markers 表缺少 blocked 键，会静默退化成 pending 的 □
    assert "! 等待前置任务" in output


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
    # 简洁模式：工具摘要与原始输出都不逐条显示，只在无正文时打印收尾统计
    assert "星瑶已查看目录" not in output
    assert "main.py" not in output
    assert "core" not in output
    assert "已执行 1 项工具操作" in output


def test_renderer_explains_markdown_only_final_answer_after_tools():
    renderer = TurnRenderer()

    renderer.consume("tool_finished", {"id": "1", "name": "read_file", "ok": True})
    renderer.consume("assistant_delta", {"text": "**\n)\n"})

    output = renderer.finish()
    assert "检查完成：已读取 1 个文件。模型未返回最终结论。" in output


def test_renderer_uses_distinct_ansi_colors_for_tool_feedback():
    """简洁模式下工具调用静默（不打印 ✓/✗ 行），通用 error 事件仍打印黄色 !。"""
    renderer = TurnRenderer()

    finished_ok = "".join(renderer.consume("tool_finished", {
        "id": "1",
        "name": "read_file",
        "ok": True,
        "output_preview": "line one",
    }))
    finished_fail = "".join(renderer.consume("tool_finished", {
        "id": "2",
        "name": "read_file",
        "ok": False,
    }))
    failed = "".join(renderer.consume("error", {"message": "失败"}))

    # 工具行静默：成功/失败都不打印 ✓/✗
    assert finished_ok == ""
    assert finished_fail == ""
    assert "✓" not in finished_ok
    assert "✗" not in finished_fail
    # error 事件仍是黄色 !，且不泄漏原始错误文案
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


def test_renderer_strips_parenthesized_advisor_leak():
    """回归：圆括号形式的 advisor 内部说明（如 "(advisor returned no content)"）
    也会泄漏到正文，必须被清理。"""
    renderer = TurnRenderer()

    renderer.consume("assistant_delta", {"text": "(advisor returned no content)\n\n实际结论在这里。"})
    renderer.consume("assistant_delta", {"text": "完成。"})

    finish_output = renderer.finish()
    assert "advisor" not in finish_output.lower()
    assert "实际结论在这里。" in finish_output
    assert "完成。" in finish_output


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


def test_concise_mode_prints_working_header_once_for_parallel_tool_batch():
    """并行的一批工具调用只应打印一次"星瑶正在干活…"。

    回归：旧实现用 `self._completed_tool_count == 0 and not self._emitted`
    判断是否已打印过，但 `_emitted` 在每次 consume() 开头都会被清空，导致
    并行批次里每个 tool_requested 都会重新打印一遍。
    """
    renderer = TurnRenderer()

    first = "".join(renderer.consume("tool_requested", {"id": "1", "name": "read_file"}))
    second = "".join(renderer.consume("tool_requested", {"id": "2", "name": "read_file"}))
    third = "".join(renderer.consume("tool_requested", {"id": "3", "name": "read_file"}))

    assert "星瑶正在干活" in first
    assert second == ""
    assert third == ""


def test_concise_mode_hides_both_success_and_failure():
    """简洁模式下：成功和失败的工具调用都静默，失败数量由收尾统计行汇总。"""
    renderer = TurnRenderer()
    renderer.consume("tool_requested", {"id": "1", "name": "read_file", "preview": "a.py"})

    ok_output = "".join(renderer.consume("tool_finished", {
        "id": "1", "name": "read_file", "ok": True, "preview": "a.py",
    }))
    fail_output = "".join(renderer.consume("tool_finished", {
        "id": "2",
        "name": "execute_command",
        "ok": False,
        "summary": "命令执行失败：boom",
    }))

    assert ok_output == ""
    assert fail_output == ""
    assert "✗" not in fail_output


def test_concise_mode_end_of_turn_recap_counts_operations_silently():
    renderer = TurnRenderer()
    renderer.consume("tool_requested", {"id": "1", "name": "read_file"})
    renderer.consume("tool_finished", {"id": "1", "name": "read_file", "ok": True})
    renderer.consume("tool_requested", {"id": "2", "name": "execute_command"})
    renderer.consume("tool_finished", {
        "id": "2", "name": "execute_command", "ok": False, "summary": "失败了",
    })

    plain = Text.from_ansi(renderer.concise_recap()).plain
    # 简洁模式完全静默失败：只显示操作总数，不显示失败计数
    assert plain.strip() == "● 共执行 2 项操作"
    assert "失败" not in plain
    # 同一实例只应生成一次收尾行，避免 main.py 在多个终止分支里重复打印
    assert renderer.concise_recap() == ""


def test_concise_recap_omits_failures_completely():
    renderer = TurnRenderer()
    renderer.consume("tool_requested", {"id": "1", "name": "read_file"})
    renderer.consume("tool_finished", {"id": "1", "name": "read_file", "ok": True})

    plain = Text.from_ansi(renderer.concise_recap()).plain
    assert plain.strip() == "● 共执行 1 项操作"


def test_concise_recap_is_empty_when_no_tools_ran():
    idle_renderer = TurnRenderer()
    assert idle_renderer.concise_recap() == ""


def test_diff_ready_shows_one_line_summary():
    renderer = TurnRenderer()
    diff_text = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,3 +1,3 @@\n"
        " context\n"
        "-old line\n"
        "+new line one\n"
        "+new line two\n"
    )

    output = "".join(renderer.consume("diff_ready", {
        "file_path": "x.py",
        "text": diff_text,
    }))
    plain = Text.from_ansi(output).plain

    assert "✎ x.py" in plain
    assert "+2 行" in plain
    assert "-1 行" in plain
    assert "context" not in plain
    assert "old line" not in plain


def test_code_fence_after_holdback_prefix_does_not_corrupt_output():
    """回归：代码围栏前的文本递归处理后，若仍有被保守持留的 <think> 标签
    前缀尾部，旧实现会误把该尾部当成围栏本身切片，导致内容错位/丢字。
    修复后应把整段内容原样冲刷到最终输出（详见 turn_renderer._consume_text）。
    """
    renderer = TurnRenderer()
    chunk = "hello<thi```python\nprint(1)\n```\ndone"

    renderer.consume("assistant_delta", {"text": chunk})
    output = renderer.finish()

    assert output == chunk
