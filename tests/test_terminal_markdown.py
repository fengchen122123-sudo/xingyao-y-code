import pytest
from rich.text import Text

from core.terminal_markdown import MarkdownRenderError, render_markdown_to_ansi


def plain(ansi: str) -> str:
    return Text.from_ansi(ansi).plain


def test_markdown_removes_source_markers_and_keeps_structure():
    rendered = plain(render_markdown_to_ansi(
        "# 检查结果\n\n- 第一项\n- `core/llm.py`",
        width=60,
    ))

    assert "检查结果" in rendered
    assert "第一项" in rendered
    assert "core/llm.py" in rendered
    assert "# 检查结果" not in rendered
    assert "`core/llm.py`" not in rendered


def test_table_fits_requested_terminal_width():
    markdown = (
        "| 文件 | 职责 |\n"
        "| --- | --- |\n"
        "| core/llm.py | 模型调用和工具编排 |"
    )

    rendered = plain(render_markdown_to_ansi(markdown, width=48))
    lines = rendered.splitlines()

    assert "core/llm.py" in rendered
    assert "模型调用和工具编排" in rendered
    assert max(len(line) for line in lines) <= 48


def test_invalid_markdown_type_fails_early():
    with pytest.raises(TypeError, match="markdown_text must be a string"):
        render_markdown_to_ansi(None, width=60)


def test_markdown_parser_failure_is_explicit(monkeypatch):
    class BrokenMarkdown:
        def __init__(self, *args, **kwargs):
            raise ValueError("invalid document")

    monkeypatch.setattr("core.terminal_markdown.Markdown", BrokenMarkdown)

    with pytest.raises(MarkdownRenderError, match="Markdown 渲染失败"):
        render_markdown_to_ansi("# 标题", width=60)
