import logging
import threading

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
from tests.computer_fakes import FakeImage, FakeSurfaceLocator, FakeWin32Adapter


def _capture_send_input(captured):

    def send_input(events):
        captured.extend(events)
        return len(events)

    return send_input


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


def test_stop_during_drag_rejects_later_path_points_but_releases_button():
    input_entered = threading.Event()
    allow_current_batch = threading.Event()
    submitted = []

    def send_input(events):
        batch = list(events)
        submitted.append(batch)
        if len(submitted) == 3:
            input_entered.set()
            assert allow_current_batch.wait(timeout=2)
        return len(batch)

    adapter = Win32InputAdapter(send_input=send_input)
    errors = []

    def drag():
        try:
            adapter.send_mouse(
                action="drag",
                path=((10, 10), (20, 20), (30, 30)),
            )
        except ComputerControlError as error:
            errors.append(error)

    worker = threading.Thread(target=drag)
    worker.start()
    try:
        assert input_entered.wait(timeout=1)
        stopping = threading.Thread(target=adapter.request_stop)
        stopping.start()
        assert adapter._input_stop.wait(timeout=1)
        allow_current_batch.set()
        stopping.join(timeout=2)
    finally:
        allow_current_batch.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert not stopping.is_alive()
    assert len(errors) == 1 and errors[0].code == "computer_cancelled"
    assert len(submitted) == 4
    assert submitted[-1] == [MouseEventDescriptor(0, 0, MOUSEEVENTF_LEFTUP)]


def test_pressed_keys_are_released_when_keypress_fails():
    adapter = FakeWin32Adapter(fail_after=2)
    with pytest.raises(ComputerControlError):
        adapter.send_keys(("CTRL", "SHIFT", "A"))
    assert adapter.keys_down == set()


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
        if calls["count"] > 2:
            raise OSError("screen grab failed")
        return FakeImage(1920, 1080)

    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    backend = WindowsComputerBackend(image_grab=grab, win32_adapter=adapter)
    first = backend.observe("run-a")
    assert first.ok
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_backend_start_failed"
    assert backend._last_observation is first


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


def test_close_signals_stop_before_waiting_for_inflight_input():
    entered_input = threading.Event()
    allow_input_to_finish = threading.Event()
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1920, 1080))
    original_send_mouse = adapter.send_mouse

    def blocked_send_mouse(**kwargs):
        entered_input.set()
        assert allow_input_to_finish.wait(timeout=2)
        original_send_mouse(**kwargs)

    adapter.send_mouse = blocked_send_mouse
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 1080), win32_adapter=adapter
    )
    backend.observe("run-a")
    action_thread = threading.Thread(
        target=lambda: backend.execute(
            "run-a", ComputerAction(kind="click", target="desktop", x=10, y=10)
        )
    )
    close_thread = threading.Thread(target=backend.close)
    action_thread.start()
    try:
        assert entered_input.wait(timeout=2)
        close_thread.start()
        assert backend._stop_event.wait(timeout=0.5)
    finally:
        allow_input_to_finish.set()
        action_thread.join(timeout=3)
        if close_thread.ident is not None:
            close_thread.join(timeout=3)

    assert not action_thread.is_alive()
    assert not close_thread.is_alive()
    assert adapter.release_all_calls >= 1


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
    adapter.send_text("😀")
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
        adapter.send_mouse(action="move", x=1920, y=0)
    assert raised.value.code == "computer_coordinate_out_of_bounds"
    assert len(captured) == 1


def test_wheel_and_hwheel_use_wheel_delta_units():
    captured = []
    adapter = Win32InputAdapter(send_input=_capture_send_input(captured))
    adapter.send_mouse(action="wheel", delta=-3)
    adapter.send_mouse(action="hwheel", delta=2)
    assert captured == [
        MouseEventDescriptor(x=0, y=0, flags=MOUSEEVENTF_WHEEL, data=360),
        MouseEventDescriptor(x=0, y=0, flags=MOUSEEVENTF_HWHEEL, data=240),
    ]


def test_wheel_positive_delta_maps_to_negative_wheel_data():
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
            return 0
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
        if calls["count"] in (2, 3):
            raise OSError("inject failure")
        return len(events)

    adapter = Win32InputAdapter(send_input=rejecting_send_input)
    with pytest.raises(OSError):
        adapter.send_keys(("CTRL",))
    assert adapter._pressed_keys == ["CTRL"]
    adapter.release_all()
    assert adapter._pressed_keys == []
    ups = [event for event in captured if event.flags & KEYEVENTF_KEYUP]
    assert ups and all(event.vk == KEY_NAME_TO_VK["CTRL"] for event in ups)


def test_release_all_wraps_send_input_exception_as_cleanup_failed():
    def failing_send_input(_events):
        raise OSError("release inject failure")

    adapter = Win32InputAdapter(send_input=failing_send_input)
    adapter._pressed_buttons.add("left")
    adapter._pressed_keys.append("CTRL")

    with pytest.raises(ComputerControlError) as raised:
        adapter.release_all()
    assert raised.value.code == "computer_cleanup_failed"
    assert adapter._pressed_buttons == {"left"}
    assert adapter._pressed_keys == ["CTRL"]


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


def test_release_all_retains_pressed_markers_after_partial_send_input():
    submitted = []

    def send_input(events):
        submitted.append(list(events))
        return len(events) - 1 if len(submitted) == 1 else len(events)

    adapter = Win32InputAdapter(send_input=send_input)
    adapter._pressed_buttons.add("left")
    adapter._pressed_keys.append("CTRL")

    with pytest.raises(ComputerControlError) as raised:
        adapter.release_all()
    assert raised.value.code == "computer_input_rejected"
    assert adapter._pressed_buttons == {"left"}
    assert adapter._pressed_keys == ["CTRL"]

    adapter.release_all()
    assert adapter._pressed_buttons == set()
    assert adapter._pressed_keys == []


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
    assert calls == ["dpi"]


_DPI_CONTEXT_VALUES = {"unaware": -1, "system": -2, "pmv1": -3, "pmv2": -4}


def _dpi_user32(current: str, set_succeeds: bool):
    state = {"current": current}
    set_calls = []

    def get_thread_ctx(*_args):
        return _DPI_CONTEXT_VALUES[state["current"]]

    def contexts_equal(left, right, *_args):
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
    import ctypes
    from types import SimpleNamespace

    from core.computer import win32_adapter

    user32, helpers = _dpi_user32(current, set_succeeds)
    user32.GetThreadDpiAwarenessContext = _DpiFn(helpers["get"])
    user32.AreDpiAwarenessContextsEqual = _DpiFn(helpers["equal"])
    user32.SetProcessDpiAwarenessContext = _DpiFn(helpers["set"])
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32))
    return user32, helpers


class _DpiFn:

    def __init__(self, impl):
        self.restype = None
        self.argtypes = None
        self._impl = impl

    def __call__(self, *args, **kwargs):
        return self._impl(*args, **kwargs)


def test_dpi_ensure_accepts_pmv2_without_set_call(monkeypatch):
    from core.computer import win32_adapter

    _, helpers = _dpi_fake_windll(monkeypatch, "pmv2", set_succeeds=False)
    assert win32_adapter._ctypes_ensure_dpi_awareness() is True
    assert helpers["set_calls"] == []


def test_dpi_ensure_sets_pmv2_from_unaware_when_allowed(monkeypatch):
    from core.computer import win32_adapter

    _, helpers = _dpi_fake_windll(monkeypatch, "unaware", set_succeeds=True)
    assert win32_adapter._ctypes_ensure_dpi_awareness() is True
    assert helpers["set_calls"] == [(1 << 64) - 4]


def test_dpi_ensure_accepts_system_aware_when_set_is_locked(monkeypatch):
    from core.computer import win32_adapter

    _, helpers = _dpi_fake_windll(monkeypatch, "system", set_succeeds=False)
    assert win32_adapter._ctypes_ensure_dpi_awareness() is True
    assert helpers["set_calls"] == [(1 << 64) - 4]


@pytest.mark.parametrize("current", ["unaware", "pmv1"])
def test_dpi_ensure_rejects_other_states_when_set_is_locked(monkeypatch, current):
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


class _FakeWin32Function:

    def __init__(self, return_value=0):
        self.restype = None
        self.argtypes = None
        self._return_value = return_value
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self._return_value


def _fake_windll(monkeypatch, user32):
    import ctypes
    from types import SimpleNamespace

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32))


def test_input_desktop_name_declares_pointer_restype(monkeypatch):
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

    assert win32_adapter._ctypes_input_desktop_name() is None
    assert user32.OpenInputDesktop.restype == ctypes.c_void_p
    assert user32.CloseDesktop.argtypes == [ctypes.c_void_p]


def test_foreground_window_declares_pointer_restype(monkeypatch):
    import ctypes

    from core.computer import win32_adapter

    user32 = type(
        "User32",
        (),
        {"GetForegroundWindow": _FakeWin32Function(return_value=0x1AB2CD3EF456)},
    )()
    _fake_windll(monkeypatch, user32)

    assert win32_adapter._ctypes_foreground_window() == 0x1AB2CD3EF456
    assert user32.GetForegroundWindow.restype == ctypes.c_void_p


def test_desktop_foreground_change_between_actions_rejects_stale_input():
    image = FakeImage(width=1280, height=720)
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720), foreground_id=1001)
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    observation = backend.observe("run-a")
    assert observation.surface_id == "hwnd:1001"
    adapter.foreground_id = 2002
    adapter.foreground_title = "Other App"
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=10, y=10))
    assert raised.value.code == "computer_surface_changed"
    assert adapter.clicks == []
    with pytest.raises(ComputerControlError) as raised_keys:
        backend.execute("run-a", ComputerAction(kind="keypress", target="desktop", keys=("ENTER",)))
    assert raised_keys.value.code == "computer_surface_changed"
    assert adapter.keys_sent == []
    refreshed = backend.observe("run-a")
    assert refreshed.surface_id == "hwnd:2002"
    assert refreshed.surface_generation > observation.surface_generation
    backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=10, y=10))
    assert adapter.clicks == [(10, 10, "left")]


def test_desktop_geometry_change_invalidates_stale_coordinates():
    image = FakeImage(width=1280, height=720)
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720))
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    observation = backend.observe("run-a")
    adapter.virtual_rect = (-1920, 0, 3840, 2160)
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=10, y=10))
    assert raised.value.code == "computer_surface_changed"
    assert adapter.clicks == []


def test_desktop_same_surface_input_allowed_and_wait_unchecked():
    image = FakeImage(width=1280, height=720)
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720))
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    backend.observe("run-a")
    backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=100, y=100))
    backend.execute("run-a", ComputerAction(kind="type_text", target="desktop", text="你好"))
    assert adapter.clicks == [(100, 100, "left")]
    assert adapter.clipboard_writes == ["你好"]


def test_desktop_scroll_rejects_pixel_unit_and_out_of_range():
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
    time.sleep(0.1)
    started = time.monotonic()
    backend.close()
    assert time.monotonic() - started < 3
    waiter.join(timeout=3)
    assert not waiter.is_alive(), "close 后桌面显式等待仍悬挂"
    assert outcome == ["computer_cancelled"]


def _activation_backend(adapter):
    return WindowsComputerBackend(
        image_grab=lambda: FakeImage(width=3840, height=2160, origin_x=-1920, origin_y=0),
        win32_adapter=adapter,
    )


def test_click_activates_non_foreground_window_before_send_mouse():
    adapter = FakeWin32Adapter()
    adapter.window_at_point_id = 2002
    backend = _activation_backend(adapter)
    backend.start()
    try:
        backend.observe("run-c")
        backend.execute(
            "run-c", ComputerAction(kind="click", target="desktop", x=100, y=100)
        )
        assert adapter.window_from_point_calls == [(-1720, 200), (-1720, 200)]
        assert adapter.set_foreground_calls == [2002]
        labels = [entry[0] for entry in adapter.call_order]
        assert labels.index("set_foreground") < labels.index("send_mouse")
        assert adapter.clicks == [(-1720, 200, "left")]
    finally:
        backend.close()


def test_click_skips_activation_when_already_foreground():
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


def test_activation_failure_blocks_input_with_explicit_error():
    adapter = FakeWin32Adapter()
    adapter.window_at_point_id = 2002
    adapter.set_foreground_result = False
    backend = _activation_backend(adapter)
    backend.start()
    try:
        backend.observe("run-c")
        with pytest.raises(ComputerControlError) as excinfo:
            backend.execute(
                "run-c", ComputerAction(kind="click", target="desktop", x=100, y=100)
            )
        assert excinfo.value.code == "computer_foreground_activation_failed"
        assert adapter.set_foreground_calls == [2002]
        assert adapter.clicks == []
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
                path=((100, 100), (150, 150)),
            ),
        )
        assert adapter.set_foreground_calls == [2002]
        assert adapter.window_from_point_calls == [(-1720, 200), (-1720, 200)]
    finally:
        backend.close()


def test_type_text_after_window_switch_is_rejected_without_focus_steal():
    adapter = FakeWin32Adapter(foreground_id=2002)
    backend = _activation_backend(adapter)
    backend.start()
    try:
        observation = backend.observe("run-c")
        assert observation.surface_id == "hwnd:2002"
        adapter.foreground_id = 1001
        with pytest.raises(ComputerControlError) as exc:
            backend.execute(
                "run-c", ComputerAction(kind="type_text", target="desktop", text="hello")
            )
        assert exc.value.code == "computer_surface_changed"
        assert adapter.set_foreground_calls == []
        assert adapter.texts == []
        assert adapter.keys_sent == []
    finally:
        backend.close()


@pytest.mark.parametrize(
    "action",
    [
        ComputerAction(kind="keypress", target="desktop", keys=("CTRL", "A")),
        ComputerAction(kind="type_text", target="desktop", text="hello"),
    ],
    ids=["keypress", "type_text"],
)
def test_keyboard_after_focus_steal_back_is_rejected_with_foreground_lost(action):
    adapter = FakeWin32Adapter(foreground_id=1001)
    backend = _activation_backend(adapter)
    backend.start()
    try:
        assert backend.observe("run-c").surface_id == "hwnd:1001"
        adapter.window_at_point_id = 2002
        backend.execute(
            "run-c", ComputerAction(kind="click", target="desktop", x=100, y=100)
        )
        assert adapter.set_foreground_calls == [2002]
        assert adapter.clicks
        adapter.foreground_id = 1001
        adapter.window_at_point_id = None
        with pytest.raises(ComputerControlError) as exc:
            backend.execute("run-c", action)
        assert exc.value.code == "computer_foreground_lost"
        assert exc.value.details == {"expected_hwnd": 2002, "current_hwnd": 1001}
        assert "activate_surface" in exc.value.message
        assert adapter.texts == []
        assert adapter.keys_sent == []
        assert adapter.clipboard_writes == []  # 非 ASCII 路径也绝不落剪贴板
    finally:
        backend.close()


@pytest.mark.parametrize(
    "action",
    [
        ComputerAction(kind="keypress", target="desktop", keys=("CTRL", "A")),
        ComputerAction(kind="type_text", target="desktop", text="hi"),
    ],
    ids=["keypress", "type_text"],
)
def test_keyboard_without_focus_reference_is_allowed(action):
    adapter = FakeWin32Adapter(foreground_id=1001)
    backend = _activation_backend(adapter)
    try:
        backend.execute("run-c", action)
        assert adapter.keys_sent or adapter.texts
        assert adapter.window_from_point_calls == []  # 键盘动作绝不做置前探测
        assert adapter.set_foreground_calls == []
    finally:
        backend.close()


def test_input_activated_reflects_activation_once():
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
    from core.computer.actions import observation_provider_projection

    observation = ComputerObservation(
        run_id="r", target="desktop", input_activated=True
    )
    assert "input_activated" not in observation_provider_projection(observation)


def test_adapter_activation_seams_swallow_errors():

    def _boom(*args, **kwargs):
        raise RuntimeError("injected failure")

    adapter = Win32InputAdapter(
        window_from_point=_boom, set_foreground_window=_boom
    )
    assert adapter.window_from_point(1, 2) == 0
    assert adapter.set_foreground_window(123) is False


class _SurfaceLocatorStub(FakeSurfaceLocator):

    def __init__(self, *, windows=(), tray_icons=(), windows_error=None, tray_error=None):
        super().__init__(windows=windows, tray_icons=tray_icons)
        self.windows_error = windows_error
        self.tray_error = tray_error
        self.list_windows_calls = 0
        self.list_tray_calls = 0

    def list_windows(self):
        self.list_windows_calls += 1
        if self.windows_error is not None:
            raise self.windows_error
        return super().list_windows()

    def list_tray_icons(self):
        self.list_tray_calls += 1
        if self.tray_error is not None:
            raise self.tray_error
        return super().list_tray_icons()


def _surface_backend(locator):
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1280, 720),
        win32_adapter=adapter,
        surface_locator=locator,
    )
    backend.observe("run-a")
    return backend, adapter


_WINDOW_SPEC = {"label": "记事本", "bounds": (10, 20, 300, 200)}
_TRAY_SPEC = {
    "label": "微信",
    "bounds": (1180, 680, 24, 24),
    "operations": ("invoke", "click_point"),
}


def test_list_surfaces_degrades_when_tray_enumeration_times_out(caplog):
    timeout = ComputerControlError(
        "computer_surfaces_unavailable", "UI Automation 调用超时"
    )
    locator = _SurfaceLocatorStub(
        windows=[_WINDOW_SPEC], tray_icons=[_TRAY_SPEC], tray_error=timeout
    )
    backend, _adapter = _surface_backend(locator)
    with caplog.at_level(logging.WARNING, logger="core.computer.windows_backend"):
        backend.execute("run-a", ComputerAction(kind="list_surfaces", target="desktop"))
    candidates = backend.observe("run-a").surface_candidates
    assert [candidate["kind"] for candidate in candidates] == ["window"]
    assert "记事本" in candidates[0]["label"]
    candidate = candidates[0]
    backend.execute(
        "run-a",
        ComputerAction(
            kind="activate_surface",
            target="desktop",
            surface_ref=candidate["ref"],
            surface_generation=candidate["surface_generation"],
        ),
    )
    assert locator.activate_calls == [
        (candidate["ref"], candidate["surface_generation"])
    ]
    # 降级必须可诊断：warning 日志带失败原因，不是静默吞错。
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "托盘" in message and "UI Automation 调用超时" in message
        for message in messages
    ), messages


def test_list_surfaces_degrades_on_unexpected_tray_exception(caplog):
    locator = _SurfaceLocatorStub(
        windows=[_WINDOW_SPEC], tray_error=RuntimeError("UIA 元素树读取崩溃")
    )
    backend, _adapter = _surface_backend(locator)
    with caplog.at_level(logging.WARNING, logger="core.computer.windows_backend"):
        backend.execute("run-a", ComputerAction(kind="list_surfaces", target="desktop"))
    candidates = backend.observe("run-a").surface_candidates
    assert [candidate["kind"] for candidate in candidates] == ["window"]
    assert locator.list_tray_calls == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "托盘" in message and "UIA 元素树读取崩溃" in message for message in messages
    ), messages


def test_list_surfaces_still_fails_closed_when_window_enumeration_fails():
    error = ComputerControlError(
        "computer_surfaces_unavailable", "窗口枚举句柄无效"
    )
    locator = _SurfaceLocatorStub(windows_error=error, tray_icons=[_TRAY_SPEC])
    backend, _adapter = _surface_backend(locator)
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="list_surfaces", target="desktop"))
    assert raised.value.code == "computer_surfaces_unavailable"
    assert raised.value.message == "窗口枚举句柄无效"
    assert backend._pending_surface_candidates == ()
    assert backend.observe("run-a").surface_candidates == ()


def test_list_surfaces_success_keeps_window_then_tray_order():
    locator = _SurfaceLocatorStub(
        windows=[
            {"label": "记事本", "bounds": (10, 20, 300, 200)},
            {"label": "浏览器", "bounds": (400, 100, 800, 600)},
        ],
        tray_icons=[_TRAY_SPEC],
    )
    backend, _adapter = _surface_backend(locator)
    backend.execute("run-a", ComputerAction(kind="list_surfaces", target="desktop"))
    candidates = backend.observe("run-a").surface_candidates
    assert len(candidates) == 3
    assert [candidate["kind"] for candidate in candidates] == [
        "window",
        "window",
        "tray_icon",
    ]
    assert [candidate["label"] for candidate in candidates] == [
        "记事本",
        "浏览器",
        "微信",
    ]
    assert candidates[2]["operations"] == ("invoke", "click_point")
    assert backend.observe("run-a").surface_candidates == ()


def test_observe_returns_stale_observation_when_hwnd_changes_during_capture():
    adapter = FakeWin32Adapter(foreground_id=1001, virtual_rect=(0, 0, 1280, 720))

    def switching_grab():
        # 两次抓取都换前台：内部重试一次后仍然不稳定，观察必须失败。
        adapter.foreground_id += 1000
        return FakeImage(1280, 720)

    backend = WindowsComputerBackend(
        image_grab=switching_grab, win32_adapter=adapter
    )
    backend._started = True
    try:
        obs = backend.observe("run-stale-cap")
        assert obs.ok is False
        assert obs.error_code == "computer_surface_changed"
        assert obs.width == 0 and obs.height == 0
        assert obs.elements == ()
        with pytest.raises(ComputerControlError) as raised:
            backend.execute(
                "run-stale-cap", ComputerAction(kind="click", target="desktop", x=10, y=10)
            )
        assert raised.value.code == "computer_invalid_action"
    finally:
        backend.close()


def test_click_rejects_when_post_activation_target_or_foreground_drifts():
    class _DriftingAdapter(FakeWin32Adapter):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.point_sequence = [2002, 3003]

        def window_from_point(self, x, y):
            self.window_from_point_calls.append((x, y))
            return self.point_sequence.pop(0) if self.point_sequence else 3003

    adapter = _DriftingAdapter(foreground_id=1001, virtual_rect=(0, 0, 1280, 720))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1280, 720), win32_adapter=adapter
    )
    backend.start()
    try:
        backend.observe("run-drift")
        with pytest.raises(ComputerControlError) as raised:
            backend.execute(
                "run-drift", ComputerAction(kind="click", target="desktop", x=50, y=50)
            )
        assert raised.value.code == "computer_surface_changed"
        assert adapter.clicks == []
    finally:
        backend.close()

    # 若 set_foreground_window 返回 True 但 GetForegroundWindow 仍不是目标 HWND
    adapter2 = FakeWin32Adapter(foreground_id=1001, virtual_rect=(0, 0, 1280, 720))
    adapter2.window_at_point_id = 2002
    adapter2.set_foreground_window = lambda hwnd: True  # 不改 foreground_id
    backend2 = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1280, 720), win32_adapter=adapter2
    )
    backend2.start()
    try:
        backend2.observe("run-fg-mismatch")
        with pytest.raises(ComputerControlError) as raised2:
            backend2.execute(
                "run-fg-mismatch",
                ComputerAction(kind="click", target="desktop", x=50, y=50),
            )
        assert raised2.value.code == "computer_foreground_activation_failed"
        assert adapter2.clicks == []
    finally:
        backend2.close()


def test_activate_surface_invalidates_prior_observation_and_elements():
    from tests.test_computer_elements import FakeComHost
    from core.computer.platforms.windows_elements import WindowsElementLocator

    locator_el = WindowsElementLocator(
        collect_fn=FakeComHost().collect_impl,
        click_fn=lambda ref: None,
        set_value_fn=lambda ref, text: None,
    )
    surface_stub = _SurfaceLocatorStub(windows=[_WINDOW_SPEC])
    adapter = FakeWin32Adapter(virtual_rect=(0, 0, 1280, 720))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1280, 720),
        win32_adapter=adapter,
        surface_locator=surface_stub,
        element_locator=locator_el,
    )
    backend.start()
    try:
        obs = backend.observe("run-act")
        assert obs.elements
        backend.execute("run-act", ComputerAction(kind="list_surfaces", target="desktop"))
        candidates = backend.observe("run-act").surface_candidates
        backend.execute(
            "run-act",
            ComputerAction(
                kind="activate_surface",
                target="desktop",
                surface_ref=candidates[0]["ref"],
                surface_generation=candidates[0]["surface_generation"],
            ),
        )
        # 激活/恢复窗口后旧坐标与元素引用立即失效，未重新 observe 前必须拒绝
        with pytest.raises(ComputerControlError) as raised:
            backend.execute(
                "run-act", ComputerAction(kind="click", target="desktop", x=20, y=20)
            )
        assert raised.value.code == "computer_invalid_action"
        with pytest.raises(ComputerControlError) as raised_el:
            locator_el.click("el-1")
        assert raised_el.value.code == "computer_element_stale"
    finally:
        backend.close()


def test_drag_samples_only_caller_path_and_scroll_does_not_activate_foreground():
    captured = []
    adapter = Win32InputAdapter(
        send_input=_capture_send_input(captured),
        get_system_metrics=lambda idx: {76: 0, 77: 0, 78: 1920, 79: 1080}.get(idx, 4),
    )
    # 水平和垂直跨度即使小于系统阈值 SM_CXDRAG/SM_CYDRAG，也只沿用户 path 采样、绝不外推
    adapter.send_mouse(action="drag", path=((100, 100), (102, 100)))
    moves = [ev for ev in captured if ev.flags & MOUSEEVENTF_MOVE]
    assert len(moves) == 2
    assert moves[0].x <= moves[1].x
    assert all(ev.y == moves[0].y for ev in moves)

    captured_v = []
    adapter_v = Win32InputAdapter(send_input=_capture_send_input(captured_v))
    adapter_v.send_mouse(action="drag", path=((100, 100), (100, 102)))
    moves_v = [ev for ev in captured_v if ev.flags & MOUSEEVENTF_MOVE]
    assert len(moves_v) == 2 and all(ev.x == moves_v[0].x for ev in moves_v)

    fake_adapter = FakeWin32Adapter(foreground_id=1001, virtual_rect=(0, 0, 1280, 720))
    fake_adapter.window_at_point_id = 2002
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1280, 720), win32_adapter=fake_adapter
    )
    backend.start()
    try:
        backend.observe("run-scroll")
        backend.execute("run-scroll", ComputerAction(kind="scroll", target="desktop", delta_y=2))
        backend.execute(
            "run-scroll",
            ComputerAction(kind="scroll", target="desktop", x=200, y=200, delta_y=-2),
        )
        assert fake_adapter.set_foreground_calls == []
        assert fake_adapter.wheels == [2, -2]
        assert fake_adapter.moves == [(200, 200)]
    finally:
        backend.close()


def _foreground_user32(*, become_foreground_on_call):
    """become_foreground_on_call: 第几次 SetForegroundWindow 之后前台变成目标。0 表示始终拒绝。"""
    state = {"fg": 1001, "sets": 0}
    attach_calls = []
    bring_calls = []
    keybd_calls = []

    class _SetFgFn:
        def __call__(self, hwnd):
            state["sets"] += 1
            if become_foreground_on_call and state["sets"] >= become_foreground_on_call:
                state["fg"] = int(hwnd)
            return False

    class _GetFgFn:
        def __call__(self):
            return state["fg"]

    class _FakeUser32:
        SetForegroundWindow = _SetFgFn()
        GetForegroundWindow = _GetFgFn()

        def GetWindowThreadProcessId(self, hwnd, pid):
            return 42

        def AttachThreadInput(self, src, dst, attach):
            attach_calls.append((int(src), int(dst), bool(attach)))
            return True

        def BringWindowToTop(self, hwnd):
            bring_calls.append(int(hwnd))
            return True

        def keybd_event(self, *args):
            keybd_calls.append(args)

    class _FakeKernel32:
        def GetCurrentThreadId(self):
            return 7

    windll = type("_W", (), {"user32": _FakeUser32(), "kernel32": _FakeKernel32()})()
    return windll, state, attach_calls, bring_calls, keybd_calls


def test_ctypes_set_foreground_window_attaches_thread_then_verifies(monkeypatch):
    import core.computer.win32_adapter as wa

    windll, _state, attach_calls, bring_calls, keybd_calls = _foreground_user32(
        become_foreground_on_call=2
    )
    monkeypatch.setattr(wa.ctypes, "windll", windll)
    assert wa._ctypes_set_foreground_window(2002) is True
    assert attach_calls == [(7, 42, True), (7, 42, False)]
    assert bring_calls == [2002]
    assert keybd_calls == []


def test_ctypes_set_foreground_window_detaches_when_still_rejected(monkeypatch):
    import core.computer.win32_adapter as wa

    windll, _state, attach_calls, _bring_calls, keybd_calls = _foreground_user32(
        become_foreground_on_call=0
    )
    monkeypatch.setattr(wa.ctypes, "windll", windll)
    assert wa._ctypes_set_foreground_window(2002) is False
    assert attach_calls == [(7, 42, True), (7, 42, False)]
    assert keybd_calls == []


def test_ctypes_set_foreground_window_skips_attach_when_already_foreground(monkeypatch):
    import core.computer.win32_adapter as wa

    windll, _state, attach_calls, bring_calls, keybd_calls = _foreground_user32(
        become_foreground_on_call=1
    )
    monkeypatch.setattr(wa.ctypes, "windll", windll)
    assert wa._ctypes_set_foreground_window(2002) is True
    assert attach_calls == []
    assert bring_calls == []
    assert keybd_calls == []


def test_observe_retries_once_when_foreground_changes_during_capture():
    adapter = FakeWin32Adapter(foreground_id=1001, virtual_rect=(0, 0, 1280, 720))
    grabs = {"n": 0}

    def grab():
        grabs["n"] += 1
        if grabs["n"] == 1:
            adapter.foreground_id = 2002
        return FakeImage(1280, 720)

    backend = WindowsComputerBackend(image_grab=grab, win32_adapter=adapter)
    backend._started = True
    try:
        observation = backend.observe("run-retry")
        assert observation.ok is True
        assert observation.surface_id == "hwnd:2002"
        assert grabs["n"] == 2
    finally:
        backend.close()


def test_observe_retries_once_when_foreground_changes_during_element_collect():
    adapter = FakeWin32Adapter(foreground_id=1001, virtual_rect=(0, 0, 1280, 720))

    class _DriftLocator:
        def __init__(self):
            self.calls = 0

        def collect(self, hwnd):
            self.calls += 1
            if self.calls == 1:
                adapter.foreground_id = 2002
            return []

        def invalidate(self):
            return None

        def close(self):
            return None

    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1280, 720),
        win32_adapter=adapter,
        element_locator=_DriftLocator(),
    )
    backend._started = True
    try:
        observation = backend.observe("run-retry-collect")
        assert observation.ok is True
        assert observation.surface_id == "hwnd:2002"
    finally:
        backend.close()


def test_observe_crops_secondary_monitor_foreground_window():
    from core.computer.windows_backend import screenshot_to_physical

    adapter = FakeWin32Adapter(
        foreground_id=1001, virtual_rect=(0, -1080, 1920, 2280)
    )
    adapter.foreground_window_rect = lambda: (-8, -1088, 1928, 8)
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 2280), win32_adapter=adapter
    )
    backend._started = True
    try:
        observation = backend.observe("run-crop")
        assert observation.ok is True
        assert observation.origin_x == 0
        assert observation.origin_y == -1080
        assert observation.original_width == 1920
        assert observation.original_height == 1088
        assert screenshot_to_physical(0, 0, observation) == (0, -1080)
    finally:
        backend.close()


def test_observe_drops_fullscreen_caption_button_and_notes_self_drawn_ui():
    adapter = FakeWin32Adapter(
        foreground_id=1001,
        virtual_rect=(0, -1080, 1920, 2280),
        foreground_title="iMOM Agent",
    )
    adapter.foreground_window_rect = lambda: (-8, -1088, 1928, 8)

    class _ChromeLocator:
        def collect(self, hwnd):
            return [
                {
                    "ref": "el-1",
                    "kind": "button",
                    "name": "关闭",
                    "bounds": (0, -1080, 1920, 0),
                    "enabled": True,
                },
                {
                    "ref": "el-2",
                    "kind": "button",
                    "name": "系统",
                    "bounds": (0, -1080, 22, -1058),
                    "enabled": True,
                },
            ]

        def invalidate(self):
            return None

        def close(self):
            return None

    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 2280),
        win32_adapter=adapter,
        element_locator=_ChromeLocator(),
    )
    backend._started = True
    try:
        observation = backend.observe("run-chrome")
        names = [element["name"] for element in observation.elements]
        assert names == ["系统"]
        assert "自绘界面" in observation.ui_note
        assert "截图坐标" in observation.ui_note
    finally:
        backend.close()


def test_sequence_click_other_window_then_type_text_succeeds():
    from core.computer.actions import parse_computer_action
    from core.computer.controller import ComputerControlService
    from core.computer.models import ComputerCapability, ComputerOwner

    adapter = FakeWin32Adapter(foreground_id=1001, virtual_rect=(0, 0, 1280, 720))
    adapter.window_at_point_id = 2002
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1280, 720), win32_adapter=adapter
    )
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 0,
    )
    owner = ComputerOwner("s1", "t1", "测试", threading.Event())
    cap = ComputerCapability(
        enabled=True, protocol="generic", targets=("desktop",), settings_generation=0
    )
    snap = service.start_run(owner, cap, target="desktop")
    try:
        service.execute(
            snap.run_id, owner, ComputerAction(kind="observe", target="desktop")
        )
        seq = parse_computer_action(
            {
                "kind": "sequence",
                "target": "desktop",
                "steps": [
                    {"kind": "click", "target": "desktop", "x": 100, "y": 100},
                    {"kind": "type_text", "target": "desktop", "text": "hello"},
                    {"kind": "keypress", "target": "desktop", "keys": ["ENTER"]},
                ],
            }
        )
        obs = service.execute(snap.run_id, owner, seq)
        assert obs.ok is True
        assert obs.surface_id == "hwnd:2002"
        assert adapter.set_foreground_calls == [2002]
        assert adapter.texts == ["hello"]
        assert adapter.keys_sent == [("ENTER",)]
    finally:
        service.stop(snap.run_id, "test")


def test_sequence_alt_tab_allows_later_keypress_but_rejects_stale_click():
    from core.computer.actions import parse_computer_action
    from core.computer.controller import ComputerControlService
    from core.computer.models import ComputerCapability, ComputerOwner

    class _SwitchAdapter(FakeWin32Adapter):
        def send_keys(self, keys):
            super().send_keys(keys)
            if tuple(keys) == ("ALT", "TAB"):
                self.foreground_id = 2002
                self.foreground_title = "IDEA"

    adapter = _SwitchAdapter(foreground_id=1001, virtual_rect=(0, 0, 1280, 720))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1280, 720), win32_adapter=adapter
    )
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 0,
    )
    owner = ComputerOwner("s1", "t1", "测试", threading.Event())
    cap = ComputerCapability(
        enabled=True, protocol="generic", targets=("desktop",), settings_generation=0
    )
    snap = service.start_run(owner, cap, target="desktop")
    try:
        service.execute(
            snap.run_id, owner, ComputerAction(kind="observe", target="desktop")
        )
        seq_keys = parse_computer_action(
            {
                "kind": "sequence",
                "target": "desktop",
                "steps": [
                    {"kind": "keypress", "target": "desktop", "keys": ["ALT", "TAB"]},
                    {"kind": "wait", "target": "desktop", "duration_ms": 100},
                    {
                        "kind": "keypress",
                        "target": "desktop",
                        "keys": ["CTRL", "SHIFT", "A"],
                    },
                ],
            }
        )
        obs = service.execute(snap.run_id, owner, seq_keys)
        assert obs.ok is True
        assert adapter.keys_sent == [("ALT", "TAB"), ("CTRL", "SHIFT", "A")]

        adapter.foreground_id = 2002
        seq_click = parse_computer_action(
            {
                "kind": "sequence",
                "target": "desktop",
                "steps": [
                    {"kind": "keypress", "target": "desktop", "keys": ["ALT", "TAB"]},
                    {"kind": "click", "target": "desktop", "x": 10, "y": 10},
                ],
            }
        )
        # Re-establish baseline on 2002, then switch on ALT+TAB -> click must reject
        adapter.send_keys = lambda keys: (
            FakeWin32Adapter.send_keys(adapter, keys),
            setattr(adapter, "foreground_id", 3003)
            if tuple(keys) == ("ALT", "TAB")
            else None,
        )[0]
        with pytest.raises(ComputerControlError) as raised:
            service.execute(snap.run_id, owner, seq_click)
        assert raised.value.code == "computer_surface_changed"
        assert raised.value.details["failed_step_index"] == 1
    finally:
        service.stop(snap.run_id, "test")


def test_observe_region_clips_maximized_window_dwm_border():
    from core.computer.actions import parse_computer_action
    from tests.test_computer_surfaces import FakeSurfaceLocator

    locator = FakeSurfaceLocator(
        windows=[{"label": "IDEA", "bounds": (-8, -1088, 1936, 1096)}]
    )
    adapter = FakeWin32Adapter(foreground_id=1001, virtual_rect=(0, -1080, 1920, 2280))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1920, 2280),
        win32_adapter=adapter,
        surface_locator=locator,
    )
    backend.start()
    try:
        backend.execute("run-r", ComputerAction(kind="list_surfaces", target="desktop"))
        candidate = backend.observe("run-r").surface_candidates[0]
        backend.execute(
            "run-r",
            parse_computer_action(
                {
                    "kind": "observe_region",
                    "target": "desktop",
                    "surface_ref": candidate["ref"],
                    "surface_generation": candidate["surface_generation"],
                }
            ),
        )
        region_obs = backend.observe("run-r")
        assert region_obs.ok is True
        assert (region_obs.origin_x, region_obs.origin_y) == (0, -1080)
        assert (region_obs.original_width, region_obs.original_height) == (1920, 1088)
    finally:
        backend.close()


def test_observe_passes_bounded_deadline_to_collect():
    seen = {}

    class _DeadlineLocator:
        def collect(self, hwnd, *, deadline=None, cancel_event=None):
            seen["deadline"] = deadline
            seen["cancel_event"] = cancel_event
            return []

        def invalidate(self):
            return None

        def close(self):
            return None

    adapter = FakeWin32Adapter(foreground_id=1001, virtual_rect=(0, 0, 1280, 720))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1280, 720),
        win32_adapter=adapter,
        element_locator=_DeadlineLocator(),
    )
    backend._started = True
    try:
        obs = backend.observe("run-dl")
        assert obs.ok is True
        assert seen["deadline"] is not None
        assert seen["cancel_event"] is backend._stop_event
    finally:
        backend.close()


def test_wait_for_probes_live_foreground_hwnd_when_window_activates():
    adapter = FakeWin32Adapter(
        foreground_id=1001, foreground_title="StarYao", virtual_rect=(0, 0, 1280, 720)
    )
    probed_hwnds = []

    class _ProbeLocator:
        def probe(self, hwnd, matcher, deadline=None, cancel_event=None):
            probed_hwnds.append(hwnd)
            return {"condition": "text_exists", "name": "Ready"} if hwnd == 2002 else None

        def close(self):
            return None

    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1280, 720),
        win32_adapter=adapter,
        element_locator=_ProbeLocator(),
    )
    backend._started = True

    def _switch():
        import time

        time.sleep(0.1)
        adapter.foreground_id = 2002
        adapter.foreground_title = "IDEA Target"

    worker = threading.Thread(target=_switch, daemon=True)
    worker.start()
    try:
        backend.execute(
            "run-w",
            ComputerAction(
                kind="wait_for",
                target="desktop",
                condition="text_exists",
                text="Ready",
                window_name="IDEA",
                timeout_ms=2000,
                interval_ms=50,
            ),
        )
        assert 2002 in probed_hwnds
    finally:
        worker.join(timeout=2)
        backend.close()


def test_ctypes_set_foreground_window_attaches_distinct_foreground_and_target_threads(
    monkeypatch,
):
    import core.computer.win32_adapter as wa

    windll, _state, attach_calls, bring_calls, _keybd_calls = _foreground_user32(
        become_foreground_on_call=2
    )
    windll.user32.GetWindowThreadProcessId = lambda hwnd, pid: (
        101 if int(hwnd) == 1001 else 202
    )
    monkeypatch.setattr(wa.ctypes, "windll", windll)
    assert wa._ctypes_set_foreground_window(2002) is True
    assert attach_calls == [
        (7, 101, True),
        (7, 202, True),
        (7, 202, False),
        (7, 101, False),
    ]
    assert bring_calls == [2002]


def test_observe_recollects_fresh_elements_on_same_surface():
    calls = {"n": 0}

    class _DynamicLocator:
        def collect(self, hwnd, *, deadline=None, cancel_event=None):
            calls["n"] += 1
            label = "旧按钮" if calls["n"] == 1 else "新按钮"
            return [
                {
                    "ref": "el-1",
                    "kind": "ButtonControl",
                    "name": label,
                    "bounds": (10, 10, 80, 40),
                    "enabled": True,
                }
            ]

        def invalidate(self):
            return None

        def close(self):
            return None

    adapter = FakeWin32Adapter(foreground_id=1001, virtual_rect=(0, 0, 1280, 720))
    backend = WindowsComputerBackend(
        image_grab=lambda: FakeImage(1280, 720),
        win32_adapter=adapter,
        element_locator=_DynamicLocator(),
    )
    backend._started = True
    try:
        obs1 = backend.observe("run-dyn")
        backend.execute(
            "run-dyn", ComputerAction(kind="click", target="desktop", x=20, y=20)
        )
        obs2 = backend.observe("run-dyn")
        assert calls["n"] == 2
        assert obs1.elements[0]["name"] == "旧按钮"
        assert obs2.elements[0]["name"] == "新按钮"
    finally:
        backend.close()
