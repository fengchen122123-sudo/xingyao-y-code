from __future__ import annotations

import shutil
import sys
from types import SimpleNamespace

from prompt_toolkit.application import create_app_session
from prompt_toolkit.formatted_text import HTML, to_formatted_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.text import Text

import config
import main
from core.terminal_markdown import render_markdown_to_ansi
from core.themes import THEMES, THEME_DESCS, build_selector_style, get_theme
from core.ui_markers import (
    diff_stat_bar,
    plan_color,
    progress_bar,
    term_display_width,
    truncate_display,
)


def test_themes_contain_starlight_and_restrained_tokens():
    assert "starlight" in THEMES
    assert "starlight" in THEME_DESCS
    required_keys = {
        "accent",
        "accent_soft",
        "accent_bg",
        "surface_bg",
        "border",
        "border_dim",
        "text",
        "text_soft",
        "text_muted",
        "menu_bg",
        "menu_text",
        "menu_meta",
        "menu_current",
        "menu_current_meta",
        "ok",
        "warn",
        "err",
        "code_theme",
    }
    for name in THEMES:
        resolved = get_theme(name)
        assert required_keys.issubset(resolved.keys()), f"missing keys in theme {name}"
        assert resolved["accent"].startswith("#")
    assert get_theme("nonexistent_theme")["accent"] == THEMES["cyan"]["accent"]


def test_selector_styles_and_cli_styles_build_across_all_themes():
    main._CLI_STYLE_CACHE.clear()
    for name in THEMES:
        sel_style = build_selector_style(name)
        assert sel_style is not None
        cli_style = main._build_cli_style(name)
        assert cli_style is not None
        rules = dict(cli_style.style_rules)
        assert "bg:#0a0a0f" not in rules.get("input-field", "")
        assert "bg:#0a0a0f" not in rules.get("input-prefix", "")


def test_ui_markers_bars_width_and_truncation():
    bar_half = progress_bar(3, 6, width=10, theme_name="starlight")
    plain_bar = Text.from_ansi(bar_half).plain
    assert plain_bar == "━" * 10

    diff_bar = diff_stat_bar(8, 2, width=5, theme_name="cyan")
    plain_diff = Text.from_ansi(diff_bar).plain
    assert len(plain_diff) == 5
    assert "+" in plain_diff and "-" in plain_diff

    assert term_display_width("中文abc") == 7
    assert term_display_width("<style fg='#00b4d8'>中文</style>", strip_html=True) == 4
    assert term_display_width(plan_color("completed", "nord") + "✓\033[0m") == 1
    assert term_display_width(truncate_display("abcdefghijklmnopqrstuvwxyz", 10)) <= 10


def test_terminal_markdown_renders_with_active_theme():
    sample = "# 一级标题\n\n## 二级标题\n\n```python\nprint('hi')\n```\n\n| 列A | 列B |\n|---|---|\n| 1 | 2 |\n"
    for theme_name in ("cyan", "starlight", "nord", "white"):
        rendered = render_markdown_to_ansi(sample, width=80, theme_name=theme_name)
        plain = Text.from_ansi(rendered).plain
        assert "一级标题" in plain
        assert "▸ 二级标题" in plain
        assert "print('hi')" in plain
        code_line = next(line for line in plain.splitlines() if "print('hi')" in line)
        assert "│" not in code_line


def test_print_logo_preserves_pixel_avatar_and_adapts_columns(monkeypatch):
    captured = []
    monkeypatch.setattr(main, "print_formatted_text", lambda obj, *a, **k: captured.append(obj))
    monkeypatch.setattr(main, "get_active_model", lambda: {"name": "test-model", "reasoning_effort": "high"})

    for cols, expect_dual in ((100, True), (64, False)):
        captured.clear()
        monkeypatch.setattr(
            main.shutil,
            "get_terminal_size",
            lambda fallback=(100, 24), c=cols: SimpleNamespace(columns=c, lines=24),
        )
        main.print_logo(clear_screen=False)
        card_html = captured[0].value
        _ = to_formatted_text(HTML(card_html))
        plain = to_formatted_text(HTML(card_html))
        plain_str = "".join(frag[1] for frag in plain)

        for avatar_line in ("▄▄    ▄▄", "██████████", "██ █  █ ██", "▀██████▀"):
            assert avatar_line in plain_str

        if expect_dual:
            assert "常用指令" in plain_str
        else:
            assert "常用指令" not in plain_str

        widths = {
            term_display_width(line, strip_html=True)
            for line in card_html.splitlines()
            if line
        }
        assert len(widths) == 1


def test_confirm_card_risk_tiers_and_html_validity():
    ask_meta = main._confirm_card_meta({"function_name": "ask_user", "arguments": {"question": "ok?"}})
    assert ask_meta[0] == "class:frame"

    edit_meta = main._confirm_card_meta({"function_name": "edit_file", "arguments": {"file_path": "a.py"}})
    assert edit_meta[0] == "class:frame.warn"

    danger_meta = main._confirm_card_meta(
        {"function_name": "execute_command", "arguments": {"command": "rm -rf ./build"}}
    )
    assert danger_meta[0] == "class:frame.err"

    for fn, args in (
        ("execute_command", {"command": "echo <hello> & git status"}),
        ("ask_user", {"question": "A < B & C > D?"}),
        ("sandbox_outside_access", {"path": "C:\\outside\\file.txt"}),
    ):
        txt = main._confirm_prompt_text({
            "function_name": fn,
            "arguments": args,
            "options": ["同意", "拒绝"],
            "current_idx": 0,
        })
        frags = to_formatted_text(HTML(txt))
        assert "".join(f[1] for f in frags).strip()


def test_input_app_layout_preserves_activity_index_and_adds_subagent_panel(monkeypatch):
    fake_snaps = [
        SimpleNamespace(
            task_id="sub-1",
            parent_session=main.current_session,
            description="检索 auth 模块引用",
            subagent_type="research",
            plan_item_id=None,
            run_in_background=False,
            status="running",
            stage="tool_running",
            execution_elapsed=3.2,
            approval_wait_elapsed=0.0,
        ),
        SimpleNamespace(
            task_id="sub-done",
            parent_session=main.current_session,
            description="已完成项不应出现",
            subagent_type="research",
            plan_item_id=None,
            run_in_background=False,
            status="completed",
            stage="done",
            execution_elapsed=1.0,
            approval_wait_elapsed=0.0,
        ),
    ]
    fake_tools_mod = SimpleNamespace(subagent_manager=SimpleNamespace(snapshot=lambda: fake_snaps))
    monkeypatch.setitem(sys.modules, "core.tools", fake_tools_mod)

    active = main._active_subagent_snapshots()
    assert [s.task_id for s in active] == ["sub-1"]

    with create_pipe_input() as pipe_in:
        with create_app_session(input=pipe_in, output=DummyOutput()):
            app = main._make_input_app()
            root = app.layout.container.content
            activity_window = root.children[4].content
            assert activity_window.height == 2
            subagent_container = root.children[5]
            assert subagent_container.filter() is True
            input_vsplit = root.children[8]
            prefix_win = input_vsplit.children[0]
            assert prefix_win.width.min == 4
            input_win = input_vsplit.children[1]
            assert input_win.height.min == 1 and input_win.height.max == 5


def test_turn_output_uses_two_space_margin_and_in_order_queue(monkeypatch):
    rendered = render_markdown_to_ansi("正文测试", width=60, theme_name="cyan")
    plain_md = Text.from_ansi(rendered).plain
    assert plain_md.startswith("  正文测试")

    queued_ops = []
    printed = []

    class ActiveApp:
        loop = object()
        _is_running = True

        def invalidate(self):
            pass

    monkeypatch.setattr(main, "app_instance", ActiveApp())
    monkeypatch.setattr(main, "run_in_terminal", queued_ops.append, raising=False)
    monkeypatch.setattr(
        main,
        "print_formatted_text",
        lambda val, *a, **kw: printed.append(
            Text.from_ansi(val.value).plain
            if hasattr(val, "value") and "\x1b" in str(val.value)
            else ("".join(f[1] for f in to_formatted_text(val)) if isinstance(val, HTML) else str(val))
        ),
    )

    main.ui_renderers.clear()
    main._answer_header_printed.clear()
    main._turn_started_at.clear()
    main._assistant_stream_buffers.clear()

    turn_id = "turn-order-test"
    from core.turn_events import TurnEventFactory

    factory = TurnEventFactory(turn_id)
    main._render_ui_event(factory.emit("assistant_delta", {"text": "第一句回答\n\n"}))
    main._render_ui_event(factory.emit("turn_finished"))

    assert printed == []
    assert len(queued_ops) >= 3
    for op in queued_ops:
        op()

    combined = "\n".join(printed)
    idx_header = combined.index("  ✦ ")
    idx_body = combined.index("  第一句回答")
    idx_footer = combined.index("  ── 完成 · 用时")
    assert idx_header < idx_body < idx_footer


def test_context_watermark_levels_and_progress_helpers():
    assert main._context_watermark_style(25.0) == "class:status.token"
    assert main._context_watermark_style(65.0) == "class:status.ctx_warn"
    assert main._context_watermark_style(82.0) == "class:status.ctx_high"
    assert main._context_watermark_style(96.0) == "class:status.ctx_crit"

    assert len(main._context_bar_text(50.0, width=6)) == 6
    assert "▓" in main._context_bar_text(10.0, width=6)

    assert "+" in "".join(f[1] for f in to_formatted_text(HTML(main._diff_stat_html(4, 1))))
    assert "━" in "".join(f[1] for f in to_formatted_text(HTML(main._progress_bar_html(2, 5))))


def test_theme_command_switches_to_starlight(monkeypatch):
    printed = []
    monkeypatch.setattr(main, "print_formatted_text", lambda obj, *a, **k: printed.append(obj))
    monkeypatch.setattr(config, "save_config_value", lambda k, v: None)
    orig = main.current_theme
    try:
        main.handle_slash_command("/theme starlight")
        assert main.current_theme == "starlight"
        plain = "".join(
            "".join(f[1] for f in to_formatted_text(item))
            for item in printed
        )
        assert "✔ 已切换配色主题为: starlight" in plain
    finally:
        main.current_theme = orig
