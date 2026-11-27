from __future__ import annotations
import contextlib
import threading
from typing import Any, Callable, Optional

_context: Optional[Any] = None

# 模块级兜底状态：没有注册 ctx（如纯 headless / 单元测试）时依然可用，
# 注册了 ctx 时优先信任 ctx 的实时值。
_active_model_tuple: Optional[tuple] = None
_ui_flavor: str = "headless"
_current_session_fallback: str = ""
_manager_fallback: Optional[Any] = None
_permission_mode_fallback: str = "confirm"

# 规范权限模式集合：全系统只允许出现这五个值
# confirm=变更前确认(查询放行、变更询问) / accept_edits=自动编辑(普通变更放行、敏感变更询问)
# auto_decision=自动决策(权限与提问交由 JEV 决策；灾难性硬拒、越界/凭据仍确认)
# plan=计划模式(只读查询与规划) / full=完全访问(非灾难性操作由沙盒边界决定)
CANONICAL_PERMISSION_MODES = ("confirm", "accept_edits", "auto_decision", "plan", "full")

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
    "auto_decision": "auto_decision",
    "auto-decision": "auto_decision",
    "autodecision": "auto_decision",
    "jev": "auto_decision",
    "自动决策": "auto_decision",
    "自动决策模式": "auto_decision",
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
    """把任意来源的权限模式值规范化为五种标准权限模式之一。

    未知值（含已下线的历史 auto 模式）一律回落到最保守的 'confirm'，绝不放宽权限。
    """
    text = str(value or "").strip()
    if not text:
        return "confirm"
    lowered = text.lower()
    if lowered in CANONICAL_PERMISSION_MODES:
        return lowered
    return _PERMISSION_MODE_ALIASES.get(lowered, _PERMISSION_MODE_ALIASES.get(text, "confirm"))


def register(ctx: Any) -> None:
    """由 CLI 或 GUI 在启动时调用，注册当前运行环境的上下文对象。"""
    global _context
    _context = ctx


def get() -> Optional[Any]:
    return _context


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


def set_active_model_tuple(model: str, key: str, url: str) -> None:
    """入口层每次更新激活模型时调用，供 core 层（子代理等）读取。"""
    global _active_model_tuple
    _active_model_tuple = (model, key, url)


def get_active_model_tuple() -> Optional[tuple]:
    return _active_model_tuple


# ---------------------------------------------------------------------------
# 权限相关
# ---------------------------------------------------------------------------

# 线程级权限档位：多会话并行时每个回合线程绑定自己的会话档位，鉴权路径
# （get_permission_mode）据此判定，避免一个会话的档位污染其他会话。
_permission_mode_tls = threading.local()


def set_thread_permission_mode(mode: Any) -> None:
    """绑定当前线程的权限档位（回合入口调用；传 None 解绑回退全局）。"""
    if mode is None:
        _permission_mode_tls.mode = None
    else:
        _permission_mode_tls.mode = normalize_permission_mode(mode)


def get_thread_permission_mode() -> str | None:
    """返回当前线程绑定的权限档位；未绑定返回 None。"""
    return getattr(_permission_mode_tls, "mode", None)


@contextlib.contextmanager
def permission_mode_scope(mode: Any):
    previous = getattr(_permission_mode_tls, "mode", None)
    set_thread_permission_mode(mode)
    try:
        yield
    finally:
        _permission_mode_tls.mode = previous


# 线程级自动化回合标记：星轨自动化派发的回合线程绑定本次运行 id，工具 schema
# 层（core.llm）与执行层（core.harness.tool_pipeline）据此隐藏/拒绝写类自动化
# 工具。仅作为「本回合来源」标记，不承担跨线程权限判定（那是
# _automation_turn_labels 的职责）。
_automation_turn_tls = threading.local()


def set_thread_automation_turn(run_id: Any) -> None:
    """绑定当前线程的自动化回合标记（run_id 为空即解绑）。"""
    _automation_turn_tls.run_id = str(run_id or "") or None


def get_thread_automation_turn() -> str | None:
    """返回当前线程所属的自动化运行 id；非自动化回合返回 None。"""
    return getattr(_automation_turn_tls, "run_id", None)


@contextlib.contextmanager
def automation_turn_scope(run_id: Any):
    previous = getattr(_automation_turn_tls, "run_id", None)
    set_thread_automation_turn(run_id)
    try:
        yield
    finally:
        _automation_turn_tls.run_id = previous


def get_permission_mode() -> str:
    thread_mode = getattr(_permission_mode_tls, "mode", None)
    if thread_mode is not None:
        return _apply_managed_ceiling(normalize_permission_mode(thread_mode))
    ctx = _context
    if ctx is None:
        mode = normalize_permission_mode(_permission_mode_fallback)
    else:
        mode = normalize_permission_mode(
            getattr(ctx, "permission_mode", _permission_mode_fallback)
        )
    return _apply_managed_ceiling(mode)


def _apply_managed_ceiling(mode: str) -> str:
    """套用受管策略的权限模式上限（无策略/异常时原样返回）。"""
    try:
        # 延迟导入：本模块被大量模块导入，受管策略模块反向依赖本模块的规范化函数
        from core.managed_policy import clamp_permission_mode

        return clamp_permission_mode(mode)
    except Exception:
        return mode


def set_permission_mode(value: Any) -> str:
    global _permission_mode_fallback
    canonical = normalize_permission_mode(value)
    if getattr(_permission_mode_tls, "mode", None) is not None:
        _permission_mode_tls.mode = canonical
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
    ctx = _context
    if ctx is None:
        return None
    return getattr(ctx, "permission_callback", None)


def get_app_instance() -> Optional[Any]:
    ctx = _context
    if ctx is None:
        return None
    return getattr(ctx, "app_instance", None)


def invalidate_ui() -> None:
    """通知 UI 重绘（仅 CLI 有效；GUI 通过事件驱动无需调用）。"""
    ctx = _context
    if ctx is not None and hasattr(ctx, "invalidate_ui"):
        ctx.invalidate_ui()


def get_manager() -> Optional[Any]:
    ctx = _context
    if ctx is not None:
        mgr = getattr(ctx, "manager", None)
        if mgr is not None:
            return mgr
    return _manager_fallback


def set_manager(manager: Any) -> None:
    global _manager_fallback
    _manager_fallback = manager
    ctx = _context
    if ctx is not None:
        try:
            ctx.manager = manager
        except AttributeError:
            # ctx 的 manager 是只读 property（CLI/GUI 代理到各自全局状态），
            # 此时以 ctx 的实时值为准，fallback 仅作无 ctx 环境的兜底。
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


_thread_session_tls = threading.local()


def set_thread_session(session_id: str | None) -> None:
    """绑定/解绑当前线程的执行会话（回合入口绑定，finally 中传 None 解绑）。"""
    _thread_session_tls.session_id = (
        str(session_id).strip() if session_id else None
    )


def get_thread_session() -> str | None:
    return getattr(_thread_session_tls, "session_id", None)


@contextlib.contextmanager
def thread_session_scope(session_id: str | None):
    """线程内临时绑定执行会话，退出时恢复原值（支持嵌套）。"""
    previous = get_thread_session()
    set_thread_session(session_id)
    try:
        yield
    finally:
        set_thread_session(previous)


_thread_turn_source_tls = threading.local()


def set_thread_turn_source(source: str | None) -> None:
    """绑定/解绑当前线程的回合来源（如 local / lan / mcp）。"""
    _thread_turn_source_tls.turn_source = (
        str(source).strip().lower() if source else None
    )


def get_thread_turn_source() -> str | None:
    return getattr(_thread_turn_source_tls, "turn_source", None)


_thread_tool_call_tls = threading.local()


def set_thread_tool_call_id(call_id: str | None) -> None:
    """绑定/解绑当前线程正在执行的工具调用 id。"""
    _thread_tool_call_tls.call_id = str(call_id).strip() if call_id else None


def get_thread_tool_call_id() -> str | None:
    return getattr(_thread_tool_call_tls, "call_id", None)


@contextlib.contextmanager
def tool_call_scope(call_id: str | None):
    previous = getattr(_thread_tool_call_tls, "call_id", None)
    set_thread_tool_call_id(call_id)
    try:
        yield
    finally:
        _thread_tool_call_tls.call_id = previous


def set_task_status(val: str) -> None:
    ctx = _context
    if ctx is not None:
        ctx.task_status = val
