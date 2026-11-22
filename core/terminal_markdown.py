from __future__ import annotations

from io import StringIO
from typing import ClassVar

from rich import box
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import (
    CodeBlock,
    Heading,
    JustifyMethod,
    Markdown as _RichMarkdown,
    TableElement,
)
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from core.themes import get_theme


class _LeftHeading(Heading):

    LEVEL_ALIGN: ClassVar[dict[str, JustifyMethod]] = {
        "h1": "left",
        "h2": "left",
        "h3": "left",
        "h4": "left",
        "h5": "left",
        "h6": "left",
    }

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        text = self.text.copy()
        text.justify = "left"
        if self.tag == "h2":
            prefixed = Text("▸ ", style="markdown.h2")
            prefixed.append_text(text)
            prefixed.justify = "left"
            yield prefixed
            return
        yield text
        if self.tag == "h1":
            rule_len = min(options.max_width, max(12, text.cell_len + 4))
            yield Text("─" * rule_len, style="markdown.h1.rule")


class _ThemedCodeBlock(CodeBlock):

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        code = str(self.text).rstrip()
        bg_style = console.get_style("markdown.code_block")
        bg_color = bg_style.bgcolor.name if bg_style and bg_style.bgcolor else None
        syntax = Syntax(
            code,
            self.lexer_name,
            theme=self.theme,
            background_color=bg_color,
            word_wrap=True,
            padding=0,
        )
        lang_label = (self.lexer_name or "text").strip() or "text"
        yield Panel(
            syntax,
            box=box.HORIZONTALS,
            title=Text(lang_label, style="markdown.code.lang"),
            title_align="left",
            border_style="markdown.code.border",
            padding=(0, 1),
        )


class _ThemedTable(TableElement):

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        table = Table(
            box=box.ROUNDED,
            pad_edge=True,
            style="markdown.table.border",
            show_edge=True,
            collapse_padding=True,
        )
        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                heading = column.content.copy()
                heading.stylize("markdown.table.header")
                table.add_column(heading)
        if self.body is not None:
            for row in self.body.rows:
                table.add_row(*(element.content for element in row.cells))
        yield table


class _ThemedMarkdown(_RichMarkdown):
    elements = {
        **_RichMarkdown.elements,
        "heading_open": _LeftHeading,
        "fence": _ThemedCodeBlock,
        "code_block": _ThemedCodeBlock,
        "table_open": _ThemedTable,
    }


Markdown = _ThemedMarkdown


def _build_markdown_theme(theme_name: str | None = None) -> tuple[Theme, str]:
    t = get_theme(theme_name)
    theme = Theme({
        "markdown.paragraph": t["text"],
        "markdown.h1": f"bold {t['accent']}",
        "markdown.h1.rule": t["border_dim"],
        "markdown.h2": f"bold {t['accent_soft']}",
        "markdown.h3": f"bold {t['text']}",
        "markdown.h4": f"bold {t['text_soft']}",
        "markdown.code": f"{t['accent_soft']} on {t['code_bg']}",
        "markdown.code_block": f"{t['text']} on {t['code_bg']}",
        "markdown.code.lang": f"bold {t['accent_soft']}",
        "markdown.code.border": t["border_dim"],
        "markdown.link": f"underline {t['accent_soft']}",
        "markdown.block_quote": f"italic {t['text_soft']}",
        "markdown.item.bullet": t["accent"],
        "markdown.hr": t["border_dim"],
        "markdown.table.header": f"bold {t['accent']}",
        "markdown.table.border": t["border_dim"],
        "table.header": f"bold {t['accent']}",
        "table.border": t["border_dim"],
    })
    return theme, t.get("code_theme", "one-dark")


class MarkdownRenderError(RuntimeError):
    pass


def render_markdown_to_ansi(
    markdown_text: str,
    width: int,
    theme_name: str | None = None,
) -> str:
    if not isinstance(markdown_text, str):
        raise TypeError("markdown_text must be a string")
    if width < 20:
        raise ValueError("terminal width must be at least 20 columns")

    rich_theme, code_theme = _build_markdown_theme(theme_name)
    output = StringIO()
    console = Console(
        file=output,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
        theme=rich_theme,
    )
    try:
        renderable = Padding(
            Markdown(markdown_text, code_theme=code_theme, justify="left"),
            (0, 0, 0, 2),
        )
        console.print(renderable)
    except Exception as error:
        raise MarkdownRenderError(f"Markdown 渲染失败：{error}") from error
    return output.getvalue()
