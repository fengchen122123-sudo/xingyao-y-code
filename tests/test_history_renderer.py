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


def test_assistant_and_reasoning_ansi_escape_is_stripped():
    """assistant/reasoning 内容中的 ANSI 注入必须被剥离，防止回放劫持终端"""
    history = [{
        "role": "assistant",
        "content": "回答\x1b[2J\x1b[1;1H被清屏",
        "reasoning_content": "思考\x1b[2J内容",
    }]

    rendered = render_session_history(history, width=60)

    assert "\x1b[2J" not in rendered
    assert "\x1b[1;1H" not in rendered
    assert "被清屏" in plain(rendered)
    assert "思考" in plain(rendered)


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

    rendered = render_session_history(history, width=60)

    assert "\x1b[2J" not in rendered
    assert "evil" in plain(rendered)
