from core.turn_renderer import TurnRenderer


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


def test_renderer_reports_errors_once():
    renderer = TurnRenderer()

    renderer.consume("error", {"message": "命令失败"})
    renderer.consume("error", {"message": "命令失败"})

    assert renderer.finish().count("命令失败") == 1
