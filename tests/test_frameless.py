import copy
import ctypes
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import core.frameless as frameless
import gui_bridge
import gui_main
from core.gui_theme import BUILTIN_THEMES
from gui_bridge import ApiBridge


def test_suppress_frame_border_passes_dwm_color_none(monkeypatch):
    """防止把主题色重新画成可见的 DWM 外框。"""
    fake_dwm = SimpleNamespace(
        DwmExtendFrameIntoClientArea=Mock(return_value=0),
        DwmSetWindowAttribute=Mock(return_value=0),
    )
    monkeypatch.setattr(
        frameless.ctypes,
        "windll",
        SimpleNamespace(dwmapi=fake_dwm),
    )

    suppress = getattr(frameless, "suppress_frame_border", None)

    assert suppress is not None
    assert suppress(123) is True
    call = fake_dwm.DwmSetWindowAttribute.call_args
    assert call.args[0] == 123
    assert call.args[1] == frameless._DWMWA_BORDER_COLOR
    color = ctypes.cast(call.args[2], ctypes.POINTER(ctypes.c_uint32)).contents.value
    assert color == 0xFFFFFFFE
    assert call.args[3] == ctypes.sizeof(ctypes.c_uint32)
    assert fake_dwm.DwmSetWindowAttribute.argtypes == [
        frameless.wintypes.HWND,
        frameless.wintypes.DWORD,
        ctypes.c_void_p,
        frameless.wintypes.DWORD,
    ]
    assert fake_dwm.DwmSetWindowAttribute.restype is ctypes.c_long


def test_suppress_frame_border_requests_standard_rounded_corners(monkeypatch):
    """隐藏 DWM 外框时仍须显式保留 Windows 11 标准窗口圆角。"""
    fake_dwm = SimpleNamespace(
        DwmExtendFrameIntoClientArea=Mock(return_value=0),
        DwmSetWindowAttribute=Mock(return_value=0),
    )
    monkeypatch.setattr(
        frameless.ctypes,
        "windll",
        SimpleNamespace(dwmapi=fake_dwm),
    )
    monkeypatch.setattr(
        frameless,
        "sys",
        SimpleNamespace(getwindowsversion=lambda: SimpleNamespace(build=22631)),
        raising=False,
    )

    assert frameless.suppress_frame_border(123) is True
    corner_call = fake_dwm.DwmSetWindowAttribute.call_args_list[0]
    assert corner_call.args[0] == 123
    assert corner_call.args[1] == 33  # DWMWA_WINDOW_CORNER_PREFERENCE
    preference = ctypes.cast(
        corner_call.args[2],
        ctypes.POINTER(ctypes.c_int),
    ).contents.value
    assert preference == 2  # DWMWCP_ROUND
    assert corner_call.args[3] == ctypes.sizeof(ctypes.c_int)


def test_suppress_frame_border_clears_pywebview_extended_glass_margin(monkeypatch):
    """隐藏外框时必须撤销 pywebview 为 shadow=True 延伸进客户区的 1px frame。"""
    fake_dwm = SimpleNamespace(
        DwmExtendFrameIntoClientArea=Mock(return_value=0),
        DwmSetWindowAttribute=Mock(return_value=0),
    )
    monkeypatch.setattr(
        frameless.ctypes,
        "windll",
        SimpleNamespace(dwmapi=fake_dwm),
    )

    assert frameless.suppress_frame_border(123) is True
    call = fake_dwm.DwmExtendFrameIntoClientArea.call_args
    assert call is not None
    assert call.args[0] == 123
    margins = ctypes.cast(
        call.args[1],
        ctypes.POINTER(frameless._DWM_MARGINS),
    ).contents
    assert (
        margins.cxLeftWidth,
        margins.cxRightWidth,
        margins.cyTopHeight,
        margins.cyBottomHeight,
    ) == (0, 0, 0, 0)


def test_main_window_disables_pywebview_shadow_extension(monkeypatch):
    """窗口创建时应从源头禁用 pywebview 的 1px DWM glass margin。"""
    created = object()
    create_window = Mock(return_value=created)
    monkeypatch.setattr(gui_main, "webview", SimpleNamespace(create_window=create_window))
    create = getattr(gui_main, "_create_main_window", None)

    assert create is not None
    assert create(object(), "http://127.0.0.1", {"background": "#112233"}) is created
    assert create_window.call_args.kwargs["frameless"] is True
    assert create_window.call_args.kwargs["shadow"] is False


def test_suppress_frame_border_reports_unsupported_before_windows_11(monkeypatch):
    """Win10 不支持 DWMWA_BORDER_COLOR，必须显式返回 unsupported 而非假成功。"""
    fake_dwm = SimpleNamespace(
        DwmExtendFrameIntoClientArea=Mock(return_value=0),
        DwmSetWindowAttribute=Mock(return_value=0),
    )
    monkeypatch.setattr(
        frameless.ctypes,
        "windll",
        SimpleNamespace(dwmapi=fake_dwm),
    )
    monkeypatch.setattr(
        frameless,
        "sys",
        SimpleNamespace(getwindowsversion=lambda: SimpleNamespace(build=19045)),
        raising=False,
    )

    assert frameless.suppress_frame_border(123) is None
    fake_dwm.DwmSetWindowAttribute.assert_not_called()


def test_frameless_frame_reapply_hides_border_instead_of_repainting_it(monkeypatch):
    """窗口 shown/restored 后不得通过标题栏重放让 DWM 外框复活。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._window = SimpleNamespace(frameless=True)
    bridge._hwnd = 321
    bridge._browser_form = SimpleNamespace(Invoke=lambda callback: callback())
    bridge._native_titlebar = copy.deepcopy(BUILTIN_THEMES["builtin.light"]["native_titlebar"])
    suppressed = []
    native_writes = []
    monkeypatch.setattr(
        frameless,
        "suppress_frame_border",
        lambda hwnd: suppressed.append(hwnd) or True,
    )
    monkeypatch.setattr(
        bridge,
        "_write_native_titlebar_dwm",
        lambda value: native_writes.append(value) or True,
    )
    assert bridge._apply_titlebar_theme() is True
    assert suppressed == [321]
    assert native_writes == []


def test_frameless_theme_preview_does_not_paint_a_visible_resize_band(monkeypatch):
    """透明页面热区负责 resize 后，主题预览不应依赖宿主 Form 边带。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._hwnd = 321
    bridge._window = SimpleNamespace(frameless=True)
    monkeypatch.setattr(frameless, "suppress_frame_border", lambda _hwnd: True)

    titlebar = copy.deepcopy(BUILTIN_THEMES["builtin.light"]["native_titlebar"])
    result = bridge.preview_native_titlebar(titlebar)

    assert result["ok"] is True


def test_native_window_install_keeps_webview_full_bleed(monkeypatch):
    """透明页面热区接管 resize 后，原生安装不得再缩进 WebView 形成可见边带。"""
    class EventHook:
        def __iadd__(self, _callback):
            return self

    webview = SimpleNamespace(Dock="Fill", SetBounds=Mock())
    form = SimpleNamespace(
        Handle=SimpleNamespace(ToInt32=lambda: 123),
        Invoke=lambda callback: callback(),
        webview=webview,
        ClientSize=SimpleNamespace(Width=1200, Height=800),
        SizeChanged=EventHook(),
    )
    user32 = SimpleNamespace(
        GetWindowLongW=Mock(return_value=0),
        SetWindowLongW=Mock(return_value=0),
        SetWindowLongPtrW=Mock(return_value=99),
        CallWindowProcW=Mock(return_value=frameless.HTCLIENT),
    )
    dock_style = SimpleNamespace()
    setattr(dock_style, "None", "None")
    monkeypatch.setattr(frameless, "_u32", user32)
    monkeypatch.setattr(frameless, "_SUBCLASSED", {})
    monkeypatch.setattr(frameless, "_periodic_started", True)
    monkeypatch.setattr(frameless, "find_webview_child_hwnds", lambda _hwnd: [456])
    monkeypatch.setitem(
        sys.modules,
        "System.Windows",
        SimpleNamespace(Forms=SimpleNamespace(MethodInvoker=lambda callback: callback)),
    )
    monkeypatch.setitem(
        sys.modules,
        "System.Windows.Forms",
        SimpleNamespace(DockStyle=dock_style),
    )

    assert frameless.enable_native_window_behaviors(form) is True
    webview.SetBounds.assert_not_called()


def _make_fake_user32(cursor, monitor_rect, monitor=1):
    """构造 _cursor_snap_vk 所需的最小 user32 桩：光标坐标 + 所在显示器边界。"""

    def get_cursor_pos(point_ptr):
        pt = ctypes.cast(point_ptr, ctypes.POINTER(ctypes.wintypes.POINT)).contents
        pt.x, pt.y = cursor
        return True

    def get_monitor_info(_mon, mi_ptr):
        mi = ctypes.cast(mi_ptr, ctypes.POINTER(gui_bridge._MONITORINFO)).contents
        left, top, right, bottom = monitor_rect
        mi.rcMonitor.left = left
        mi.rcMonitor.top = top
        mi.rcMonitor.right = right
        mi.rcMonitor.bottom = bottom
        return True

    return SimpleNamespace(
        MonitorFromPoint=Mock(return_value=monitor),
        GetMonitorInfoW=Mock(side_effect=get_monitor_info),
        GetCursorPos=Mock(side_effect=get_cursor_pos),
    )


@pytest.mark.parametrize(
    ("cursor", "monitor_rect", "expected"),
    [
        # 第二屏在左边（负坐标）中间：原 bug 会误判 point.x <= 2 → 贴左边
        ((-1000, 500), (-1920, 0, 0, 1080), None),
        # 第二屏在上方（负 y）中间：原 bug 会误判 point.y <= 2 → 最大化
        ((500, -500), (0, -1080, 1920, 0), None),
        # 第二屏在右边中间：原 bug 会误判 point.x >= 主屏宽-2 → 贴右边
        ((2500, 500), (1920, 0, 3840, 1080), None),
        # 主屏正常贴左/右/上边缘
        ((0, 500), (0, 0, 1920, 1080), 0x25),
        ((1919, 500), (0, 0, 1920, 1080), 0x27),
        ((500, 0), (0, 0, 1920, 1080), 0x26),
        # 第二屏的真实边缘仍应正确触发
        ((3839, 500), (1920, 0, 3840, 1080), 0x27),
        ((-1919, 500), (-1920, 0, 0, 1080), 0x25),
    ],
)
def test_cursor_snap_vk_uses_monitor_bounds(monkeypatch, cursor, monitor_rect, expected):
    """多显示器下贴边判定必须相对光标所在显示器，而非主屏原点/宽度。"""
    monkeypatch.setattr(gui_bridge.sys, "platform", "win32")
    monkeypatch.setattr(
        gui_bridge.ctypes,
        "windll",
        SimpleNamespace(user32=_make_fake_user32(cursor, monitor_rect)),
    )

    assert gui_bridge._cursor_snap_vk() == expected


def test_frameless_preview_fails_when_dwm_border_cannot_be_hidden(monkeypatch):
    """Windows 11 的 DWM 写入失败不能被伪装成主题预览成功。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._window = SimpleNamespace(frameless=True)
    bridge._hwnd = 321
    bridge._browser_form = SimpleNamespace(Invoke=lambda callback: callback())
    monkeypatch.setattr(frameless, "suppress_frame_border", lambda _hwnd: False)

    result = bridge.preview_native_titlebar(
        copy.deepcopy(BUILTIN_THEMES["builtin.light"]["native_titlebar"]),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "native_titlebar_failed"


@pytest.mark.parametrize(
    ("edge", "size_code"),
    [
        ("left", 1),
        ("right", 2),
        ("top", 3),
        ("top_left", 4),
        ("top_right", 5),
        ("bottom", 6),
        ("bottom_left", 7),
        ("bottom_right", 8),
    ],
)
def test_window_begin_resize_starts_the_native_resize_loop(monkeypatch, edge, size_code):
    """页面透明热区必须通过 GUI 线程进入 Windows 原生八方向 resize 循环。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._hwnd = 321
    bridge._browser_form = SimpleNamespace(Invoke=lambda callback: callback())
    user32 = SimpleNamespace(
        ReleaseCapture=Mock(return_value=True),
        SendMessageW=Mock(return_value=0),
        IsZoomed=Mock(return_value=False),
    )
    monkeypatch.setattr(gui_bridge.sys, "platform", "win32")
    monkeypatch.setattr(gui_bridge.ctypes, "windll", SimpleNamespace(user32=user32))
    monkeypatch.setitem(
        sys.modules,
        "System.Windows",
        SimpleNamespace(Forms=SimpleNamespace(MethodInvoker=lambda callback: callback)),
    )

    begin_resize = getattr(bridge, "window_begin_resize", None)

    assert begin_resize is not None
    assert begin_resize(edge) == {"ok": True, "error": None}
    user32.SendMessageW.assert_called_once_with(321, 0x0112, 0xF000 | size_code, 0)


def test_window_begin_resize_rejects_maximized_window_inside_gui_thread(monkeypatch):
    """最大化检查必须发生在 GUI delegate 内，且不得启动系统 resize。"""
    in_invoke = {"value": False}

    def invoke(callback):
        in_invoke["value"] = True
        try:
            callback()
        finally:
            in_invoke["value"] = False

    def is_zoomed(_hwnd):
        assert in_invoke["value"] is True
        return True

    bridge = ApiBridge.__new__(ApiBridge)
    bridge._hwnd = 321
    bridge._browser_form = SimpleNamespace(Invoke=invoke)
    user32 = SimpleNamespace(
        ReleaseCapture=Mock(return_value=True),
        SendMessageW=Mock(return_value=0),
        IsZoomed=Mock(side_effect=is_zoomed),
    )
    monkeypatch.setattr(gui_bridge.sys, "platform", "win32")
    monkeypatch.setattr(gui_bridge.ctypes, "windll", SimpleNamespace(user32=user32))
    monkeypatch.setitem(
        sys.modules,
        "System.Windows",
        SimpleNamespace(Forms=SimpleNamespace(MethodInvoker=lambda callback: callback)),
    )

    result = bridge.window_begin_resize("right")

    assert result == {"ok": False, "error": "最大化窗口不能调整大小"}
    user32.ReleaseCapture.assert_not_called()
    user32.SendMessageW.assert_not_called()


def test_frameless_reapply_continues_after_a_transient_dwm_failure(monkeypatch):
    """一次 DWM 瞬态失败不得吞掉后续延迟重放机会。"""
    bridge = ApiBridge.__new__(ApiBridge)
    calls = []

    def apply():
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("transient DWM failure")
        return True

    monkeypatch.setattr(bridge, "_apply_titlebar_theme", apply)
    monkeypatch.setattr(gui_bridge.time, "sleep", lambda _delay: None)

    bridge._reapply_worker()

    assert len(calls) == 5


def test_frameless_startup_uses_complete_frame_application(monkeypatch):
    """句柄就绪后必须立即启用 resize 并清除全部 DWM frame 绘制。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._window = SimpleNamespace(title="Y-CODE", frameless=True)
    bridge._native_titlebar = copy.deepcopy(
        BUILTIN_THEMES["builtin.light"]["native_titlebar"],
    )
    calls = []
    monkeypatch.setattr(gui_bridge.sys, "platform", "win32")
    monkeypatch.setattr(bridge, "_find_own_hwnd", lambda _title: 456)
    monkeypatch.setattr(bridge, "_apply_window_icon", lambda: calls.append("icon"))
    monkeypatch.setattr(
        bridge,
        "_enable_frameless_resize",
        lambda: calls.append("resize"),
    )
    monkeypatch.setattr(
        bridge,
        "_apply_titlebar_theme",
        lambda: calls.append("frame") or True,
    )
    monkeypatch.setattr(frameless, "suppress_frame_border", lambda _hwnd: True)

    bridge._find_hwnd_worker()

    assert bridge._hwnd == 456
    assert calls == ["icon", "resize", "frame"]
