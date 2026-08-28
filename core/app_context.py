"""
core/app_context.py
====================
运行时上下文中间层。

CLI (main.py) 和 GUI (gui_bridge.py) 在启动时各自注册一个上下文对象，
core/llm.py 及其他核心模块通过此模块统一访问运行时状态，
彻底消除对 `import main` 的直接依赖。
"""

from __future__ import annotations
import threading
from typing import TYPE_CHECKING, Any, Callable, Optional

_context: Optional[Any] = None  # 当前注册的上下文实例

# 模块级兜底状态：没有注册 ctx（如纯 headless / 单元测试）时依然可用，
# 注册了 ctx 时优先信任 ctx 的实时值。
_active_model_tuple: Optional[tuple] = None
_ui_flavor: str = "headless"
_current_session_fallback: str = ""
_manager_fallback: Optional[Any] = None
_permission_mode_fallback: str = "confirm"

# 规范权限模式集合：全系统只允许出现这四个值
# confirm=变更前确认(查询放行、变更询问) / accept_edits=自动编辑(普通变更放行、敏感变更询问)
# plan=计划模式(只读查询与规划) / full=完全访问(非灾难性操作由沙盒边界决定)
CANONICAL_PERMISSION_MODES = ("confirm", "accept_edits", "plan", "full")

# 历史遗留 / 中文标签 -> 规范值的映射表
_PERMISSION_MODE_ALIASES = {
    "confirm": "confirm",
    "default": "confirm",
    "ask": "confirm",
    "变更前确认": "confirm",
    "默认模式": "confirm",
    "accept_edits": "accept_edits",
    "accept-edits": "accept_edits",
    "acceptedits": "accept_edits",
    "edits": "accept_edits",
    "自动编辑": "accept_edits",
    "自动接受编辑": "accept_edits",
    "自动接受修改": "accept_edits",
    "plan": "plan",
    "planning": "plan",
    "计划模式": "plan",
    "full": "full",
    "yolo": "full",
    "all": "full",
    "完全访问": "full",
    "yolo (全部允许)": "full",
    "全部允许": "full",
}


def is_known_permission_mode(value: Any) -> bool:
    """判断给定值是否是可识别的权限模式（规范值或已登记的别名）。

    用于区分"用户选了合法模式"与"传了个拼错/未知的值"——后者不应被
    normalize 静默兜底成 confirm 后当作切换成功。
    """
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return (
        lowered in CANONICAL_PERMISSION_MODES
        or lowered in _PERMISSION_MODE_ALIASES
        or text in _PERMISSION_MODE_ALIASES
    )


def normalize_permission_mode(value: Any) -> str:
    """把任意来源的权限模式值规范化为四种标准权限模式之一。

    未知值（含已下线的历史 auto 模式）一律回落到最保守的 'confirm'，绝不放宽权限。
    """
    text = str(value or "").strip()
    if not text:
        return "confirm"
    lowered = text.lower()
    if lowered in CANONICAL_PERMISSION_MODES:
        return lowered
    return _PERMISSION_MODE_ALIASES.get(lowered, _PERMISSION_MODE_ALIASES.get(text, "confirm"))


# ---------------------------------------------------------------------------
# 注册 / 获取
# ---------------------------------------------------------------------------

def register(ctx: Any) -> None:
    """由 CLI 或 GUI 在启动时调用，注册当前运行环境的上下文对象。"""
    global _context
    _context = ctx


def get() -> Optional[Any]:
    return _context


# ---------------------------------------------------------------------------
# UI 形态（cli / gui / headless）
# ---------------------------------------------------------------------------

def set_ui_flavor(flavor: str) -> None:
    """由入口层声明当前 UI 形态，核心模块据此决定是否走终端交互路径。"""
    global _ui_flavor
    value = str(flavor or "headless").strip().lower()
    _ui_flavor = value if value in ("cli", "gui", "headless") else "headless"
    ctx = _context
    if ctx is not None:
        try:
            ctx.ui_flavor = _ui_flavor
        except Exception:
            pass


def get_ui_flavor() -> str:
    ctx = _context
    if ctx is not None:
        value = getattr(ctx, "ui_flavor", None)
        if value:
            return str(value)
    return _ui_flavor


# ---------------------------------------------------------------------------
# 当前激活模型（model, key, url）
# ---------------------------------------------------------------------------

def set_active_model_tuple(model: str, key: str, url: str) -> None:
    """入口层每次更新激活模型时调用，供 core 层（子代理等）读取。"""
    global _active_model_tuple
    _active_model_tuple = (model, key, url)


def get_active_model_tuple() -> Optional[tuple]:
    """返回 (model, key, url)；从未设置过时返回 None，由调用方自行兜底。"""
    return _active_model_tuple


# ---------------------------------------------------------------------------
# 权限相关
# ---------------------------------------------------------------------------

def get_permission_mode() -> str:
    """返回当前权限模式的规范值（confirm / accept_edits / plan / full）。"""
    ctx = _context
    if ctx is None:
        return normalize_permission_mode(_permission_mode_fallback)
    return normalize_permission_mode(getattr(ctx, "permission_mode", _permission_mode_fallback))


def set_permission_mode(value: Any) -> str:
    """写入权限模式（自动规范化），返回最终生效的规范值。"""
    global _permission_mode_fallback
    canonical = normalize_permission_mode(value)
    _permission_mode_fallback = canonical
    ctx = _context
    if ctx is not None:
        try:
            ctx.permission_mode = canonical
        except Exception:
            # ctx 的 permission_mode 可能是只读属性（由入口层自行维护），忽略即可
            pass
    return canonical


def get_permission_callback() -> Optional[Callable]:
    """返回 GUI 注册的权限回调，cli 模式下返回 None。"""
    ctx = _context
    if ctx is None:
        return None
    return getattr(ctx, "permission_callback", None)


# ---------------------------------------------------------------------------
# CLI App 实例
# ---------------------------------------------------------------------------

def get_app_instance() -> Optional[Any]:
    """返回 CLI 的 prompt_toolkit App 实例；GUI 模式下为 None。"""
    ctx = _context
    if ctx is None:
        return None
    return getattr(ctx, "app_instance", None)


def invalidate_ui() -> None:
    """通知 UI 重绘（仅 CLI 有效；GUI 通过事件驱动无需调用）。"""
    ctx = _context
    if ctx is not None and hasattr(ctx, "invalidate_ui"):
        ctx.invalidate_ui()


# ---------------------------------------------------------------------------
# 消息持久化
# ---------------------------------------------------------------------------

def get_manager() -> Optional[Any]:
    """返回 MemoryManager 实例，用于消息持久化。"""
    ctx = _context
    if ctx is not None:
        mgr = getattr(ctx, "manager", None)
        if mgr is not None:
            return mgr
    return _manager_fallback


def set_manager(manager: Any) -> None:
    """显式注册 MemoryManager（没有 ctx 或 ctx 不暴露 manager 时的兜底通道）。"""
    global _manager_fallback
    _manager_fallback = manager
    ctx = _context
    if ctx is not None:
        try:
            ctx.manager = manager
        except Exception:
            pass


def get_current_session() -> str:
    ctx = _context
    if ctx is not None:
        name = getattr(ctx, "current_session", "")
        if name:
            return str(name)
    return _current_session_fallback


def set_current_session(name: str) -> None:
    """入口层切换会话时调用，保证 core 层拿到的会话名始终最新。"""
    global _current_session_fallback
    _current_session_fallback = str(name or "")
    ctx = _context
    if ctx is not None:
        try:
            ctx.current_session = _current_session_fallback
        except Exception:
            # ctx 的 current_session 常为只读属性（代理入口层全局变量），忽略即可
            pass


# ---------------------------------------------------------------------------
# 线程会话绑定（多会话并行隔离的统一路由锚点）
# ---------------------------------------------------------------------------
# GUI 回合线程 / 子代理线程在入口处绑定自己的会话名，core 层的"每会话"状态
# （计划管理器、授权白名单、沙盒放行）据此路由到各自会话的存储，并行回合
# 互不可见、互不阻塞。未绑定线程（CLI 后台 worker 之外的主流程、测试主线程）
# 返回 None，调用方回落到进程级全局（与单会话时代行为一致）。
# 注意：get_current_session 是"UI 当前查看的会话"（进程级，会随用户切换变化），
# get_thread_session 是"本线程正在执行的回合所属会话"（线程级，回合期间稳定），
# 并行场景下所有会话归属判断必须用后者。
_thread_session_tls = threading.local()


def set_thread_session(session_id: str | None) -> None:
    """绑定/解绑当前线程的执行会话（回合入口绑定，finally 中传 None 解绑）。"""
    _thread_session_tls.session_id = (
        str(session_id).strip() if session_id else None
    )


def get_thread_session() -> str | None:
    return getattr(_thread_session_tls, "session_id", None)


# ---------------------------------------------------------------------------
# 任务状态
# ---------------------------------------------------------------------------

def set_task_status(val: str) -> None:
    ctx = _context
    if ctx is not None:
        ctx.task_status = val
