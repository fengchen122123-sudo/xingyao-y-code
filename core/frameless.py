"""无边框（frameless）窗口的原生窗口行为支持。

frameless 窗口（FormBorderStyle.None）默认丢失：边缘拖拽调整大小、Aero Snap、
最小化/最大化动画。本模块恢复它们，方案按实测约束分层：

1. 窗口样式位（GUI 线程内 SetWindowLong）：
   - WS_MINIMIZEBOX | WS_MAXIMIZEBOX：恢复最小化动画、双击最大化、任务栏交互
   - WS_THICKFRAME：Aero Snap 的硬前提（缺它 Win+方向键无效）；副作用是系统
     画原生外框；通过 DWMWA_COLOR_NONE 禁止绘制，并用 DWM 标准圆角偏好保留
     Windows 11 的窗口圆角，只保留 resize/Snap/阴影语义
2. WM_NCCALCSIZE 拦截（wParam=1 → 返回 0）：清空非客户区，消除 THICKFRAME
   无边框窗口顶部的黑色伪标题栏残留条
3. 宿主侧窗口子类化（WM_NCHITTEST）：仅边缘返回边框码。标题栏区域保持
   HTCLIENT 交给 WebView——实测 HTCAPTION 拦截从未承接交互（双击/拖动全部
   走 DOM 事件），只会在手势进行中间歇把 mousemove/mouseup 抖动路由为非
   客户区消息，导致快速拖动失效并闪现禁用光标。标题栏拖拽统一由前端
   mousedown + 位移阈值触发 window_begin_drag（SC_MOVE 原生移动循环），
   双击走前端 dblclick → SC_MAXIMIZE/SC_RESTORE，松手后按光标贴边补发
   Win+方向键触发系统 Aero Snap
4. 页面四边/四角的透明热区调用 window_begin_resize，WebView2 保持全铺，
   不再为了命中测试而露出可见宿主边带
5. 周期补装定时器（90 秒）：宿主侧后代窗口在页面渲染期间可能销毁重建，
   幂等补装子类化

线程安全：SetWindowLongPtrW 跨线程子类化不可靠，必须通过 form.Invoke 在
WinForms GUI 线程内执行（实测验证）。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)

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
# DWMWA_COLOR_NONE 禁止绘制外框，但不移除样式位。
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
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", wintypes.POINT),
        ("ptMaxSize", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("ptMinTrackSize", wintypes.POINT),
        ("ptMaxTrackSize", wintypes.POINT),
    ]

# 原生 WM_NCHITTEST 边缘命中宽度（物理像素）；页面层另有透明 CSS 热区，
# 深层 WebView2 窗口接管命中时也能进入相同的系统 resize 循环。
RESIZE_BORDER = 3

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
_u32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
_u32.MonitorFromPoint.restype = wintypes.HANDLE
_u32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_u32.MonitorFromWindow.restype = wintypes.HANDLE
_u32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
_u32.GetMonitorInfoW.restype = wintypes.BOOL
_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u32.GetWindowRect.restype = wintypes.BOOL


# 已子类化窗口的回调引用（防 GC 回收导致窗口过程悬空）
_SUBCLASSED: dict[int, object] = {}
# 周期补装定时器是否已启动（防 loaded/restored 重装触发多个定时器）
_periodic_started = False
_border_suppression_unsupported_logged = False


def _signed_short(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value >= 0x8000 else value


def _edge_code(hwnd: int, lparam: int, border: int, ref_hwnd: int | None = None) -> int | None:
    """屏幕坐标命中 → 边框码；不在边缘返回 None。

    ref_hwnd：边缘判定相对该窗口的 rect（默认 hwnd 自身）。子窗口命中时传
    父窗口句柄——子窗口几乎填满客户区，必须按父窗口边缘判定。
    """
    x = _signed_short(lparam & 0xFFFF)
    y = _signed_short((lparam >> 16) & 0xFFFF)
    rect = wintypes.RECT()
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


def suppress_frame_border(hwnd: int) -> bool | None:
    """禁止 DWM 绘制 WS_THICKFRAME 外框，并保留 Windows 11 标准圆角。

    返回 True 表示成功，False 表示 Windows 11 调用失败，None 表示系统版本
    早于 Windows 11 22000、公开 DWM API 不支持该能力。
    """
    global _border_suppression_unsupported_logged
    if not hwnd:
        return False
    # pywebview 在 shadow=True 时会先把 DWM frame 向客户区延伸 1px；即使
    # DWMWA_BORDER_COLOR=NONE，这层 glass margin 仍会形成固定灰线。先把
    # 四边 margin 清零，再关闭 THICKFRAME 的 DWM 外框绘制。
    margins = _DWM_MARGINS(0, 0, 0, 0)
    extend_frame = ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea
    extend_frame.argtypes = [wintypes.HWND, ctypes.POINTER(_DWM_MARGINS)]
    extend_frame.restype = ctypes.c_long
    hr = extend_frame(hwnd, ctypes.pointer(margins))
    if hr != 0:
        logger.warning("撤销 pywebview DWM frame 延伸失败 hr=0x%08X", hr & 0xFFFFFFFF)
        return False
    if sys.getwindowsversion().build < 22000:
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
    """启用 frameless 窗口原生行为：边缘 resize（标题栏保持 HTCLIENT 走 DOM）。

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
        # WM_NCCALCSIZE：THICKFRAME 无边框窗口顶部会残留一条黑色伪标题栏
        # （non-client 顶部条）。wParam=1 时返回 0 清空非客户区，黑线消失。
        if msg == WM_NCCALCSIZE and wparam:
            return 0
        # WM_GETMINMAXINFO：系统最大化（SC_MAXIMIZE/Aero Snap/Win+Up/双击标题栏）
        # 默认按 rcMonitor 全屏放置 frameless 窗口，会覆盖任务栏。这里把最大
        # 位置/尺寸钳制到窗口所在显示器的工作区（rcWork，排除任务栏），最大化
        # 仍走系统原生路径：动画、WebView2 布局、DPI、多屏全部系统接管。
        if msg == WM_GETMINMAXINFO:
            try:
                hmon = _u32.MonitorFromWindow(hwnd_arg, 2)  # MONITOR_DEFAULTTONEAREST
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
                        return 0  # 已定制，不再走默认
            except Exception as error:  # noqa: BLE001
                # 窗口过程绝不抛异常（打包版 stderr 为空会静默吞掉、极难排查）；
                # 记录后回退默认最大化，避免拖垮整个窗口消息循环。
                logger.error("WM_GETMINMAXINFO 处理失败，回退默认最大化: %s", error)
        # 顶层窗口：穿透上来的命中只在边缘返回边框码（顶部边缘也能 resize）。
        # 标题栏区域保持 HTCLIENT 交给 WebView（见模块 docstring 第 3 点）。
        if msg == WM_NCHITTEST:
            result = _u32.CallWindowProcW(state["orig_parent"], hwnd_arg, msg, wparam, lparam)
            if result == HTCLIENT:
                edge = _edge_code(hwnd_arg, lparam, border)
                if edge is not None:
                    return edge
            return result
        return _u32.CallWindowProcW(state["orig_parent"], hwnd_arg, msg, wparam, lparam)

    def _child_wndproc(hwnd_arg, msg, wparam, lparam):
        # WebView2 子窗口：仅边缘 → HTTRANSPARENT（穿透到父窗口继续命中测试）。
        # 边缘相对父窗口 rect 判定（子窗口填满客户区，自身 rect 不等于父边缘）；
        # 标题栏区域不拦截，保持 HTCLIENT 让 DOM 事件流完整到达前端。
        if msg == WM_NCHITTEST:
            if _edge_code(hwnd_arg, lparam, border, ref_hwnd=hwnd) is not None:
                return HTTRANSPARENT
        orig = orig_children.get(hwnd_arg)
        if orig is None:
            return 0  # 未记录原过程：保守放行默认
        return _u32.CallWindowProcW(orig, hwnd_arg, msg, wparam, lparam)

    # 保持引用防 GC
    parent_proc_ref = _WNDPROC(_parent_wndproc)
    child_proc_ref = _WNDPROC(_child_wndproc)

    def _do_install():
        """幂等安装：样式位 + 父/宿主侧后代子类化。
        可安全重复调用。"""
        try:
            # 1. 顶层窗口样式位：恢复最小化动画 / 双击最大化 / 任务栏交互 /
            #    Aero Snap（THICKFRAME 是 Snap 的硬前提，实测缺它 Win+Left 无效）
            style = _u32.GetWindowLongW(hwnd, GWL_STYLE)
            _u32.SetWindowLongW(
                hwnd, GWL_STYLE,
                style | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_THICKFRAME,
            )
            # 2. 顶层窗口子类化（幂等：同句柄只装一次）
            if hwnd not in _SUBCLASSED:
                state["orig_parent"] = _u32.SetWindowLongPtrW(
                    hwnd, GWLP_WNDPROC, ctypes.cast(parent_proc_ref, ctypes.c_void_p).value,
                )
                if state["orig_parent"] in (None, 0):
                    state["error"] = "parent SetWindowLongPtrW failed"
                    return
                _SUBCLASSED[hwnd] = parent_proc_ref
            # 3. 宿主侧后代窗口子类化（容器 / Chrome_WidgetWin_0 等，属本进程）
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
                logger.info("frameless 后代窗口已子类化: hwnd=%s（累计 %s）", child, installed_children)
            state["children"] = installed_children
            state["ok"] = True
        except Exception as error:  # noqa: BLE001 - 安装失败绝不带出 GUI 线程
            state["error"] = str(error)

    def _start_periodic_install():
        """GUI 线程定时器周期补装：深层 WebView2 后代（Chrome_WidgetWin_1 /
        RenderWidgetHostHWND）在页面实际渲染时才创建，比首屏 Vue 挂载晚得多，
        短稳定期判定会提前停手（实测漏装深层窗口）。固定每 1.5s 枚举补装，
        跑满 90 秒；EnumChildWindows 开销极小。"""
        global _periodic_started
        if _periodic_started:
            return  # 已有周期补装在跑（loaded/restored 重装触发时防重入）
        _periodic_started = True
        try:
            timer = WinForms.Timer()

            def _tick(_sender, _args):
                _do_install()
                if not state.get("ok"):
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
            # 持有引用防 GC
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
