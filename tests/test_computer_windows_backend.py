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


def test_type_text_routes_unicode_text_to_adapter():
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    backend.observe("run-a")
    backend.execute("run-a", ComputerAction(kind="type_text", target="desktop", text="你好"))
    assert adapter.texts == ["你好"]


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
    captured = []
    adapter = Win32InputAdapter(send_input=_capture_send_input(captured))
    adapter.send_mouse(action="wheel", delta=-3)
    adapter.send_mouse(action="hwheel", delta=2)
    assert captured == [
        MouseEventDescriptor(x=0, y=0, flags=MOUSEEVENTF_WHEEL, data=-360),
        MouseEventDescriptor(x=0, y=0, flags=MOUSEEVENTF_HWHEEL, data=240),
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
