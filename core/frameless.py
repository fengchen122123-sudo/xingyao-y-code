from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    from ctypes import wintypes

    _DWORD = wintypes.DWORD
    _LONG = wintypes.LONG
    _BOOL = wintypes.BOOL
    _HWND = wintypes.HWND
    _UINT = wintypes.UINT
    _WPARAM = wintypes.WPARAM
    _LPARAM = wintypes.LPARAM
    _HANDLE = wintypes.HANDLE
    _RECT = wintypes.RECT
    _POINT = wintypes.POINT
else:

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    _DWORD = ctypes.c_uint32
    _LONG = ctypes.c_long
    _BOOL = ctypes.c_int
    _HWND = ctypes.c_void_p
    _UINT = ctypes.c_uint
    _WPARAM = ctypes.c_size_t
    _LPARAM = ctypes.c_ssize_t
    _HANDLE = ctypes.c_void_p

WM_NCHITTEST = 0x0084
WM_NCCALCSIZE = 0x0083
WM_GETMINMAXINFO = 0x0024
HTCLIENT = 1
HTTRANSPARENT = -1
HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
HTTOPLEFT, HTTOPRIGHT = 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

GWL_STYLE = -16
GWLP_WNDPROC = -4
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
# Windows 的 Aero Snap（键盘 Win+方向键与拖拽贴边）要求窗口带 WS_THICKFRAME，
# FormBorderStyle.None 把它剥掉了。加回后 DWM 同时恢复窗口阴影（无边框窗口
# 常配）；DefWindowProc 的默认命中测试也会在边缘返回边框码，与子类化叠加无害。
# 副作用：THICKFRAME 会让系统恢复原生外框；suppress_frame_border 用
# DWMWA_COLOR_NONE 禁止绘制外框，但不移除样式位（Win10 无此 API，
# 改为保留 1px 玻璃框延伸换 DWM 阴影）。
WS_THICKFRAME = 0x00040000

# DWM 窗口外观属性（Windows 11 22000+）
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34
_DWMWCP_ROUND = 2
_DWMWA_COLOR_NONE = 0xFFFFFFFE


class _DWM_MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", _DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", _DWORD),
    ]


class _MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", _POINT),
        ("ptMaxSize", _POINT),
        ("ptMaxPosition", _POINT),
        ("ptMinTrackSize", _POINT),
        ("ptMaxTrackSize", _POINT),
    ]

RESIZE_BORDER = 3

if sys.platform == "win32":
    _WNDPROC = ctypes.WINFUNCTYPE(_LPARAM, _HWND, _UINT, _WPARAM, _LPARAM)

    _u32 = ctypes.windll.user32
    _u32.SetWindowLongPtrW.argtypes = [_HWND, ctypes.c_int, _LPARAM]
    _u32.SetWindowLongPtrW.restype = _LPARAM
    _u32.CallWindowProcW.argtypes = [
        _LPARAM, _HWND, _UINT, _WPARAM, _LPARAM,
    ]
    _u32.CallWindowProcW.restype = _LPARAM
    _u32.SetWindowLongW.argtypes = [_HWND, ctypes.c_int, _LONG]
    _u32.SetWindowLongW.restype = _LONG
    _u32.GetWindowLongW.argtypes = [_HWND, ctypes.c_int]
    _u32.GetWindowLongW.restype = _LONG
    _u32.MonitorFromPoint.argtypes = [_POINT, _DWORD]
    _u32.MonitorFromPoint.restype = _HANDLE
    _u32.MonitorFromWindow.argtypes = [_HWND, _DWORD]
    _u32.MonitorFromWindow.restype = _HANDLE
    _u32.GetMonitorInfoW.argtypes = [_HANDLE, ctypes.POINTER(_MONITORINFO)]
    _u32.GetMonitorInfoW.restype = _BOOL
    _u32.GetWindowRect.argtypes = [_HWND, ctypes.POINTER(_RECT)]
    _u32.GetWindowRect.restype = _BOOL
else:
    _WNDPROC = None
    _u32 = None


_SUBCLASSED: dict[int, object] = {}
_periodic_started = False
_border_suppression_unsupported_logged = False


def _signed_short(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


def _edge_code(hwnd: int, lparam: int, border: int, ref_hwnd: int | None = None) -> int | None:
    if _u32 is None:
        return None
    x = _signed_short(lparam & 0xFFFF)
    y = _signed_short((lparam >> 16) & 0xFFFF)
    rect = _RECT()
    if not _u32.GetWindowRect(ref_hwnd or hwnd, ctypes.byref(rect)):
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


def find_webview_child_hwnds(parent_hwnd: int) -> list[int]:
    if sys.platform != "win32":
        return []
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        found.append(hwnd)
        return True

    _u32.EnumChildWindows(parent_hwnd, _cb, 0)
    return found


def suppress_frame_border(hwnd: int) -> bool | None:
    """禁止 DWM 绘制 WS_THICKFRAME 外框，并保留 Windows 11 标准圆角。

    返回 True 表示成功，False 表示 Windows 11 调用失败，None 表示系统版本
    早于 Windows 11 22000、公开 DWM API 不支持该能力（此时保留 1px 玻璃框
    延伸换取 Win10 DWM 系统阴影）。
    """
    # 测试会替换本模块的 sys 为仅含 getwindowsversion 的假对象；真实平台
    # 判断在缺省时按 win32 处理，避免属性缺失把受控窗口路径打断。
    if getattr(sys, "platform", "win32") != "win32":
        return False
    global _border_suppression_unsupported_logged
    if not hwnd:
        return False
    win11_or_later = sys.getwindowsversion().build >= 22000
    # pywebview 在 shadow=True 时会先把 DWM frame 向客户区延伸 1px；即使
    # DWMWA_BORDER_COLOR=NONE，这层 glass margin 仍会形成固定灰线。Win11 先把
    # 四边 margin 清零，再关闭 THICKFRAME 的 DWM 外框绘制。
    # Win10 没有 DWMWA_BORDER_COLOR 可隐藏边线，且 DWM 只给带玻璃框延伸的
    # 窗口画阴影：保留 1px 延伸换系统阴影（视觉边线由前端 os-win10 CSS 覆盖）。
    margin = 0 if win11_or_later else 1
    margins = _DWM_MARGINS(margin, margin, margin, margin)
    extend_frame = ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea
    extend_frame.argtypes = [wintypes.HWND, ctypes.POINTER(_DWM_MARGINS)]
    extend_frame.restype = ctypes.c_long
    hr = extend_frame(hwnd, ctypes.pointer(margins))
    if hr != 0:
        logger.warning("撤销 pywebview DWM frame 延伸失败 hr=0x%08X", hr & 0xFFFFFFFF)
        return False
    if not win11_or_later:
        if not _border_suppression_unsupported_logged:
            logger.warning("当前 Windows 版本不支持隐藏 DWM 窗口外框")
            _border_suppression_unsupported_logged = True
        return None
    color = ctypes.c_uint32(_DWMWA_COLOR_NONE)
    set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
    set_attribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_attribute.restype = ctypes.c_long
    corner_preference = ctypes.c_int(_DWMWCP_ROUND)
    hr = set_attribute(
        hwnd,
        _DWMWA_WINDOW_CORNER_PREFERENCE,
        ctypes.byref(corner_preference),
        ctypes.sizeof(corner_preference),
    )
    if hr != 0:
        logger.warning("启用 frameless 标准窗口圆角失败 hr=0x%08X", hr & 0xFFFFFFFF)
        return False
    hr = set_attribute(
        hwnd,
        _DWMWA_BORDER_COLOR,
        ctypes.byref(color),
        ctypes.sizeof(color),
    )
    if hr == 0:
        logger.info("frameless DWM 窗口外框已隐藏")
        return True
    logger.warning("隐藏 frameless DWM 窗口外框失败 hr=0x%08X", hr & 0xFFFFFFFF)
    return False


def enable_native_window_behaviors(form, border: int = RESIZE_BORDER) -> bool:
    if sys.platform != "win32":
        return False
    if not hasattr(form, "Invoke") or form.Handle is None:
        return False
    hwnd = form.Handle.ToInt32()
    if hwnd == 0:
        return False

    state: dict = {"orig_parent": None, "ok": False, "error": None}
    orig_children: dict = {}

    def _parent_wndproc(hwnd_arg, msg, wparam, lparam):
        if msg == WM_NCCALCSIZE and wparam:
            return 0
        if msg == WM_GETMINMAXINFO:
            try:
                hmon = _u32.MonitorFromWindow(hwnd_arg, 2)
                if hmon:
                    mi = _MONITORINFO()
                    mi.cbSize = ctypes.sizeof(_MONITORINFO)
                    if _u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                        mmi = ctypes.cast(
                            lparam, ctypes.POINTER(_MINMAXINFO),
                        ).contents
                        mmi.ptMaxPosition.x = mi.rcWork.left - mi.rcMonitor.left
                        mmi.ptMaxPosition.y = mi.rcWork.top - mi.rcMonitor.top
                        mmi.ptMaxSize.x = mi.rcWork.right - mi.rcWork.left
                        mmi.ptMaxSize.y = mi.rcWork.bottom - mi.rcWork.top
                        return 0
            except Exception as error:  # noqa: BLE001
                # 窗口过程绝不抛异常（打包版 stderr 为空会静默吞掉、极难排查）；
                # 记录后回退默认最大化，避免拖垮整个窗口消息循环。
                logger.error("WM_GETMINMAXINFO 处理失败，回退默认最大化: %s", error)
        if msg == WM_NCHITTEST:
            result = _u32.CallWindowProcW(state["orig_parent"], hwnd_arg, msg, wparam, lparam)
            if result == HTCLIENT:
                edge = _edge_code(hwnd_arg, lparam, border)
                if edge is not None:
                    return edge
            return result
        return _u32.CallWindowProcW(state["orig_parent"], hwnd_arg, msg, wparam, lparam)

    def _child_wndproc(hwnd_arg, msg, wparam, lparam):
        if msg == WM_NCHITTEST:
            if _edge_code(hwnd_arg, lparam, border, ref_hwnd=hwnd) is not None:
                return HTTRANSPARENT
        orig = orig_children.get(hwnd_arg)
        if orig is None:
            return 0
        return _u32.CallWindowProcW(orig, hwnd_arg, msg, wparam, lparam)

    parent_proc_ref = _WNDPROC(_parent_wndproc)
    child_proc_ref = _WNDPROC(_child_wndproc)

    def _do_install():
        """幂等安装：样式位 + 父/宿主侧后代子类化。
        可安全重复调用。"""
        try:
            style = _u32.GetWindowLongW(hwnd, GWL_STYLE)
            _u32.SetWindowLongW(
                hwnd, GWL_STYLE,
                style | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_THICKFRAME,
            )
            if hwnd not in _SUBCLASSED:
                state["orig_parent"] = _u32.SetWindowLongPtrW(
                    hwnd, GWLP_WNDPROC, ctypes.cast(parent_proc_ref, ctypes.c_void_p).value,
                )
                if state["orig_parent"] in (None, 0):
                    state["error"] = "parent SetWindowLongPtrW failed"
                    return
                _SUBCLASSED[hwnd] = parent_proc_ref
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
                    continue
                orig_children[child] = orig
                _SUBCLASSED[child] = child_proc_ref
                installed_children += 1
                logger.info("frameless 后代窗口已子类化: hwnd=%s（累计 %s）", child, installed_children)
            state["children"] = installed_children
            state["ok"] = True
        except Exception as error:  # noqa: BLE001 - 安装失败绝不带出 GUI 线程
            state["error"] = str(error)

    def _start_periodic_install():
        global _periodic_started
        if _periodic_started:
            return  # 已有周期补装在跑（loaded/restored 重装触发时防重入）
        _periodic_started = True
        try:
            timer = WinForms.Timer()

            def _tick(_sender, _args):
                _do_install()
                # 总 tick 上限：即使始终不 ok（深层窗口始终枚举不到的异常
                # 环境——恰是本定时器存在的场景）也要停表，不能无限空转。
                if not state.get("ok"):
                    if getattr(_tick, "count", 0) + 1 >= 60:
                        timer.Stop()
                        timer.Dispose()
                        logger.info(
                            "frameless 周期补装放弃: 90 秒内未成功（已子类化 %s 个窗口）",
                            len(_SUBCLASSED),
                        )
                    _tick.count = getattr(_tick, "count", 0) + 1
                    return
                if getattr(_tick, "count", 0) + 1 >= 60:
                    timer.Stop()
                    timer.Dispose()
                    logger.info(
                        "frameless 周期补装完成: 共子类化 %s 个窗口", len(_SUBCLASSED),
                    )
                _tick.count = getattr(_tick, "count", 0) + 1

            timer.Interval = 1500
            timer.Tick += _tick
            timer.Start()
            _SUBCLASSED[-1] = timer
        except Exception:
            logger.warning("frameless 周期补装定时器创建失败", exc_info=True)

    try:
        from System.Windows import Forms as WinForms

        form.Invoke(WinForms.MethodInvoker(lambda: (_do_install(), _start_periodic_install())))
    except Exception as error:  # noqa: BLE001
        logger.warning("frameless 原生窗口行为启用失败: %s", error)
        return False

    if not state["ok"]:
        logger.warning("frameless 原生窗口行为启用失败: %s", state["error"])
        return False
    logger.info("frameless 原生窗口行为已启用 (parent=%s children=%s)", hwnd, state.get("children"))
    return True
