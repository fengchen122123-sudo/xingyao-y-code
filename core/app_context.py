"""
core/app_context.py
====================
运行时上下文中间层。

TUI (main.py) 和 GUI (gui_bridge.py) 在启动时各自注册一个上下文对象，
core/llm.py 及其他核心模块通过此模块统一访问运行时状态，
彻底消除对 `import main` 的直接依赖。
"""

from __future__ import annotations
from typing import TYPE_CHECKING, Any, Callable, Optional

_context: Optional[Any] = None  # 当前注册的上下文实例


# ---------------------------------------------------------------------------
# 注册 / 获取
# ---------------------------------------------------------------------------

def register(ctx: Any) -> None:
    """由 TUI 或 GUI 在启动时调用，注册当前运行环境的上下文对象。"""
    global _context
    _context = ctx


def get() -> Optional[Any]:
    return _context


# ---------------------------------------------------------------------------
# 权限相关
# ---------------------------------------------------------------------------

def get_permission_mode() -> str:
    """返回当前权限模式字符串，如 'full'、'plan'、'default'。"""
    ctx = _context
    if ctx is None:
        return "default"
    return getattr(ctx, "permission_mode", "default")


def get_plan_approved() -> bool:
    ctx = _context
    if ctx is None:
        return False
    return bool(getattr(ctx, "plan_approved_this_turn", False))


def set_plan_approved(val: bool) -> None:
    ctx = _context
    if ctx is not None:
        ctx.plan_approved_this_turn = val


def get_permission_callback() -> Optional[Callable]:
    """返回 GUI 注册的权限回调，TUI 模式下返回 None。"""
    ctx = _context
    if ctx is None:
        return None
    return getattr(ctx, "permission_callback", None)


# ---------------------------------------------------------------------------
# TUI App 实例
# ---------------------------------------------------------------------------

def get_app_instance() -> Optional[Any]:
    """返回 TUI 的 prompt_toolkit App 实例；GUI 模式下为 None。"""
    ctx = _context
    if ctx is None:
        return None
    return getattr(ctx, "app_instance", None)


def invalidate_ui() -> None:
    """通知 UI 重绘（仅 TUI 有效；GUI 通过事件驱动无需调用）。"""
    ctx = _context
    if ctx is not None and hasattr(ctx, "invalidate_ui"):
        ctx.invalidate_ui()


# ---------------------------------------------------------------------------
# 消息持久化
# ---------------------------------------------------------------------------

def get_manager() -> Optional[Any]:
    """返回 MemoryManager 实例，用于消息持久化。"""
    ctx = _context
    if ctx is None:
        return None
    return getattr(ctx, "manager", None)


def get_current_session() -> str:
    ctx = _context
    if ctx is None:
        return ""
    return str(getattr(ctx, "current_session", ""))


# ---------------------------------------------------------------------------
# 任务状态
# ---------------------------------------------------------------------------

def set_task_status(val: str) -> None:
    ctx = _context
    if ctx is not None:
        ctx.task_status = val
