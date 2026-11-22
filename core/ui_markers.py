from __future__ import annotations

import re
import unicodedata

from core.themes import ansi_fg, get_theme

ANSI_RESET = "\x1b[0m"
ANSI_BOLD = "\x1b[1m"
ANSI_ITALIC = "\x1b[3m"
ANSI_GREEN = "\x1b[32m"
ANSI_RED = "\x1b[31m"
ANSI_YELLOW = "\x1b[33m"
ANSI_CYAN = "\x1b[36m"
ANSI_MAGENTA = "\x1b[35m"
ANSI_GRAY = "\x1b[90m"

MARK_DONE = "✓"
MARK_ACTIVE = "■"
MARK_FAILED = "✗"
MARK_BLOCKED = "!"
MARK_CANCELLED = "-"
MARK_PENDING = "□"

ICON_SPARK = "✦"
ICON_SUBAGENT = "◈"
ICON_PLAN = "❖"
ICON_GOAL = "◎"
ICON_PROMPT = "❯"
ICON_BULLET_ON = "●"
ICON_BULLET_OFF = "○"

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# core/plan.py VALID_STATUSES = {pending, in_progress, completed, failed, blocked, cancelled}。
# "done"/"active" 作为通用别名一并保留，兼容非 plan.py 场景（如通用任务状态展示）复用本表。
PLAN_MARKS = {
    "completed": MARK_DONE,
    "done": MARK_DONE,
    "in_progress": MARK_ACTIVE,
    "active": MARK_ACTIVE,
    "failed": MARK_FAILED,
    "blocked": MARK_BLOCKED,
    "cancelled": MARK_CANCELLED,
    "pending": MARK_PENDING,
}


def plan_mark(status: str) -> str:
    return PLAN_MARKS.get(str(status or "").lower(), MARK_PENDING)


# 状态 → 原始 ANSI 颜色码（历史兼容表，与 theme_ansi 语义对应）。
PLAN_COLORS = {
    "completed": ANSI_GREEN,
    "done": ANSI_GREEN,
    "in_progress": ANSI_CYAN,
    "active": ANSI_CYAN,
    "failed": ANSI_RED,
    "blocked": ANSI_YELLOW,
    "cancelled": ANSI_GRAY,
    "pending": ANSI_GRAY,
}

_PLAN_THEME_TOKENS = {
    "completed": "ok",
    "done": "ok",
    "in_progress": "accent",
    "active": "accent",
    "failed": "err",
    "blocked": "warn",
    "cancelled": "text_muted",
    "pending": "text_muted",
}


def theme_ansi(token: str, theme_name: str | None = None) -> str:
    """读取当前或指定主题语义令牌并转为 24-bit TrueColor ANSI 前景色序列。"""
    theme = get_theme(theme_name)
    color = theme.get(token) or theme["accent"]
    return ansi_fg(color)


def plan_color(status: str, theme_name: str | None = None) -> str:
    """按计划状态返回与当前主题协调的 ANSI 颜色序列。"""
    token = _PLAN_THEME_TOKENS.get(str(status or "").lower(), "text_muted")
    return theme_ansi(token, theme_name=theme_name)


PLAN_HTML_COLORS = {
    "completed": "ansigreen",
    "done": "ansigreen",
    "in_progress": "ansicyan",
    "active": "ansicyan",
    "failed": "ansired",
    "blocked": "ansiyellow",
    "cancelled": "ansigray",
    "pending": "ansigray",
}


def plan_html_color(status: str) -> str:
    return PLAN_HTML_COLORS.get(str(status or "").lower(), "ansigray")


def html_escape(text: object) -> str:
    """转义一切插入 prompt_toolkit HTML(...) 的外部/用户内容，防止标签解析错乱。"""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def progress_bar(
    completed: int,
    total: int,
    width: int = 10,
    theme_name: str | None = None,
) -> str:
    """生成主题主色进度条（如 ━━━━░░░░░░）。"""
    width = max(2, int(width))
    if total <= 0:
        filled = 0
    else:
        ratio = max(0.0, min(1.0, completed / total))
        filled = round(ratio * width)
        if completed > 0 and filled == 0:
            filled = 1
        if completed < total and filled == width:
            filled = width - 1
    empty = width - filled
    accent = theme_ansi("accent", theme_name)
    muted = theme_ansi("border_dim", theme_name)
    parts = []
    if filled:
        parts.append(f"{accent}{'━' * filled}{ANSI_RESET}")
    if empty:
        parts.append(f"{muted}{'━' * empty}{ANSI_RESET}")
    return "".join(parts)


def diff_stat_bar(
    added: int,
    removed: int,
    width: int = 5,
    theme_name: str | None = None,
) -> str:
    """生成紧凑的 diff 增删比例条（如 ++++-）。"""
    total = max(0, int(added)) + max(0, int(removed))
    if total <= 0 or width <= 0:
        return ""
    slots = min(width, total)
    plus_slots = round(max(0, added) / total * slots)
    if added > 0 and plus_slots == 0:
        plus_slots = 1
    if removed > 0 and plus_slots == slots and slots > 1:
        plus_slots = slots - 1
    minus_slots = slots - plus_slots
    ok_c = theme_ansi("ok", theme_name)
    err_c = theme_ansi("err", theme_name)
    out = []
    if plus_slots:
        out.append(f"{ok_c}{'+' * plus_slots}{ANSI_RESET}")
    if minus_slots:
        out.append(f"{err_c}{'-' * minus_slots}{ANSI_RESET}")
    return "".join(out)


def term_display_width(text: object, *, strip_html: bool = False) -> int:
    """按终端显示宽度测量字符串（East Asian 宽字符计 2 格，自动剥离 ANSI 序列）。"""
    raw = str(text or "")
    raw = _ANSI_ESCAPE_RE.sub("", raw)
    if strip_html:
        raw = _HTML_TAG_RE.sub("", raw)
        raw = raw.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in raw
    )


def pad_right(text: object, width: int, *, strip_html: bool = False) -> str:
    """按终端显示宽度右补空格到 width 格。"""
    s = str(text)
    return s + " " * max(0, width - term_display_width(s, strip_html=strip_html))


def pad_left(text: object, width: int) -> str:
    """按终端显示宽度左补空格到 width 格。"""
    s = str(text)
    return " " * max(0, width - term_display_width(s)) + s


def truncate_display(text: object, width: int, ellipsis: str = "…") -> str:
    """把文本按终端显示宽度裁剪到 width 以内（保留首尾、中间省略）。"""
    s = str(text)
    if width <= 0:
        return ""
    if term_display_width(s) <= width:
        return s
    ellipsis_w = term_display_width(ellipsis)
    if ellipsis_w >= width:
        return ellipsis[:width]
    budget = width - ellipsis_w
    head_budget = (budget + 1) // 2

    def _take(chars, limit):
        taken = []
        used = 0
        for ch in chars:
            cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if used + cw > limit:
                break
            taken.append(ch)
            used += cw
        return "".join(taken), used

    head, head_used = _take(s, head_budget)
    tail, _ = _take(reversed(s), budget - head_used)
    return head + ellipsis + "".join(reversed(tail))
