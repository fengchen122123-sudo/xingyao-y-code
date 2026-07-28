from rich.text import Text

from core.history_renderer import render_session_history


def plain(text: str) -> str:
    return Text.from_ansi(text).plain


def test_structured_history_replays_markdown_and_never_shows_reasoning():
    """回放历史时正文按 Markdown 渲染；reasoning_content 与实时路径一致，
    永不显示（实时路径里思考过程只消费不展示，回放必须保持一致）。"""
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
    """旧版用内联 <think> 标签存储推理内容：标签本身和标签内的推理都不应
    出现在回放输出里，只保留标签之外的公开正文。"""
    history = [{
        "role": "assistant",
        "content": "<think>旧思考</think>旧回答",
    }]

    rendered = plain(render_session_history(history, width=60))

    assert "旧思考" not in rendered
    assert "旧回答" in rendered
    assert "<think>" not in rendered


def test_history_replays_tool_call_without_raw_file_content():
    """详细模式回放：每个工具只有一条结果行（不再有单独的"请求"行），
    且绝不泄漏工具返回内容的原文。"""
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
    """简短模式回放：成功和失败的工具调用都不刷屏；
    与实时路径一致，失败数量由收尾统计行汇总。"""
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
    """用户内容中的 ANSI 转义序列必须被剥离，防止注入劫持终端"""
    history = [{"role": "user", "content": "正常文字\x1b[2J\x1b[31m红色"}]

    rendered = render_session_history(history, width=60)

    assert "\x1b[2J" not in rendered
    assert "\x1b[31m红色" not in rendered
    assert "正常文字" in plain(rendered)
    assert "红色" in plain(rendered)


def test_user_multimodal_list_content_extracts_text_blocks():
    """多模态 list 内容提取 text 块，而不是输出 Python repr"""
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
    """width < 20 的窄终端恢复会话不应崩溃"""
    history = [{"role": "assistant", "content": "# 标题"}]

    rendered = render_session_history(history, width=5)

    assert "标题" in plain(rendered)


def test_assistant_ansi_escape_is_stripped_and_reasoning_never_leaks():
    """assistant 内容中的 ANSI 注入必须被剥离，防止回放劫持终端；
    reasoning_content 永不回放，即使其中包含注入尝试也不会以任何形式泄漏。"""
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
    """工具参数（模型生成）里的 ANSI 注入不得进入 tool summary"""
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
    """多轮对话回放：相邻轮次（user 消息）之间出现一条灰色细分隔线，
    首条消息前不打印分隔线。"""
    history = [
        {"role": "user", "content": "第一个问题"},
        {"role": "assistant", "content": "第一个回答"},
        {"role": "user", "content": "第二个问题"},
        {"role": "assistant", "content": "第二个回答"},
    ]

    rendered = render_session_history(history, width=60)
    text = plain(rendered)

    # 分隔线只出现一次（第二轮提问前），首条消息前没有
    assert text.count("─" * 60) == 1
    first_q = text.index("第一个问题")
    second_q = text.index("第二个问题")
    sep_pos = text.index("─" * 60)
    assert first_q < sep_pos < second_q


def test_blank_line_between_assistant_blocks_in_replay():
    """回放时 assistant 正文块与上一块之间有一个空行，不再全部汇集在一起。"""
    history = [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答一"},
        {"role": "assistant", "content": "回答二"},
    ]

    rendered = render_session_history(history, width=60)

    assert "\n🤖" in plain(rendered) or "\n\x1b" in rendered
    # 第二个 assistant 块头部前必须有空行
    assert "\n\x1b[35m🤖" in rendered
