"""无边框窗口的原生窗口行为支持。

frameless 窗口（FormBorderStyle.None）默认丢失：边缘拖拽调整大小、Aero Snap、
最小化/最大化动画。本模块恢复它们：

1. 子类化顶层窗口（WinForms Form）：加回 WS_MINIMIZEBOX/WS_MAXIMIZEBOX 样式位
   （恢复最小化动画与任务栏交互），并对 WebView2 穿透上来的命中返回
   HTCAPTION（标题栏拖拽区）或边框码（边缘调整）。
2. 子类化 WebView2 子窗口：真实鼠标命中测试发生在子窗口上。命中在
   「标题栏拖拽区矩形」或「窗口边缘」内时返回 HTTRANSPARENT，让 Windows
   继续对父窗口做命中测试，父窗口再返回 HTCAPTION/边框码——系统随即接管，
   原生 resize / 拖拽 / Aero Snap / 动画全部生效。

线程安全：SetWindowLongPtrW 跨线程子类化不可靠，必须通过 form.Invoke 在
WinForms GUI 线程内执行（实测验证）。HTTRANSPARENT 只对同线程子窗口有效，
WebView2 子窗口与宿主同在 UI 线程，满足条件。

拖拽区矩形：前端把标题栏拖拽区在客户区内的矩形（物理像素）上报给
set_drag_region()。客户区坐标系下窗口移动不改变矩形，仅窗口 resize /
前端布局变化时需要更新。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger(__name__)

WM_NCHITTEST = 0x0084
HTCLIENT = 1
HTTRANSPARENT = -1
HTCAPTION = 2
HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
HTTOPLEFT, HTTOPRIGHT = 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

GWL_STYLE = -16
GWLP_WNDPROC = -4
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000

# 边缘可拖拽宽度（物理像素）
RESIZE_BORDER = 8

_WNDPROC = ctypes.WINFUNCTYPE(
    wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

_u32 = ctypes.windll.user32
_u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LPARAM]
_u32.SetWindowLongPtrW.restype = wintypes.LPARAM
_u32.CallWindowProcW.argtypes = [
    wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
_u32.CallWindowProcW.restype = wintypes.LPARAM
_u32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
_u32.SetWindowLongW.restype = wintypes.LONG
_u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.GetWindowLongW.restype = wintypes.LONG
_u32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]

# 拖拽区矩形（WebView2 客户区物理像素）：[x, y, w, h]
_drag_region: list[int] | None = None
# 已子类化窗口的回调引用（防 GC 回收导致窗口过程悬空）
_SUBCLASSED: dict[int, object] = {}


def _signed_short(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


def _edge_code(hwnd: int, lparam: int, border: int) -> int | None:
    """屏幕坐标命中 → 边框码；不在边缘返回 None。"""
    x = _signed_short(lparam & 0xFFFF)
    y = _signed_short((lparam >> 16) & 0xFFFF)
    rect = wintypes.RECT()
    if not _u32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    px, py = x - rect.left, y - rect.top
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    left, right = px < border, px >= width - border
    top, bottom = py < border, py >= height - border
    if top and left:
        return HTTOPLEFT
    if top and right:
        return HTTOPRIGHT
    if bottom and left:
        return HTBOTTOMLEFT
    if bottom and right:
        return HTBOTTOMRIGHT
    if left:
        return HTLEFT
    if right:
        return HTRIGHT
    if top:
        return HTTOP
    if bottom:
        return HTBOTTOM
    return None


def _in_drag_region(hwnd: int, lparam: int) -> bool:
    """屏幕坐标是否落在前端上报的标题栏拖拽区内（客户区坐标比较）。"""
    if _drag_region is None:
        return False
    point = wintypes.POINT(
        _signed_short(lparam & 0xFFFF),
        _signed_short((lparam >> 16) & 0xFFFF),
    )
    if not _u32.ScreenToClient(hwnd, ctypes.byref(point)):
        return False
    x, y, w, h = _drag_region
    return x <= point.x < x + w and y <= point.y < y + h


def find_webview_child_hwnds(parent_hwnd: int) -> list[int]:
    """枚举全部后代窗口（EnumChildWindows 递归），返回句柄列表。

    WebView2 的鼠标命中发生在最深层后代（Chrome_WidgetWin_0 常有嵌套层级），
    只子类化第一层不够；且页面加载时这些窗口会销毁重建，调用方需延迟重装。
    """
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        found.append(hwnd)
        return True

    _u32.EnumChildWindows(parent_hwnd, _cb, 0)
    return found


def set_drag_region(x: int, y: int, w: int, h: int) -> None:
    """前端上报标题栏拖拽区矩形（WebView2 客户区物理像素）。"""
    global _drag_region
    _drag_region = [int(x), int(y), int(w), int(h)]


def enable_native_window_behaviors(form, border: int = RESIZE_BORDER) -> bool:
    """启用 frameless 窗口原生行为：边缘 resize + HTCAPTION 拖拽（Snap/动画）。

    form 为 pywebview BrowserForm；必须在 GUI 线程外的调用线程里通过 form.Invoke
    送入执行。返回是否成功。幂等：重复调用只补样式位，不重复子类化。
    """
    if not hasattr(form, "Invoke") or form.Handle is None:
        return False
    hwnd = form.Handle.ToInt32()
    if hwnd == 0:
        return False

    state: dict = {"orig_parent": None, "ok": False, "error": None}
    # 每个子窗口的原过程按句柄记录（子类化多个后代，闭包共享）
    orig_children: dict = {}

    def _parent_wndproc(hwnd_arg, msg, wparam, lparam):
        # 顶层窗口：WebView2 穿透上来的命中 → HTCAPTION / 边框码
        if msg == WM_NCHITTEST:
            result = _u32.CallWindowProcW(state["orig_parent"], hwnd_arg, msg, wparam, lparam)
            if result == HTCLIENT:
                if _in_drag_region(hwnd_arg, lparam):
                    return HTCAPTION
                edge = _edge_code(hwnd_arg, lparam, border)
                if edge is not None:
                    return edge
            return result
        return _u32.CallWindowProcW(state["orig_parent"], hwnd_arg, msg, wparam, lparam)

    def _child_wndproc(hwnd_arg, msg, wparam, lparam):
        # WebView2 子窗口：拖拽区/边缘 → HTTRANSPARENT（穿透到父窗口继续命中测试）
        if msg == WM_NCHITTEST:
            if _in_drag_region(hwnd_arg, lparam):
                return HTTRANSPARENT
            edge = _edge_code(hwnd_arg, lparam, border)
            if edge is not None:
                return HTTRANSPARENT
        orig = orig_children.get(hwnd_arg)
        if orig is None:
            return 0  # 未记录原过程：保守放行默认
        return _u32.CallWindowProcW(orig, hwnd_arg, msg, wparam, lparam)

    # 保持引用防 GC
    parent_proc_ref = _WNDPROC(_parent_wndproc)
    child_proc_ref = _WNDPROC(_child_wndproc)

    def _do_install():
        try:
            # 1. 顶层窗口样式位：恢复最小化动画 / 双击最大化 / 任务栏交互
            style = _u32.GetWindowLongW(hwnd, GWL_STYLE)
            _u32.SetWindowLongW(hwnd, GWL_STYLE, style | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
            # 2. 顶层窗口子类化（幂等：同句柄只装一次）
            if hwnd not in _SUBCLASSED:
                state["orig_parent"] = _u32.SetWindowLongPtrW(
                    hwnd, GWLP_WNDPROC, ctypes.cast(parent_proc_ref, ctypes.c_void_p).value,
                )
                if state["orig_parent"] in (None, 0):
                    state["error"] = "parent SetWindowLongPtrW failed"
                    return
                _SUBCLASSED[hwnd] = parent_proc_ref
            # 3. WebView2 全部后代窗口子类化（命中测试发生在最深层后代，
            #    且页面加载时这些窗口可能销毁重建——新句柄在此幂等补装）
            children = find_webview_child_hwnds(hwnd)
            if not children:
                state["error"] = "no webview child hwnd"
                return
            installed_children = 0
            for child in children:
                if child in _SUBCLASSED:
                    installed_children += 1
                    continue
                orig = _u32.SetWindowLongPtrW(
                    child, GWLP_WNDPROC, ctypes.cast(child_proc_ref, ctypes.c_void_p).value,
                )
                if orig in (None, 0):
                    continue  # 单个后代失败不阻塞其它
                orig_children[child] = orig  # 记录该句柄的原过程，供 _child_wndproc 转发
                _SUBCLASSED[child] = child_proc_ref
                installed_children += 1
            state["children"] = installed_children
            state["ok"] = True
        except Exception as error:  # noqa: BLE001 - 安装失败绝不带出 GUI 线程
            state["error"] = str(error)

    try:
        from System.Windows import Forms as WinForms

        form.Invoke(WinForms.MethodInvoker(_do_install))
    except Exception as error:  # noqa: BLE001
        logger.warning("frameless 原生窗口行为启用失败: %s", error)
        return False

    if not state["ok"]:
        logger.warning("frameless 原生窗口行为启用失败: %s", state["error"])
        return False
    logger.info("frameless 原生窗口行为已启用 (parent=%s children=%s)", hwnd, state.get("children"))
    return True
