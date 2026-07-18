from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.markdown import Markdown
from rich.theme import Theme


_MARKDOWN_THEME = Theme({
    "markdown.paragraph": "white",
    "markdown.h1": "bold white",
    "markdown.h2": "bold white",
    "markdown.h3": "bold white",
    "markdown.code": "cyan",
    "markdown.code_block": "white on #111111",
    "markdown.link": "cyan",
    "markdown.block_quote": "grey62",
    "markdown.item.bullet": "white",
    "table.header": "bold white",
    "table.border": "grey62",
})


class MarkdownRenderError(RuntimeError):
    pass


def render_markdown_to_ansi(markdown_text: str, width: int) -> str:
    if not isinstance(markdown_text, str):
        raise TypeError("markdown_text must be a string")
    if width < 20:
        raise ValueError("terminal width must be at least 20 columns")

    output = StringIO()
    console = Console(
        file=output,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
        theme=_MARKDOWN_THEME,
    )
    try:
        renderable = Markdown(markdown_text, code_theme="monokai", justify="left")
        console.print(renderable)
    except Exception as error:
        raise MarkdownRenderError(f"Markdown 渲染失败：{error}") from error
    return output.getvalue()
