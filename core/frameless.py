"""无边框窗口的边缘调整大小 + Aero Snap 支持。

frameless 窗口（FormBorderStyle.None）默认丢失原生的边缘拖拽调整大小与
Aero Snap。本模块在 WinForms GUI 线程内用 SetWindowLongPtrW 子类化窗口过程，
拦截 WM_NCHITTEST 把命中测试从 HTCLIENT 改写为对应的边框/角落码，Windows
据此自动启用边缘调整大小与拖拽到屏幕边缘的 Snap。

线程安全：SetWindowLongPtrW 跨线程子类化不可靠，必须通过 form.Invoke 在创建
窗口的 GUI 线程内执行（实测验证）。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger(__name__)

WM_NCHITTEST = 0x0084
HTCLIENT = 1
HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
HTTOPLEFT, HTTOPRIGHT = 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

# 边缘可拖拽宽度（物理像素）
RESIZE_BORDER = 8

_WNDPROC = ctypes.WINFUNCTYPE(
    wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

_u32 = ctypes.windll.user32
# 64 位指针安全：显式声明参数/返回类型，避免指针截断或溢出
_u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LPARAM]
_u32.SetWindowLongPtrW.restype = wintypes.LPARAM
_u32.CallWindowProcW.argtypes = [
    wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
_u32.CallWindowProcW.restype = wintypes.LPARAM


def _signed_short(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


def _edge_hit(hwnd: int, lparam: int, border: int) -> int | None:
    """客户区坐标 → 边框命中码；命中边缘返回边框码，否则 None（交给默认处理）。"""
    x = _signed_short(lparam & 0xFFFF)
    y = _signed_short((lparam >> 16) & 0xFFFF)
    rect = wintypes.RECT()
    if not _u32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    px, py = x - rect.left, y - rect.top
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    left = px < border
    right = px >= width - border
    top = py < border
    bottom = py >= height - border
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


def subclass_frameless_resize(form, border: int = RESIZE_BORDER) -> bool:
    """在 WinForms GUI 线程内子类化 BrowserForm，拦截 WM_NCHITTEST。

    form 是 pywebview 的 BrowserForm（WinForms.Form 子类）实例。
    返回是否成功。任何失败都不影响窗口本身（只是没有边缘调整大小）。
    """
    if not hasattr(form, "Invoke") or form.Handle is None:
        return False
    hwnd = form.Handle.ToInt32()
    if hwnd == 0:
        return False

    state: dict = {"orig": None}

    @_WNDPROC
    def _wndproc(hwnd_arg, msg, wparam, lparam):
        if msg == WM_NCHITTEST:
            result = _u32.CallWindowProcW(state["orig"], hwnd_arg, msg, wparam, lparam)
            if result == HTCLIENT:
                edge = _edge_hit(hwnd_arg, lparam, border)
                if edge is not None:
                    return edge
            return result
        return _u32.CallWindowProcW(state["orig"], hwnd_arg, msg, wparam, lparam)

    done = {"ok": False, "error": None}

    def _do_subclass():
        GWLP_WNDPROC = -4
        try:
            proc_ptr = ctypes.cast(_wndproc, ctypes.c_void_p).value
            state["orig"] = _u32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, proc_ptr)
            done["ok"] = state["orig"] not in (None, 0)
            if not done["ok"]:
                done["error"] = f"SetWindowLongPtrW returned {state['orig']}"
        except Exception as error:  # noqa: BLE001 - 子类化失败绝不带出 GUI 线程
            done["error"] = str(error)

    try:
        from System.Windows import Forms as WinForms

        form.Invoke(WinForms.MethodInvoker(_do_subclass))
    except Exception as error:  # noqa: BLE001
        logger.warning("frameless 边缘调整大小子类化失败: %s", error)
        return False

    if not done["ok"]:
        logger.warning("frameless 边缘调整大小子类化失败: %s", done["error"])
        return False
    # 持有回调引用，防止被 GC 回收导致窗口过程悬空
    _SUBCLASSED[hwnd] = _wndproc
    logger.info("frameless 边缘调整大小已启用 (hwnd=%s)", hwnd)
    return True


# 已子类化的窗口回调引用注册表（防止 GC 回收 _wndproc 导致窗口过程悬空）
_SUBCLASSED: dict[int, object] = {}
