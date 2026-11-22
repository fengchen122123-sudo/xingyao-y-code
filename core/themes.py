from __future__ import annotations

from typing import Any


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """把 #RRGGBB 或 #RGB 转为 (r, g, b) 三元组；异常值回落中性浅灰。"""
    cleaned = str(hex_color or "").strip().lstrip("#")
    if len(cleaned) == 3:
        cleaned = "".join(ch * 2 for ch in cleaned)
    if len(cleaned) != 6:
        return (200, 210, 220)
    try:
        return (
            int(cleaned[0:2], 16),
            int(cleaned[2:4], 16),
            int(cleaned[4:6], 16),
        )
    except ValueError:
        return (200, 210, 220)


def blend_hex(fg_hex: str, bg_hex: str, alpha: float) -> str:
    a = max(0.0, min(1.0, float(alpha)))
    fr, fg, fb = hex_to_rgb(fg_hex)
    br, bg, bb = hex_to_rgb(bg_hex)
    r = round(fr * a + br * (1.0 - a))
    g = round(fg * a + bg * (1.0 - a))
    b = round(fb * a + bb * (1.0 - a))
    return f"#{r:02x}{g:02x}{b:02x}"


def ansi_fg(hex_color: str) -> str:
    r, g, b = hex_to_rgb(hex_color)
    return f"\x1b[38;2;{r};{g};{b}m"


def ansi_bg(hex_color: str) -> str:
    r, g, b = hex_to_rgb(hex_color)
    return f"\x1b[48;2;{r};{g};{b}m"


def _make_theme(
    desc: str,
    accent: str,
    *,
    surface_bg: str = "#0c1014",
    menu_fg: str = "#081016",
    text: str = "#e4e9ee",
    text_soft: str = "#9aa7b4",
    text_muted: str = "#627080",
    ok: str = "#6bc48c",
    warn: str = "#d9a857",
    err: str = "#d97373",
    code_theme: str = "one-dark",
) -> dict[str, str]:
    accent_soft = blend_hex(accent, "#edf2f7", 0.36)
    accent_bg = blend_hex(accent, surface_bg, 0.16)
    border_dim = blend_hex(accent, surface_bg, 0.28)
    code_bg = blend_hex("#000000", surface_bg, 0.18)
    menu_bg = blend_hex(accent, surface_bg, 0.08)
    ok_bg = blend_hex(ok, surface_bg, 0.14)
    warn_bg = blend_hex(warn, surface_bg, 0.14)
    err_bg = blend_hex(err, surface_bg, 0.14)
    return {
        "desc": desc,
        "accent": accent,
        "accent_soft": accent_soft,
        "accent_bg": accent_bg,
        "surface_bg": surface_bg,
        "border_dim": border_dim,
        "text": text,
        "text_soft": text_soft,
        "text_muted": text_muted,
        "code_bg": code_bg,
        "menu_bg": menu_bg,
        "menu_fg": menu_fg,
        "menu_text": text,
        "menu_meta": text_muted,
        "ok": ok,
        "warn": warn,
        "err": err,
        "ok_bg": ok_bg,
        "warn_bg": warn_bg,
        "err_bg": err_bg,
        "code_theme": code_theme,
        # 兼容既有调用方的标准键名
        "border": accent,
        "label": f"bold {accent}",
        "status_main": f"bold {accent}",
        "status_text": text_soft,
        "status_sep": border_dim,
        "text_input": text,
        "menu_current": f"bg:{accent} {menu_fg} bold",
        "menu_current_meta": f"bg:{accent} {menu_fg}",
    }


THEMES: dict[str, dict[str, str]] = {
    "cyan": _make_theme(
        "极光青",
        "#00b4d8",
        surface_bg="#0c1014",
        menu_fg="#061218",
    ),
    "starlight": _make_theme(
        "星辰蓝",
        "#6ea8fe",
        surface_bg="#0d1017",
        menu_fg="#0a111c",
    ),
    "nord": _make_theme(
        "北欧蓝",
        "#88c0d0",
        surface_bg="#222730",
        menu_fg="#1d222b",
        text="#eceff4",
        text_soft="#b2bccb",
        text_muted="#6b768a",
        ok="#8fbc8f",
        warn="#ebcb8b",
        err="#d07a82",
        code_theme="nord",
    ),
    "dracula": _make_theme(
        "德古拉紫",
        "#bd93f9",
        surface_bg="#1b1c24",
        menu_fg="#181922",
        text="#eeeef4",
        text_soft="#a8a9bf",
        text_muted="#686b85",
        code_theme="dracula",
    ),
    "matrix": _make_theme(
        "骇客绿",
        "#4ade80",
        surface_bg="#080d09",
        menu_fg="#051008",
        text="#dfece2",
        text_soft="#8ea994",
        text_muted="#4c6652",
    ),
    "sunset": _make_theme(
        "落日橙",
        "#f29e5c",
        surface_bg="#15110e",
        menu_fg="#18100a",
        text="#f2eae4",
        text_soft="#b8a597",
        text_muted="#756357",
    ),
    "amber": _make_theme(
        "琥珀黄",
        "#e6c86e",
        surface_bg="#14130e",
        menu_fg="#16140a",
        text="#f0ede4",
        text_soft="#b4ae9c",
        text_muted="#706b5c",
    ),
    "lava": _make_theme(
        "熔岩红",
        "#e06c75",
        surface_bg="#150f10",
        menu_fg="#180b0c",
        text="#f2e8e9",
        text_soft="#b89fa2",
        text_muted="#735b5e",
    ),
    "cyberpunk": _make_theme(
        "赛博粉",
        "#e85d9e",
        surface_bg="#140e14",
        menu_fg="#160912",
        text="#f0e8ef",
        text_soft="#b4a0b2",
        text_muted="#6e5a6c",
    ),
    "white": _make_theme(
        "象牙白",
        "#e4e4e7",
        surface_bg="#111214",
        menu_fg="#111214",
        text="#ececef",
        text_soft="#a1a1aa",
        text_muted="#63636c",
    ),
}

THEME_DESCS: dict[str, tuple[str, str]] = {
    name: (cfg["desc"], cfg["accent"]) for name, cfg in THEMES.items()
}


def get_active_theme_name() -> str:
    """解析当前 CLI 生效主题名；优先读取运行时上下文，其次 config.MY_THEME。"""
    try:
        import core.app_context as app_context

        ctx = app_context.get()
        if ctx is not None:
            name = getattr(ctx, "current_theme", None)
            if isinstance(name, str) and name in THEMES:
                return name
    except Exception:
        pass
    try:
        import config

        name = getattr(config, "MY_THEME", "cyan")
        if isinstance(name, str) and name in THEMES:
            return name
    except Exception:
        pass
    return "cyan"


def get_theme(theme_name: str | None = None) -> dict[str, str]:
    """返回指定或当前生效的完整主题令牌字典，未知主题名安全回落 cyan。"""
    name = theme_name or get_active_theme_name()
    return THEMES.get(name, THEMES["cyan"])


def build_selector_style(theme_name: str | None = None) -> Any:
    """为全屏选择器（/models /session /skills /mcp）生成主题联动的 prompt_toolkit Style。"""
    from prompt_toolkit.styles import Style

    t = get_theme(theme_name)
    accent = t["accent"]
    accent_soft = t["accent_soft"]
    accent_bg = t["accent_bg"]
    text = t["text"]
    soft = t["text_soft"]
    muted = t["text_muted"]
    ok = t["ok"]
    warn = t["warn"]
    err = t["err"]
    cursor_line = f"bold {text} bg:{accent_bg}"
    return Style.from_dict({
        "header": f"bold {accent}",
        "footer": muted,
        "provider": f"bold {accent_soft}",
        "model.active": f"bold {accent}",
        "model.normal": soft,
        "model.cursor": cursor_line,
        "add.cursor": f"bold {accent} bg:{accent_bg}",
        "add.normal": accent_soft,
        "wiz.title": f"bold {accent}",
        "wiz.hint": muted,
        "proto.cursor": cursor_line,
        "proto.normal": soft,
        "proto.ok": f"bold {ok}",
        "input.label": f"bold {accent}",
        "input.sep": t["border_dim"],
        "input.field": text,
        "session.active": f"bold {accent}",
        "session.normal": soft,
        "session.cursor": cursor_line,
        "skill.active": f"bold {accent}",
        "skill.normal": soft,
        "skill.cursor": cursor_line,
        "mcp.on": f"bold {accent}",
        "mcp.off": soft,
        "mcp.cursor": cursor_line,
        "ok": f"bold {ok}",
        "warn": warn,
        "empty": warn,
        "err": err,
        "cursor-column": "",
        "cursor-line": "",
        "focused  cursor-column": f"bg:{accent_bg}",
    })
