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
    created = object()
    create_window = Mock(return_value=created)
    monkeypatch.setattr(gui_main, "webview", SimpleNamespace(create_window=create_window))
    create = getattr(gui_main, "_create_main_window", None)

    assert create is not None
    assert create(object(), "http://127.0.0.1", {"background": "#112233"}) is created
    assert create_window.call_args.kwargs["frameless"] is True
    assert create_window.call_args.kwargs["shadow"] is False


def test_suppress_frame_border_reports_unsupported_before_windows_11(monkeypatch):
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
    # Win10 无 DWMWA_BORDER_COLOR 可隐藏边线：保留 1px 玻璃框延伸换 DWM 系统阴影
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
    ) == (1, 1, 1, 1)


def test_frameless_frame_reapply_hides_border_instead_of_repainting_it(monkeypatch):
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
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._hwnd = 321
    bridge._window = SimpleNamespace(frameless=True)
    monkeypatch.setattr(frameless, "suppress_frame_border", lambda _hwnd: True)

    titlebar = copy.deepcopy(BUILTIN_THEMES["builtin.light"]["native_titlebar"])
    result = bridge.preview_native_titlebar(titlebar)

    assert result["ok"] is True


def test_native_window_install_keeps_webview_full_bleed(monkeypatch):
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


def test_hit_test_leaves_titlebar_area_to_webview(monkeypatch):

    class EventHook:
        def __iadd__(self, _callback):
            return self

    def get_window_rect(_hwnd, rect_ptr):
        rect = ctypes.cast(rect_ptr, ctypes.POINTER(ctypes.wintypes.RECT)).contents
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1200, 800
        return True

    installed: dict[int, int] = {}

    def set_window_long_ptr(hwnd, _index, proc_addr):
        installed[hwnd] = proc_addr
        return 99

    webview = SimpleNamespace(Dock="Fill", SetBounds=Mock())
    form = SimpleNamespace(
        Handle=SimpleNamespace(ToInt32=lambda: 123),
        Invoke=lambda callback: callback(),
        webview=webview,
        ClientSize=SimpleNamespace(Width=1200, Height=800),
        SizeChanged=EventHook(),
    )
    def screen_to_client(_hwnd, point_ptr):
        return True

    user32 = SimpleNamespace(
        GetWindowLongW=Mock(return_value=0),
        SetWindowLongW=Mock(return_value=0),
        SetWindowLongPtrW=Mock(side_effect=set_window_long_ptr),
        CallWindowProcW=Mock(return_value=frameless.HTCLIENT),
        GetWindowRect=Mock(side_effect=get_window_rect),
        ScreenToClient=Mock(side_effect=screen_to_client),
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

    parent_proc = frameless._WNDPROC(installed[123])
    child_proc = frameless._WNDPROC(installed[456])
    lparam = (30 << 16) | 600
    assert parent_proc(123, frameless.WM_NCHITTEST, 0, lparam) == frameless.HTCLIENT
    assert child_proc(456, frameless.WM_NCHITTEST, 0, lparam) == frameless.HTCLIENT


def _make_fake_user32(cursor, monitor_rect, monitor=1):

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
        ((0, 500), (0, 0, 1920, 1080), 0x25),
        ((1919, 500), (0, 0, 1920, 1080), 0x27),
        ((500, 0), (0, 0, 1920, 1080), 0x26),
        ((3839, 500), (1920, 0, 3840, 1080), 0x27),
        ((-1919, 500), (-1920, 0, 0, 1080), 0x25),
        ((500, -1079), (0, -1080, 1920, 0), 0x26),
    ],
)
def test_cursor_snap_vk_uses_monitor_bounds(monkeypatch, cursor, monitor_rect, expected):
    monkeypatch.setattr(gui_bridge.sys, "platform", "win32")
    monkeypatch.setattr(
        gui_bridge.ctypes,
        "windll",
        SimpleNamespace(user32=_make_fake_user32(cursor, monitor_rect)),
    )

    assert gui_bridge._cursor_snap_vk() == expected


def test_frameless_preview_fails_when_dwm_border_cannot_be_hidden(monkeypatch):
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


def test_window_begin_drag_rejects_released_left_button(monkeypatch):
    in_gui_thread = {"value": False}
    checked_in_gui_thread = []

    def invoke(callback):
        in_gui_thread["value"] = True
        try:
            callback()
        finally:
            in_gui_thread["value"] = False

    def get_async_key_state(_vk):
        checked_in_gui_thread.append(in_gui_thread["value"])
        return 0x0000

    bridge = ApiBridge.__new__(ApiBridge)
    bridge._hwnd = 321
    bridge._browser_form = SimpleNamespace(Invoke=invoke)
    user32 = SimpleNamespace(
        GetAsyncKeyState=Mock(side_effect=get_async_key_state),
        ReleaseCapture=Mock(return_value=True),
        SendMessageW=Mock(return_value=0),
    )
    monkeypatch.setattr(gui_bridge.sys, "platform", "win32")
    monkeypatch.setattr(gui_bridge.ctypes, "windll", SimpleNamespace(user32=user32))
    monkeypatch.setitem(
        sys.modules,
        "System.Windows",
        SimpleNamespace(Forms=SimpleNamespace(MethodInvoker=lambda callback: callback)),
    )
    monkeypatch.setattr(gui_bridge, "_cursor_snap_vk", lambda: None)

    result = bridge.window_begin_drag()

    assert result == {"ok": False, "error": "left_button_released"}
    user32.ReleaseCapture.assert_not_called()
    user32.SendMessageW.assert_not_called()
    assert checked_in_gui_thread and all(checked_in_gui_thread)


def test_window_begin_drag_starts_native_move_when_left_button_down(monkeypatch):
    in_gui_thread = {"value": False}
    checked_in_gui_thread = []

    def invoke(callback):
        in_gui_thread["value"] = True
        try:
            callback()
        finally:
            in_gui_thread["value"] = False

    def get_async_key_state(_vk):
        checked_in_gui_thread.append(in_gui_thread["value"])
        return 0x8000

    bridge = ApiBridge.__new__(ApiBridge)
    bridge._hwnd = 321
    bridge._browser_form = SimpleNamespace(Invoke=invoke)
    user32 = SimpleNamespace(
        GetAsyncKeyState=Mock(side_effect=get_async_key_state),
        ReleaseCapture=Mock(return_value=True),
        SendMessageW=Mock(return_value=0),
    )
    monkeypatch.setattr(gui_bridge.sys, "platform", "win32")
    monkeypatch.setattr(gui_bridge.ctypes, "windll", SimpleNamespace(user32=user32))
    monkeypatch.setitem(
        sys.modules,
        "System.Windows",
        SimpleNamespace(Forms=SimpleNamespace(MethodInvoker=lambda callback: callback)),
    )
    monkeypatch.setattr(gui_bridge, "_cursor_snap_vk", lambda: None)

    result = bridge.window_begin_drag()

    assert result == {"ok": True, "error": None}
    user32.ReleaseCapture.assert_called_once()
    user32.SendMessageW.assert_called_once_with(321, 0x0112, 0xF012, 0)
    assert checked_in_gui_thread and all(checked_in_gui_thread)


def test_frameless_reapply_continues_after_a_transient_dwm_failure(monkeypatch):
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


def test_getminmaxinfo_clamps_maximize_to_monitor_work_area(monkeypatch):
    installed: dict[int, int] = {}

    def set_window_long_ptr(hwnd, _index, proc_addr):
        installed[hwnd] = proc_addr
        return 99

    def get_monitor_info(_mon, mi_ptr):
        mi = ctypes.cast(mi_ptr, ctypes.POINTER(frameless._MONITORINFO)).contents
        mi.rcMonitor.left, mi.rcMonitor.top = 0, -1080
        mi.rcMonitor.right, mi.rcMonitor.bottom = 1920, 0
        mi.rcWork.left, mi.rcWork.top = 0, -1080
        mi.rcWork.right, mi.rcWork.bottom = 1920, -40
        return True

    user32 = SimpleNamespace(
        GetWindowLongW=Mock(return_value=0),
        SetWindowLongW=Mock(return_value=0),
        SetWindowLongPtrW=Mock(side_effect=set_window_long_ptr),
        CallWindowProcW=Mock(return_value=frameless.HTCLIENT),
        GetWindowRect=Mock(return_value=True),
        MonitorFromWindow=Mock(return_value=2),
        GetMonitorInfoW=Mock(side_effect=get_monitor_info),
    )
    form = SimpleNamespace(
        Handle=SimpleNamespace(ToInt32=lambda: 123),
        Invoke=lambda callback: callback(),
    )
    monkeypatch.setattr(frameless, "_u32", user32)
    monkeypatch.setattr(frameless, "_SUBCLASSED", {})
    monkeypatch.setattr(frameless, "_periodic_started", True)
    monkeypatch.setattr(frameless, "find_webview_child_hwnds", lambda _hwnd: [456])
    monkeypatch.setitem(
        sys.modules,
        "System.Windows",
        SimpleNamespace(Forms=SimpleNamespace(MethodInvoker=lambda callback: callback)),
    )

    assert frameless.enable_native_window_behaviors(form) is True
    parent_proc = frameless._WNDPROC(installed[123])

    mmi = frameless._MINMAXINFO()
    mmi_ptr = ctypes.addressof(mmi)
    result = parent_proc(123, frameless.WM_GETMINMAXINFO, 0, mmi_ptr)
    assert result == 0

    assert (mmi.ptMaxPosition.x, mmi.ptMaxPosition.y) == (0, 0)
    assert (mmi.ptMaxSize.x, mmi.ptMaxSize.y) == (1920, 1040)
    user32.MonitorFromWindow.assert_called_once_with(123, 2)
