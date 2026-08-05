from core.turn_renderer import TurnRenderer
from rich.text import Text


def plain(lines):
    return Text.from_ansi("".join(lines)).plain


def tool_payload(
    tool_id,
    name,
    category,
    action,
    target,
    *,
    ok=True,
    duration_ms=100,
    command="",
    error_summary="",
    metrics=None,
):
    return {
        "id": tool_id,
        "name": name,
        "ok": ok,
        "duration_ms": duration_ms,
        "display": {
            "category": category,
            "action": action,
            "target": target,
            "command": command,
            "metrics": metrics or {},
            "error_summary": error_summary,
            "is_skill": False,
        },
    }


def read_payload(tool_id, path):
    return tool_payload(
        tool_id,
        "read_file",
        "read",
        "\u8bfb\u53d6\u6587\u4ef6",
        path,
        metrics={"line_count": 10},
    )


def command_payload(tool_id, command, *, ok):
    return tool_payload(
        tool_id,
        "execute_command",
        "command",
        "\u8fd0\u884c\u547d\u4ee4",
        command,
        ok=ok,
        command=command,
        error_summary="" if ok else "ModuleNotFoundError: pytest",
    )


def test_renderer_groups_reads_before_answer():
    renderer = TurnRenderer()
    for index in range(3):
        payload = read_payload(str(index), f"file-{index}.py")
        renderer.consume("tool_started", payload)
        renderer.consume("tool_finished", payload)

    group = plain(renderer.flush_tool_progress())
    assert group.count("\u5df2\u8bfb\u53d6") == 1
    assert "\u5df2\u8bfb\u53d6 3 \u4e2a\u6587\u4ef6" in group
    answer = "".join(renderer.consume("assistant_delta", {"text": "\u7ed3\u8bba"}))
    assert "\u7ed3\u8bba" in answer


def test_renderer_exposes_failed_then_active_command():
    renderer = TurnRenderer()
    failed = command_payload("bad", "pytest -q", ok=False)
    renderer.consume("tool_started", failed)

    output = plain(renderer.consume("tool_finished", failed))
    assert "\u8fd0\u884c\u547d\u4ee4\u5931\u8d25" in output

    corrected = command_payload("good", "python -m pytest -q", ok=True)
    renderer.consume("tool_started", corrected)
    assert renderer.active_tool_status().detail == "python -m pytest -q"


def test_renderer_flushes_pending_group_when_turn_finishes():
    renderer = TurnRenderer()
    payload = read_payload("read", "core/llm.py")
    renderer.consume("tool_started", payload)
    renderer.consume("tool_finished", payload)

    assert "\u5df2\u8bfb\u53d6 1 \u4e2a\u6587\u4ef6" in plain([renderer.finish()])


def test_renderer_forwards_diff_to_mutation_progress_without_duplication():
    renderer = TurnRenderer()
    mutation = tool_payload(
        "edit-1", "edit_file", "mutation", "\u4fee\u6539\u6587\u4ef6", "x.py"
    )
    renderer.consume("tool_started", mutation)
    diff = renderer.consume(
        "diff_ready",
        {"tool_call_id": "edit-1", "file_path": "x.py", "text": "-old\n+new\n"},
    )
    result = renderer.consume("tool_finished", mutation)

    assert "\u5df2\u4fee\u6539 x.py" in plain(diff)
    assert result == []


def test_renderer_uses_only_safe_display_fields_for_tool_progress():
    renderer = TurnRenderer()
    payload = command_payload("cmd", "python -m pytest -q", ok=True)
    payload["arguments"] = {"command": "secret command must stay hidden"}
    payload["result"] = {"content": "secret result must stay hidden"}

    renderer.consume("tool_started", payload)
    output = plain(renderer.consume("tool_finished", payload))

    assert "python -m pytest -q" in output
    assert "secret command" not in output
    assert "secret result" not in output


def test_renderer_does_not_leak_split_thinking_tags():
    renderer = TurnRenderer()
    renderer.consume("assistant_delta", {"text": "A <thi"})
    renderer.consume("assistant_delta", {"text": "nk>private</think>B"})

    assert renderer.finish() == "A B"


def test_renderer_reports_neutral_status_once_without_raw_error():
    renderer = TurnRenderer()
    renderer.consume("error", {"message": "raw failure"})
    renderer.consume("error", {"message": "raw failure"})

    output = renderer.finish()
    assert output.count("\u661f\u7476\u6682\u672a\u5b8c\u6210\u5f53\u524d\u6b65\u9aa4") == 1
    assert "raw failure" not in output


def test_renderer_shows_structured_plan_status():
    renderer = TurnRenderer()
    renderer.consume(
        "plan_updated",
        {"items": [
            {"subject": "check", "status": "completed"},
            {"subject": "run", "status": "in_progress"},
            {"subject": "wait", "status": "blocked"},
        ]},
    )

    output = plain([renderer.finish()])
    assert "\u2713 check" in output
    assert "\u25a0 run" in output
    assert "! wait" in output


def test_renderer_buffers_reasoning_and_streams_public_answer():
    renderer = TurnRenderer()
    renderer.consume("thinking_delta", {"text": "private"})
    renderer.consume("thinking_finished")

    answer = renderer.consume("assistant_delta", {"text": "public"})

    assert answer == ["public"]
    assert renderer.assistant_text() == "public"
    assert "private" not in renderer.finish()


def test_renderer_preserves_markdown_split_across_stream_chunks():
    renderer = TurnRenderer()
    renderer.consume("assistant_delta", {"text": "**important"})
    renderer.consume("assistant_delta", {"text": "** and __minor__"})

    assert renderer.assistant_text() == "**important** and __minor__"


def test_renderer_sanitizes_protocol_and_raw_tool_call_tags():
    renderer = TurnRenderer()
    renderer.consume("assistant_delta", {"text": "[Advisor review]"})
    renderer.consume("assistant_delta", {"text": '<tool_call id="1">raw</tool_call>'})
    renderer.consume("assistant_delta", {"text": "visible"})

    output = renderer.finish()
    assert "Advisor" not in output
    assert "tool_call" not in output
    assert "visible" in output


def test_renderer_preserves_code_block_with_think_tags_inside():
    renderer = TurnRenderer()
    text = "```python\n# <think>\nprint(1)\n# </think>\n```\n"
    for char in text:
        renderer.consume("assistant_delta", {"text": char})

    output = renderer.assistant_text()
    assert "# <think>" in output
    assert "# </think>" in output
    assert "print(1)" in output


def test_code_fence_after_holdback_prefix_does_not_corrupt_output():
    renderer = TurnRenderer()
    chunk = "hello<thi```python\nprint(1)\n```\ndone"
    renderer.consume("assistant_delta", {"text": chunk})

    assert renderer.finish() == chunk
