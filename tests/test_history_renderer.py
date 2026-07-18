from rich.text import Text

from core.history_renderer import render_session_history


def plain(text: str) -> str:
    return Text.from_ansi(text).plain


def test_structured_history_replays_reasoning_and_markdown():
    history = [
        {"role": "user", "content": "检查项目"},
        {
            "role": "assistant",
            "content": "# 检查结果\n\n- 正常",
            "reasoning_content": "先查看项目结构",
        },
    ]

    rendered = plain(render_session_history(history, width=60))

    assert "检查项目" in rendered
    assert "先查看项目结构" in rendered
    assert "检查结果" in rendered
    assert "# 检查结果" not in rendered


def test_legacy_tagged_history_remains_compatible():
    history = [{
        "role": "assistant",
        "content": "<think>旧思考</think>旧回答",
    }]

    rendered = plain(render_session_history(history, width=60))

    assert "旧思考" in rendered
    assert "旧回答" in rendered
    assert "<think>" not in rendered


def test_history_replays_tool_call_without_raw_file_content():
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "read-1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"file_path":"core/llm.py"}',
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "read-1",
            "name": "read_file",
            "content": "secret source\nsecond line",
        },
    ]

    rendered = plain(render_session_history(history, width=60))

    assert "星瑶正在查看文件：core/llm.py" in rendered
    assert "星瑶已查看文件：core/llm.py（2 行）" in rendered
    assert "secret source" not in rendered
