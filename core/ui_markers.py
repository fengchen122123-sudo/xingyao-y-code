"""统一的终端标记与 ANSI 颜色常量（所有渲染路径共用）。

单一事实来源：turn_renderer（实时渲染）、history_renderer（历史回放）、
main.py（计划面板/命令输出）此前各自维护一份标记表，颜色和字符不一致
（如失败标记有的用 ✗ 红色，有的用 ! 黄色；blocked 状态有的直接丢失）。
所有渲染路径都应从本模块导入，禁止再本地硬编码颜色/标记。
"""

from __future__ import annotations

ANSI_RESET = "\x1b[0m"
ANSI_GREEN = "\x1b[32m"
ANSI_RED = "\x1b[31m"
ANSI_YELLOW = "\x1b[33m"
ANSI_CYAN = "\x1b[36m"
ANSI_MAGENTA = "\x1b[35m"
ANSI_GRAY = "\x1b[90m"

# 计划/工具状态标记（六态）
MARK_DONE = "✓"
MARK_ACTIVE = "■"
MARK_FAILED = "✗"
MARK_BLOCKED = "!"
MARK_CANCELLED = "-"
MARK_PENDING = "□"

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


# 状态 → 原始 ANSI 颜色码，供直接拼接 \x1b 转义序列的渲染路径使用
# （turn_renderer / history_renderer / main.py 里用 ANSI() 渲染的面板）。
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


def plan_color(status: str) -> str:
    return PLAN_COLORS.get(str(status or "").lower(), ANSI_GRAY)


# 状态 → prompt_toolkit HTML() 标签名，供 main.py 中用 HTML(...) 渲染的命令输出使用；
# 与 PLAN_COLORS 语义一一对应，只是 token 形式不同（HTML 标签名 vs 原始转义码）。
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
