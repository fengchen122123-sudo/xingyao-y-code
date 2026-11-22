from rich.text import Text

from core.history_renderer import render_session_history


def plain(text: str) -> str:
    return Text.from_ansi(text).plain


def test_structured_history_replays_markdown_and_never_shows_reasoning():
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
    assert "先查看项目结构" not in rendered
    assert "检查结果" in rendered
    assert "# 检查结果" not in rendered


def test_legacy_tagged_history_remains_compatible():
    history = [{
        "role": "assistant",
        "content": "<think>旧思考</think>旧回答",
    }]

    rendered = plain(render_session_history(history, width=60))

    assert "旧思考" not in rendered
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

    rendered = plain(render_session_history(history, width=60, tool_verbose=True))

    assert "星瑶已查看文件：core/llm.py（2 行）" in rendered
    assert "secret source" not in rendered


def test_history_concise_mode_hides_all_tool_lines():
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "ok-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"file_path":"a.py"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "ok-1", "name": "read_file", "content": "print(1)"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "fail-1",
                "type": "function",
                "function": {"name": "execute_command", "arguments": '{"command":"boom"}'},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "fail-1",
            "name": "execute_command",
            "content": "🚨 command not found",
        },
        {"role": "assistant", "content": "已完成排查。"},
    ]

    rendered = plain(render_session_history(history, width=60, tool_verbose=False))

    assert "a.py" not in rendered
    assert "星瑶已查看文件" not in rendered
    assert "boom" not in rendered
    assert "command not found" not in rendered
    assert "✗" not in rendered
    assert "已完成排查" in rendered


def test_history_verbose_mode_shows_both_successful_and_failed_tool_lines():
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "ok-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"file_path":"a.py"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "ok-1", "name": "read_file", "content": "print(1)"},
    ]

    rendered = plain(render_session_history(history, width=60, tool_verbose=True))

    assert "星瑶已查看文件：a.py" in rendered


def test_user_content_ansi_escape_is_stripped():
    history = [{"role": "user", "content": "正常文字\x1b[2J\x1b[31m红色"}]

    rendered = render_session_history(history, width=60)

    assert "\x1b[2J" not in rendered
    assert "\x1b[31m红色" not in rendered
    assert "正常文字" in plain(rendered)
    assert "红色" in plain(rendered)


def test_user_multimodal_list_content_extracts_text_blocks():
    history = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ],
    }]

    rendered = plain(render_session_history(history, width=60))

    assert "看这张图" in rendered
    assert "image_url" not in rendered
    assert "[图片]" in rendered


def test_narrow_terminal_width_is_clamped():
    history = [{"role": "assistant", "content": "# 标题"}]

    rendered = render_session_history(history, width=5)

    assert "标题" in plain(rendered)


def test_assistant_ansi_escape_is_stripped_and_reasoning_never_leaks():
    history = [{
        "role": "assistant",
        "content": "回答\x1b[2J\x1b[1;1H被清屏",
        "reasoning_content": "思考\x1b[2J内容",
    }]

    rendered = render_session_history(history, width=60)

    assert "\x1b[2J" not in rendered
    assert "\x1b[1;1H" not in rendered
    assert "被清屏" in plain(rendered)
    assert "思考" not in plain(rendered)


def test_tool_target_ansi_escape_is_stripped():
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "w1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": '{"file_path":"evil\\u001b[2J.py","content":"x"}',
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "w1",
            "name": "write_file",
            "content": "ok",
        },
    ]

    rendered = render_session_history(history, width=60, tool_verbose=True)

    assert "\x1b[2J" not in rendered
    assert "evil" in plain(rendered)


def test_separator_between_turns_but_not_before_first_message():
    history = [
        {"role": "user", "content": "第一个问题"},
        {"role": "assistant", "content": "第一个回答"},
        {"role": "user", "content": "第二个问题"},
        {"role": "assistant", "content": "第二个回答"},
    ]

    rendered = render_session_history(history, width=60)
    text = plain(rendered)

    assert text.count("─" * 60) == 1
    first_q = text.index("第一个问题")
    second_q = text.index("第二个问题")
    sep_pos = text.index("─" * 60)
    assert first_q < sep_pos < second_q


def test_blank_line_between_assistant_blocks_in_replay():
    history = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答一"},
        {"role": "assistant", "content": "回答二"},
    ]

    rendered = render_session_history(history, width=60)
    plain_text = plain(rendered)

    assert "\n  ✦" in plain_text
    assert plain_text.count("✦ 星瑶") == 2
    assert "\n\n  ✦ 星瑶" in plain_text or "回答一\n\n  ✦ 星瑶" in plain_text


def test_history_replay_skips_system_injected_user_messages():
    history = [
        {"role": "user", "content": "真正的问题"},
        {
            "role": "user",
            "content": (
                "<task-notification>\n"
                "  <task-id>subagent-d66d7cca</task-id>\n"
                "  <status>timed_out</status>\n"
                "</task-notification>"
            ),
        },
        {"role": "user", "content": "计划尚未执行完毕，仍有可执行的计划项: ['P1'](x)。"},
        {"role": "assistant", "content": "回答"},
    ]

    rendered = plain(render_session_history(history, width=60))

    assert "真正的问题" in rendered
    assert "task-notification" not in rendered
    assert "计划尚未执行完毕" not in rendered
    assert "回答" in rendered
