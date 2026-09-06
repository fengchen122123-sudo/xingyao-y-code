"""Windows 桌面 backend 与 Win32 SendInput adapter 测试（计划 Task 7 / 设计 13、21、22）。

所有测试只使用 tests.computer_fakes.FakeImage / FakeWin32Adapter 或注入
send_input 等全部底层 seam 的 Win32InputAdapter；pytest 运行期间绝不真实
移动鼠标、发送键盘或截取屏幕。scripts/smoke_computer_control.py 的
--desktop-observe / --desktop-input 人工探针不在 pytest 中执行。
"""

import pytest

from core.computer.models import ComputerAction, ComputerControlError, ComputerObservation
from core.computer.win32_adapter import (
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    KEY_NAME_TO_VK,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_WHEEL,
    KeyEventDescriptor,
    MouseEventDescriptor,
    Win32InputAdapter,
)
from core.computer.windows_backend import WindowsComputerBackend, screenshot_to_physical
from tests.computer_fakes import FakeImage, FakeWin32Adapter


def _capture_send_input(captured):
    """返回一个记录事件并全部接受的 send_input seam。"""

    def send_input(events):
        captured.extend(events)
        return len(events)

    return send_input


# ---------------- brief 样例（逐字） ----------------


def test_virtual_desktop_is_scaled_to_1920_and_coordinates_round_trip():
    image = FakeImage(width=3840, height=2160, origin_x=-1920, origin_y=0)
    adapter = FakeWin32Adapter()
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    observation = backend.observe("run-a")
    assert (observation.width, observation.height) == (1920, 1080)
    assert (observation.origin_x, observation.origin_y) == (-1920, 0)
    backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=960, y=540))
    assert adapter.clicks == [(0, 1080, "left")]


def test_sendinput_partial_result_is_error():
    adapter = Win32InputAdapter(send_input=lambda events: len(events) - 1)
    with pytest.raises(ComputerControlError) as raised:
        adapter.send_text("秘密")
    assert raised.value.code == "computer_input_rejected"


def test_pressed_keys_are_released_when_keypress_fails():
    adapter = FakeWin32Adapter(fail_after=2)
    with pytest.raises(ComputerControlError):
        adapter.send_keys(("CTRL", "SHIFT", "A"))
    assert adapter.keys_down == set()


# ---------------- 观察：缩放、原点、比例与受控摘要 ----------------


def test_observation_records_exact_scale_and_window_title():
    image = FakeImage(width=3840, height=2160, origin_x=-1920, origin_y=0)
    adapter = FakeWin32Adapter(foreground_title="记事本")
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    observation = backend.observe("run-a")
    assert (observation.original_width, observation.original_height) == (3840, 2160)
    assert (observation.scale_x, observation.scale_y) == (2.0, 2.0)
    assert observation.target == "desktop"
    assert observation.window_title == "记事本"
    assert image.resize_calls == [((1920, 1080), image.resize_calls[0][1])]
    assert observation.screen_sha256


def test_small_desktop_is_not_rescaled():
    image = FakeImage(width=1920, height=1080)
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    observation = backend.observe("run-a")
    assert (observation.width, observation.height) == (1920, 1080)
    assert (observation.scale_x, observation.scale_y) == (1.0, 1.0)
    assert image.resize_calls == []


def test_screenshot_size_must_match_win32_virtual_desktop():
    image = FakeImage(width=1920, height=1080)
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 3840, 2160))
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_backend_start_failed"


# ---------------- 坐标映射：负原点、多屏边界、bool 不透传 ----------------


def test_click_at_model_origin_maps_to_negative_virtual_desktop_origin():
    image = FakeImage(width=3840, height=2160, origin_x=-1920, origin_y=0)
    adapter = FakeWin32Adapter()
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    backend.observe("run-a")
    backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=0, y=0))
    assert adapter.clicks == [(-1920, 0, "left")]


def test_multi_monitor_last_model_pixel_stays_inside_virtual_desktop():
    image = FakeImage(width=3840, height=2160, origin_x=-1920, origin_y=0)
    adapter = FakeWin32Adapter()
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    backend.observe("run-a")
    backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=1919, y=1079))
    assert adapter.clicks == [(1918, 2158, "left")]


@pytest.mark.parametrize("x,y", [(-1, 0), (0, -1), (1920, 0), (0, 1080)])
def test_model_coordinates_outside_screenshot_half_open_range_are_rejected(x, y):
    image = FakeImage(width=3840, height=2160, origin_x=-1920, origin_y=0)
    adapter = FakeWin32Adapter()
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    backend.observe("run-a")
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=x, y=y))
    assert raised.value.code == "computer_coordinate_out_of_bounds"
    assert adapter.clicks == []


def test_bool_coordinates_are_not_passed_through_as_integers():
    image = FakeImage(width=1920, height=1080)
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    backend.observe("run-a")
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=True, y=0))
    assert raised.value.code == "computer_invalid_action"
    assert adapter.clicks == []


def test_screenshot_to_physical_uses_exact_recorded_ratio():
    observation = ComputerObservation(
        run_id="run-a",
        target="desktop",
        width=1920,
        height=1080,
        original_width=3840,
        original_height=2160,
        origin_x=-1920,
        origin_y=0,
        scale_x=2.0,
        scale_y=2.0,
    )
    assert screenshot_to_physical(960, 540, observation) == (0, 1080)
    assert screenshot_to_physical(0, 0, observation) == (-1920, 0)
    assert screenshot_to_physical(1919, 1079, observation) == (1918, 2158)
    with pytest.raises(ComputerControlError) as raised:
        screenshot_to_physical(True, 0, observation)
    assert raised.value.code == "computer_invalid_action"
    with pytest.raises(ComputerControlError) as raised:
        screenshot_to_physical(1920, 0, observation)
    assert raised.value.code == "computer_coordinate_out_of_bounds"


def test_execute_without_prior_observation_fails():
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=1, y=1))
    assert raised.value.code == "computer_invalid_action"
    assert adapter.clicks == []


# ---------------- 不可控桌面：secure desktop、无前台窗口、截图异常 ----------------


def test_secure_desktop_blocks_observe():
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080), desktop_name="Winlogon")
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_secure_desktop"


def test_secure_desktop_blocks_execute_after_successful_observe():
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    backend.observe("run-a")
    adapter.desktop_name = "Winlogon"
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=10, y=10))
    assert raised.value.code == "computer_secure_desktop"
    assert adapter.clicks == []


def test_missing_foreground_window_blocks_actions():
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080), has_foreground=False)
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_secure_desktop"
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="keypress", target="desktop", keys=("ENTER",)))
    assert raised.value.code == "computer_secure_desktop"
    assert adapter.keys_sent == []


def test_screenshot_failure_never_reuses_last_observation():
    calls = {"count": 0}

    def grab():
        calls["count"] += 1
        if calls["count"] > 2:  # start 试抓 + 第一次 observe 成功，第二次 observe 失败
            raise OSError("screen grab failed")
        return FakeImage(1920, 1080)

    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(image_grab=grab, win32_adapter=adapter)
    first = backend.observe("run-a")
    assert first.ok
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_backend_start_failed"
    # 旧观察不被改写、不被当作新结果返回。
    assert backend._last_observation is first


# ---------------- 桌面动作：wheel/hwheel、Unicode、double click、drag ----------------


def test_scroll_uses_wheel_and_hwheel():
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    backend.observe("run-a")
    backend.execute(
        "run-a",
        ComputerAction(kind="scroll", target="desktop", delta_x=2, delta_y=-3),
    )
    assert adapter.wheels == [-3]
    assert adapter.hwheels == [2]


def test_type_text_routes_unicode_text_to_clipboard():
    # 2026-09-06 行为变更：非 ASCII 文本改走剪贴板 + Ctrl+V（提速计划 Task 4）。
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    backend.observe("run-a")
    backend.execute("run-a", ComputerAction(kind="type_text", target="desktop", text="你好"))
    assert adapter.clipboard_writes == ["你好"]
    assert ("CTRL", "V") in [tuple(k) for k in adapter.keys_sent]
    assert adapter.texts == []


def test_double_click_sends_distinct_double_click():
    image = FakeImage(width=3840, height=2160, origin_x=-1920, origin_y=0)
    adapter = FakeWin32Adapter()
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    backend.observe("run-a")
    backend.execute("run-a", ComputerAction(kind="double_click", target="desktop", x=960, y=540))
    assert adapter.double_clicks == [(0, 1080, "left")]
    assert adapter.clicks == []


def test_drag_releases_button_in_finally_when_move_fails():
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080), fail_after=2)
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    backend.observe("run-a")
    with pytest.raises(ComputerControlError) as raised:
        backend.execute(
            "run-a",
            ComputerAction(
                kind="drag",
                target="desktop",
                path=((10, 10), (20, 20), (30, 30)),
            ),
        )
    assert raised.value.code == "computer_input_rejected"
    assert adapter.buttons_down == set()


def test_keypress_via_backend_releases_keys_on_success():
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    backend.observe("run-a")
    backend.execute("run-a", ComputerAction(kind="keypress", target="desktop", keys=("CTRL", "A")))
    assert adapter.keys_sent == [("CTRL", "A")]
    assert adapter.keys_down == set()


def test_release_inputs_delegates_to_adapter():
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    backend.release_inputs()
    assert adapter.release_all_calls == 1


def test_desktop_backend_rejects_browser_target_and_browser_only_actions():
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    backend.observe("run-a")
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="click", target="browser", x=1, y=1))
    assert raised.value.code == "computer_invalid_action"
    with pytest.raises(ComputerControlError) as raised:
        backend.execute(
            "run-a",
            ComputerAction(kind="navigate", target="desktop", url="https://example.com"),
        )
    assert raised.value.code == "computer_invalid_action"


# ---------------- Win32InputAdapter：事件形状与释放语义（全部注入 seam） ----------------


def test_send_text_uses_unicode_key_events():
    captured = []
    adapter = Win32InputAdapter(send_input=_capture_send_input(captured))
    adapter.send_text("密")
    assert captured == [
        KeyEventDescriptor(vk=0, scan=ord("密"), flags=KEYEVENTF_UNICODE),
        KeyEventDescriptor(vk=0, scan=ord("密"), flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]


def test_send_text_splits_surrogate_pairs_into_utf16_units():
    captured = []
    adapter = Win32InputAdapter(send_input=_capture_send_input(captured))
    adapter.send_text("😀")  # U+1F600 -> D83D DE00
    scans = [(event.scan, bool(event.flags & KEYEVENTF_KEYUP)) for event in captured]
    assert scans == [(0xD83D, False), (0xD83D, True), (0xDE00, False), (0xDE00, True)]
    assert all(event.flags & KEYEVENTF_UNICODE for event in captured)


def test_mouse_move_uses_absolute_virtual_desktop_normalization():
    metrics = {76: -1920, 77: 0, 78: 3840, 79: 2160}
    captured = []
    adapter = Win32InputAdapter(
        send_input=_capture_send_input(captured),
        get_system_metrics=lambda index: metrics[index],
    )
    adapter.send_mouse(action="move", x=0, y=1080)
    assert captured == [
        MouseEventDescriptor(
            x=round((0 - -1920) * 65535 / (3840 - 1)),
            y=round((1080 - 0) * 65535 / (2160 - 1)),
            flags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            data=0,
        )
    ]


def test_mouse_move_rechecks_virtual_desktop_bounds_before_sendinput():
    metrics = {76: -1920, 77: 0, 78: 3840, 79: 2160}
    captured = []
    adapter = Win32InputAdapter(
        send_input=_capture_send_input(captured),
        get_system_metrics=lambda index: metrics[index],
    )
    adapter.send_mouse(action="move", x=-1920, y=0)
    assert len(captured) == 1
    with pytest.raises(ComputerControlError) as raised:
        adapter.send_mouse(action="move", x=1920, y=0)  # origin_x + width 为半开区间外
    assert raised.value.code == "computer_coordinate_out_of_bounds"
    assert len(captured) == 1


def test_wheel_and_hwheel_use_wheel_delta_units():
    """R7：逻辑步进正值向下/向右；Win32 垂直滚轮正值=远离用户（向上），取反。"""
    captured = []
    adapter = Win32InputAdapter(send_input=_capture_send_input(captured))
    adapter.send_mouse(action="wheel", delta=-3)
    adapter.send_mouse(action="hwheel", delta=2)
    assert captured == [
        MouseEventDescriptor(x=0, y=0, flags=MOUSEEVENTF_WHEEL, data=360),
        MouseEventDescriptor(x=0, y=0, flags=MOUSEEVENTF_HWHEEL, data=240),
    ]


def test_wheel_positive_delta_maps_to_negative_wheel_data():
    """R7 方向对照：+1 步进（向下）→ Win32 垂直 wheelData -120；水平 +1 → +120。"""
    captured = []
    adapter = Win32InputAdapter(send_input=_capture_send_input(captured))
    adapter.send_mouse(action="wheel", delta=1)
    adapter.send_mouse(action="hwheel", delta=1)
    assert captured == [
        MouseEventDescriptor(x=0, y=0, flags=MOUSEEVENTF_WHEEL, data=-120),
        MouseEventDescriptor(x=0, y=0, flags=MOUSEEVENTF_HWHEEL, data=120),
    ]


def test_send_keys_presses_in_order_and_releases_in_reverse():
    captured = []
    adapter = Win32InputAdapter(send_input=_capture_send_input(captured))
    adapter.send_keys(("CTRL", "SHIFT", "A"))
    vks = KEY_NAME_TO_VK
    assert [(event.vk, bool(event.flags & KEYEVENTF_KEYUP)) for event in captured] == [
        (vks["CTRL"], False),
        (vks["SHIFT"], False),
        (vks["A"], False),
        (vks["A"], True),
        (vks["SHIFT"], True),
        (vks["CTRL"], True),
    ]
    assert adapter._pressed_keys == []


def test_send_keys_releases_pressed_keys_when_sendinput_fails_midway():
    captured = []
    calls = {"count": 0}

    def flaky_send_input(events):
        calls["count"] += 1
        captured.extend(events)
        if calls["count"] == 2:
            return 0  # 第二次按下（SHIFT）被 UIPI 拒绝
        return len(events)

    adapter = Win32InputAdapter(send_input=flaky_send_input)
    with pytest.raises(ComputerControlError) as raised:
        adapter.send_keys(("CTRL", "SHIFT", "A"))
    assert raised.value.code == "computer_input_rejected"
    assert adapter._pressed_keys == []
    ups = [event.vk for event in captured if event.flags & KEYEVENTF_KEYUP]
    assert ups == [KEY_NAME_TO_VK["CTRL"]]


def test_failed_key_release_keeps_marker_for_release_all():
    captured = []
    calls = {"count": 0}

    def rejecting_send_input(events):
        calls["count"] += 1
        captured.extend(events)
        if calls["count"] > 1:
            raise OSError("inject failure")  # down 成功，后续 up 全部被拒
        return len(events)

    adapter = Win32InputAdapter(send_input=rejecting_send_input)
    with pytest.raises(OSError):
        adapter.send_keys(("CTRL",))
    # up 释放失败必须保留按下标记，供 release_all 重试。
    assert adapter._pressed_keys == ["CTRL"]
    adapter.release_all()
    assert adapter._pressed_keys == []
    ups = [event for event in captured if event.flags & KEYEVENTF_KEYUP]
    assert ups and all(event.vk == KEY_NAME_TO_VK["CTRL"] for event in ups)


def test_release_all_sends_up_for_tracked_pressed_buttons():
    captured = []
    adapter = Win32InputAdapter(send_input=_capture_send_input(captured))
    adapter.send_mouse(action="down", button="left")
    adapter.send_mouse(action="down", button="right")
    assert adapter._pressed_buttons == {"left", "right"}
    adapter.release_all()
    assert adapter._pressed_buttons == set()
    release = captured[-2:]
    assert [event.flags for event in release] == [
        MOUSEEVENTF_LEFTUP,
        MOUSEEVENTF_RIGHTUP,
    ]


# ---------------- Win32InputAdapter.preflight：固定顺序 fail-closed ----------------


def _preflight_recorder(calls, label, value=True):
    def seam():
        calls.append(label)
        return value

    return seam


def test_preflight_rejects_non_windows_before_touching_any_seam():
    calls = []
    adapter = Win32InputAdapter(
        platform="darwin",
        ensure_dpi_awareness=_preflight_recorder(calls, "dpi"),
    )
    with pytest.raises(ComputerControlError) as raised:
        adapter.preflight()
    assert raised.value.code == "computer_unsupported_os"
    assert calls == []


def test_preflight_fails_when_dpi_awareness_cannot_be_confirmed():
    calls = []
    adapter = Win32InputAdapter(
        ensure_dpi_awareness=_preflight_recorder(calls, "dpi", False),
        get_input_desktop_name=_preflight_recorder(calls, "desktop", "Default"),
    )
    with pytest.raises(ComputerControlError) as raised:
        adapter.preflight()
    assert raised.value.code == "computer_backend_start_failed"
    assert calls == ["dpi"]  # 顺序固定：DPI 失败后不再触碰 desktop


# ---------------------------------------------------------------------------
# _ctypes_ensure_dpi_awareness 降级语义（GUI 进程 System aware 放行）
# ---------------------------------------------------------------------------


_DPI_CONTEXT_VALUES = {"unaware": -1, "system": -2, "pmv1": -3, "pmv2": -4}


def _dpi_user32(current: str, set_succeeds: bool):
    """DPI fake：current 为当前感知模式；SetProcessDpiAwarenessContext 成功时
    状态切换为 pmv2（模拟真实 API 的设置后生效语义）。"""
    state = {"current": current}
    set_calls = []

    def get_thread_ctx(*_args):
        return _DPI_CONTEXT_VALUES[state["current"]]

    def contexts_equal(left, right, *_args):
        # c_void_p(-4).value 是 64 位无符号补码（2^64-4）；归一化后再比较
        def _norm(value):
            value = getattr(value, "value", value)
            if isinstance(value, int) and value < 0:
                value += 1 << 64
            return value

        return bool(_norm(left) == _norm(right))

    def set_ctx(pmv2, *_args):
        value = getattr(pmv2, "value", pmv2)
        if isinstance(value, int) and value < 0:
            value += 1 << 64
        set_calls.append(value)
        if set_succeeds:
            state["current"] = "pmv2"
            return 1
        return 0

    return type(
        "DpiUser32",
        (),
        {
            "GetThreadDpiAwarenessContext": _FakeWin32Function(return_value=0),
            "AreDpiAwarenessContextsEqual": _FakeWin32Function(return_value=0),
            "SetProcessDpiAwarenessContext": _FakeWin32Function(return_value=0),
        },
    )(), {
        "get": get_thread_ctx,
        "equal": contexts_equal,
        "set": set_ctx,
        "state": state,
        "set_calls": set_calls,
    }


def _dpi_fake_windll(monkeypatch, current, set_succeeds):
    """安装 DPI fake 到 ctypes.windll；返回 (user32, helpers)。"""
    import ctypes
    from types import SimpleNamespace

    from core.computer import win32_adapter

    user32, helpers = _dpi_user32(current, set_succeeds)
    # _FakeWin32Function 只支持固定返回值；DPI 需要真实比较逻辑，逐个替换实现
    user32.GetThreadDpiAwarenessContext = _DpiFn(helpers["get"])
    user32.AreDpiAwarenessContextsEqual = _DpiFn(helpers["equal"])
    user32.SetProcessDpiAwarenessContext = _DpiFn(helpers["set"])
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32))
    return user32, helpers


class _DpiFn:
    """可比较/可配置实现的 user32 函数替身（支持 restype/argtypes 赋值）。"""

    def __init__(self, impl):
        self.restype = None
        self.argtypes = None
        self._impl = impl

    def __call__(self, *args, **kwargs):
        return self._impl(*args, **kwargs)


def test_dpi_ensure_accepts_pmv2_without_set_call(monkeypatch):
    """已是 PMv2：只验证不设置（Set 不得被调用）。"""
    from core.computer import win32_adapter

    _, helpers = _dpi_fake_windll(monkeypatch, "pmv2", set_succeeds=False)
    assert win32_adapter._ctypes_ensure_dpi_awareness() is True
    assert helpers["set_calls"] == []


def test_dpi_ensure_sets_pmv2_from_unaware_when_allowed(monkeypatch):
    """UNAWARE 且允许设置：切换到 PMv2 并验证通过（CLI 独立进程路径）。"""
    from core.computer import win32_adapter

    _, helpers = _dpi_fake_windll(monkeypatch, "unaware", set_succeeds=True)
    assert win32_adapter._ctypes_ensure_dpi_awareness() is True
    assert helpers["set_calls"] == [(1 << 64) - 4]  # 只尝试 PMv2（64 位补码）


def test_dpi_ensure_accepts_system_aware_when_set_is_locked(monkeypatch):
    """GUI 场景（pywebview WinForms 启动即 SetProcessDPIAware 锁死进程）：
    SetProcessDpiAwarenessContext 失败但当前已是 System DPI aware 时放行——
    System aware 下桌面坐标系（GetSystemMetrics/截图 DC/SendInput 归一化）
    全部为物理像素，操控链路自洽（2026-09-05 修复，用户 GUI 开关死锁）。"""
    from core.computer import win32_adapter

    _, helpers = _dpi_fake_windll(monkeypatch, "system", set_succeeds=False)
    assert win32_adapter._ctypes_ensure_dpi_awareness() is True
    assert helpers["set_calls"] == [(1 << 64) - 4]  # 尝试升级 PMv2 失败后按 System aware 放行


@pytest.mark.parametrize("current", ["unaware", "pmv1"])
def test_dpi_ensure_rejects_other_states_when_set_is_locked(monkeypatch, current):
    """设置失败且非 PMv2/System（UNAWARE 会被 DPI 虚拟化、PMv1 语义陈旧）：
    必须拒绝（fail-closed 保持）。"""
    from core.computer import win32_adapter

    _, _helpers = _dpi_fake_windll(monkeypatch, current, set_succeeds=False)
    assert win32_adapter._ctypes_ensure_dpi_awareness() is False


def test_preflight_fails_without_interactive_input_desktop():
    adapter = Win32InputAdapter(
        ensure_dpi_awareness=lambda: True,
        get_input_desktop_name=lambda: None,
    )
    with pytest.raises(ComputerControlError) as raised:
        adapter.preflight()
    assert raised.value.code == "computer_noninteractive_session"


def test_preflight_fails_on_secure_desktop():
    adapter = Win32InputAdapter(
        ensure_dpi_awareness=lambda: True,
        get_input_desktop_name=lambda: "Winlogon",
    )
    with pytest.raises(ComputerControlError) as raised:
        adapter.preflight()
    assert raised.value.code == "computer_secure_desktop"


def test_preflight_fails_without_foreground_window():
    adapter = Win32InputAdapter(
        ensure_dpi_awareness=lambda: True,
        get_input_desktop_name=lambda: "Default",
        get_foreground_window=lambda: 0,
    )
    with pytest.raises(ComputerControlError) as raised:
        adapter.preflight()
    assert raised.value.code == "computer_secure_desktop"


def test_preflight_fails_when_emergency_hotkey_unavailable():
    adapter = Win32InputAdapter(
        ensure_dpi_awareness=lambda: True,
        get_input_desktop_name=lambda: "Default",
        get_foreground_window=lambda: 1234,
        hotkey_check=lambda: False,
    )
    with pytest.raises(ComputerControlError) as raised:
        adapter.preflight()
    assert raised.value.code == "computer_hotkey_unavailable"


def test_preflight_success_follows_fixed_order():
    calls = []
    adapter = Win32InputAdapter(
        ensure_dpi_awareness=_preflight_recorder(calls, "dpi", True),
        get_input_desktop_name=_preflight_recorder(calls, "desktop", "Default"),
        get_foreground_window=_preflight_recorder(calls, "foreground", 1234),
        hotkey_check=_preflight_recorder(calls, "hotkey", True),
    )
    adapter.preflight()
    assert calls == ["dpi", "desktop", "foreground", "hotkey"]


def test_backend_start_runs_adapter_preflight(tmp_path):
    error = ComputerControlError("computer_secure_desktop", "fake 安全桌面")
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080), preflight_error=error)
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080),
        win32_adapter=adapter,
        screenshot_dir=tmp_path,
    )
    with pytest.raises(ComputerControlError) as raised:
        backend.start()
    assert raised.value.code == "computer_secure_desktop"
    assert adapter.preflight_calls == 1


# ---------------------------------------------------------------------------
# Task 16：64 位句柄截断防护（OpenInputDesktop / GetForegroundWindow restype）
# ---------------------------------------------------------------------------


class _FakeWin32Function:
    """记录 restype/argtypes 声明的 ctypes 函数替身。"""

    def __init__(self, return_value=0):
        self.restype = None
        self.argtypes = None
        self._return_value = return_value
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self._return_value


def _fake_windll(monkeypatch, user32):
    """把 ctypes.windll 换成只暴露 user32 fake 的替身（绝不触碰真实 OS）。"""
    import ctypes
    from types import SimpleNamespace

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32))


def test_input_desktop_name_declares_pointer_restype(monkeypatch):
    """OpenInputDesktop 必须声明 c_void_p restype：默认 c_int 会把 64 位
    HDESK 截断成 32 位，CloseDesktop 拿到无效句柄（Task 16 修复）。"""
    import ctypes

    from core.computer import win32_adapter

    user32 = type(
        "User32",
        (),
        {
            "OpenInputDesktop": _FakeWin32Function(return_value=0),
            "GetUserObjectInformationW": _FakeWin32Function(return_value=0),
            "CloseDesktop": _FakeWin32Function(return_value=0),
        },
    )()
    _fake_windll(monkeypatch, user32)

    assert win32_adapter._ctypes_input_desktop_name() is None  # NULL 句柄
    assert user32.OpenInputDesktop.restype == ctypes.c_void_p
    assert user32.CloseDesktop.argtypes == [ctypes.c_void_p]


def test_foreground_window_declares_pointer_restype(monkeypatch):
    """GetForegroundWindow 必须声明 c_void_p restype：64 位 HWND 不得截断。"""
    import ctypes

    from core.computer import win32_adapter

    user32 = type(
        "User32",
        (),
        {"GetForegroundWindow": _FakeWin32Function(return_value=0x1AB2CD3EF456)},
    )()
    _fake_windll(monkeypatch, user32)

    assert win32_adapter._ctypes_foreground_window() == 0x1AB2CD3EF456  # 64 位不截断
    assert user32.GetForegroundWindow.restype == ctypes.c_void_p


# ---------------------------------------------------------------------------
# R5：桌面前台窗口身份与几何代数（旧坐标失效）
# ---------------------------------------------------------------------------


def test_desktop_foreground_change_between_actions_rejects_stale_input():
    """观察后用户切到其他软件：输入前身份复核失败，零输入并显式报错。"""
    image = FakeImage(width=1280, height=720)
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720), foreground_id=1001)
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    observation = backend.observe("run-a")
    assert observation.surface_id == "hwnd:1001"
    adapter.foreground_id = 2002  # 用户切到另一个前台窗口
    adapter.foreground_title = "Other App"
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=10, y=10))
    assert raised.value.code == "computer_surface_changed"
    assert adapter.clicks == []
    # 键盘输入（通用化重构 Task C 新语义）：目标取最近观察的受控窗口，
    # 前置激活把它带回前台后正常发送，而不是拒绝输入。
    keyboard_adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720), foreground_id=1001)
    keyboard_backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(width=1280, height=720), win32_adapter=keyboard_adapter
    )
    keyboard_backend.observe("run-k")
    keyboard_adapter.foreground_id = 2002  # 用户切到另一个前台窗口
    keyboard_backend.execute(
        "run-k", ComputerAction(kind="keypress", target="desktop", keys=("ENTER",))
    )
    assert keyboard_adapter.set_foreground_calls == [1001]
    assert keyboard_adapter.keys_sent == [("ENTER",)]
    # 激活失败（best-effort）时维持 fail-closed：R5 复核拒绝，零输入。
    failed_adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720), foreground_id=1001)
    failed_backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(width=1280, height=720), win32_adapter=failed_adapter
    )
    failed_backend.observe("run-f")
    failed_adapter.foreground_id = 2002
    failed_adapter.set_foreground_result = False
    with pytest.raises(ComputerControlError) as raised_failed:
        failed_backend.execute(
            "run-f", ComputerAction(kind="keypress", target="desktop", keys=("ENTER",))
        )
    assert raised_failed.value.code == "computer_surface_changed"
    assert failed_adapter.keys_sent == []
    # 重新观察后基线刷新，输入恢复。
    refreshed = backend.observe("run-a")
    assert refreshed.surface_id == "hwnd:2002"
    assert refreshed.surface_generation > observation.surface_generation
    backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=10, y=10))
    assert adapter.clicks == [(10, 10, "left")]


def test_desktop_geometry_change_invalidates_stale_coordinates():
    """分辨率/多屏几何变化：surface 代数递增，旧坐标输入被拒绝。"""
    image = FakeImage(width=1280, height=720)
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720))
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    observation = backend.observe("run-a")
    adapter.virtual_rect = (-1920, 0, 3840, 2160)  # 显示器配置改变
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=10, y=10))
    assert raised.value.code == "computer_surface_changed"
    assert adapter.clicks == []


def test_desktop_same_surface_input_allowed_and_wait_unchecked():
    """签名一致时输入正常；wait 不做 surface 复核。"""
    image = FakeImage(width=1280, height=720)
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720))
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    backend.observe("run-a")
    backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=100, y=100))
    backend.execute("run-a", ComputerAction(kind="type_text", target="desktop", text="你好"))
    assert adapter.clicks == [(100, 100, "left")]
    assert adapter.clipboard_writes == ["你好"]  # 2026-09-06：非 ASCII 走剪贴板


def test_desktop_scroll_rejects_pixel_unit_and_out_of_range():
    """R7：桌面滚动仅 wheel_step；像素单位、超限、两轴为零全部零执行拒绝。"""
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    backend.observe("run-a")
    for action in (
        ComputerAction(kind="scroll", target="desktop", delta_y=1, unit="css_pixel"),
        ComputerAction(kind="scroll", target="desktop", delta_y=11),
        ComputerAction(kind="scroll", target="desktop", delta_x=0, delta_y=0),
    ):
        with pytest.raises(ComputerControlError) as raised:
            backend.execute("run-a", action)
        assert raised.value.code == "computer_invalid_action"
    assert adapter.wheels == [] and adapter.hwheels == []


def test_desktop_wait_is_interruptible_and_close_not_blocked():
    """R8：桌面 wait 不持大锁睡满时长；close 可收尾且等待以取消收尾。"""
    import threading
    import time

    image = FakeImage(width=1280, height=720)
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720))
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    backend.observe("run-a")
    outcome = []

    def wait_action():
        try:
            backend.execute(
                "run-a", ComputerAction(kind="wait", target="desktop", duration_ms=10_000)
            )
            outcome.append("completed")
        except ComputerControlError as error:
            outcome.append(error.code)

    waiter = threading.Thread(target=wait_action, daemon=True)
    waiter.start()
    time.sleep(0.1)  # 让 waiter 进入分片等待
    started = time.monotonic()
    backend.close()  # 不得被进行中的 wait 阻塞
    assert time.monotonic() - started < 3
    waiter.join(timeout=3)
    assert not waiter.is_alive(), "close 后桌面显式等待仍悬挂"
    assert outcome == ["computer_cancelled"]


# ---------------- 通用化重构 Task C：输入前置激活 ----------------


def _activation_backend(adapter):
    return WindowsComputerBackend(
        image_grab=lambda: FakeImage(width=3840, height=2160, origin_x=-1920, origin_y=0),
        win32_adapter=adapter,
    )


def test_click_activates_non_foreground_window_before_send_mouse():
    """目标点所属窗口非前台：先 set_foreground_window 再 send_mouse。"""
    adapter = FakeWin32Adapter()
    adapter.window_at_point_id = 2002
    backend = _activation_backend(adapter)
    backend.start()
    try:
        backend.observe("run-c")
        backend.execute(
            "run-c", ComputerAction(kind="click", target="desktop", x=100, y=100)
        )
        assert adapter.window_from_point_calls == [(-1720, 200)]
        assert adapter.set_foreground_calls == [2002]
        labels = [entry[0] for entry in adapter.call_order]
        assert labels.index("set_foreground") < labels.index("send_mouse")
        assert adapter.clicks == [(-1720, 200, "left")]
    finally:
        backend.close()


def test_click_skips_activation_when_already_foreground():
    """目标点所属窗口已是前台：不调用 set_foreground_window。"""
    adapter = FakeWin32Adapter()
    backend = _activation_backend(adapter)
    backend.start()
    try:
        backend.observe("run-c")
        backend.execute(
            "run-c", ComputerAction(kind="click", target="desktop", x=100, y=100)
        )
        assert adapter.set_foreground_calls == []
        assert adapter.clicks
    finally:
        backend.close()


def test_activation_failure_does_not_block_input():
    """SetForegroundWindow 失败（含 Alt 键技巧重试后）：输入照常发送。"""
    adapter = FakeWin32Adapter()
    adapter.window_at_point_id = 2002
    adapter.set_foreground_result = False
    backend = _activation_backend(adapter)
    backend.start()
    try:
        backend.observe("run-c")
        backend.execute(
            "run-c", ComputerAction(kind="click", target="desktop", x=100, y=100)
        )
        assert adapter.set_foreground_calls == [2002]
        assert adapter.clicks == [(-1720, 200, "left")]
    finally:
        backend.close()


def test_drag_activates_window_of_first_path_point():
    adapter = FakeWin32Adapter()
    adapter.window_at_point_id = 2002
    backend = _activation_backend(adapter)
    backend.start()
    try:
        backend.observe("run-c")
        backend.execute(
            "run-c",
            ComputerAction(
                kind="drag",
                target="desktop",
                path=((100, 100), (150, 150)),  # 模型坐标 → 物理 (-1720,200)/(-1620,300)
            ),
        )
        assert adapter.set_foreground_calls == [2002]
        assert adapter.window_from_point_calls == [(-1720, 200)]
    finally:
        backend.close()


def test_type_text_activates_window_from_last_observation_surface():
    """键盘动作：目标 = 最近观察的受控窗口（hwnd:N）；先于 R5 复核激活。"""
    adapter = FakeWin32Adapter(foreground_id=2002)
    backend = _activation_backend(adapter)
    backend.start()
    try:
        observation = backend.observe("run-c")
        assert observation.surface_id == "hwnd:2002"
        adapter.foreground_id = 1001  # 用户切走窗口（R5 复核本会拒绝）
        backend.execute(
            "run-c", ComputerAction(kind="type_text", target="desktop", text="hello")
        )
        assert adapter.set_foreground_calls == [2002]
        assert adapter.texts == ["hello"]
        labels = [entry[0] for entry in adapter.call_order]
        assert labels.index("set_foreground") < labels.index("send_text")
    finally:
        backend.close()


def test_input_activated_reflects_activation_once():
    """input_activated 只在发生激活后的那次观察为 True，消费后清零。"""
    adapter = FakeWin32Adapter()
    adapter.window_at_point_id = 2002
    backend = _activation_backend(adapter)
    backend.start()
    try:
        assert backend.observe("run-c").input_activated is False
        backend.execute(
            "run-c", ComputerAction(kind="click", target="desktop", x=100, y=100)
        )
        assert backend.observe("run-c").input_activated is True
        assert backend.observe("run-c").input_activated is False
    finally:
        backend.close()


def test_projection_excludes_input_activated():
    """投影字节稳定：input_activated 绝不进入 observation_provider_projection。"""
    from core.computer.actions import observation_provider_projection

    observation = ComputerObservation(
        run_id="r", target="desktop", input_activated=True
    )
    assert "input_activated" not in observation_provider_projection(observation)


def test_adapter_activation_seams_swallow_errors():
    """seam 抛错归一为安全返回值：window_from_point→0，set_foreground→False。"""

    def _boom(*args, **kwargs):
        raise RuntimeError("injected failure")

    adapter = Win32InputAdapter(
        window_from_point=_boom, set_foreground_window=_boom
    )
    assert adapter.window_from_point(1, 2) == 0
    assert adapter.set_foreground_window(123) is False
