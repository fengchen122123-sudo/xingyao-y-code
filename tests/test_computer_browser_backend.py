import hashlib
import threading
import time
from pathlib import Path

import pytest

from core.computer.actions import observation_provider_projection, parse_computer_action
from core.computer.browser_backend import BrowserComputerBackend, _SCREENSHOT_TIMEOUT_MS
from core.computer.models import ComputerAction, ComputerControlError
from tests.computer_fakes import FakePage, FakePlaywrightRuntime, fake_png_bytes


@pytest.fixture
def runtime():
    return FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(6))


@pytest.fixture
def backend(tmp_path, runtime):
    instance = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    instance.start()
    yield instance
    instance.close()


def test_browser_backend_uses_visible_persistent_system_channel(tmp_path):
    runtime = FakePlaywrightRuntime()
    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    backend.start()
    assert runtime.chromium.launch_persistent_context_calls == [{
        "user_data_dir": str((tmp_path / "edge").resolve()),
        "channel": "msedge",
        "headless": False,
        "viewport": {"width": 1440, "height": 900},
        "device_scale_factor": 1,
    }]


def test_selected_browser_failure_does_not_fallback(tmp_path):
    runtime = FakePlaywrightRuntime(error=RuntimeError("msedge missing"))
    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    with pytest.raises(ComputerControlError) as raised:
        backend.start()
    assert raised.value.code == "computer_browser_unavailable"
    assert runtime.requested_channels == ["msedge"]


def test_closed_active_page_fails_instead_of_reopening(backend):
    backend.active_page.closed = True
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_browser_closed"


def test_request_stop_rejects_actions_but_keeps_input_release_available(backend):
    request_stop = getattr(backend, "request_stop", None)
    assert callable(request_stop)

    backend._pressed_buttons.add("left")
    request_stop()

    with pytest.raises(ComputerControlError) as raised:
        backend.execute(
            "run-stop", ComputerAction(kind="click", target="browser", x=5, y=5)
        )
    assert raised.value.code == "computer_cancelled"

    backend.release_inputs()
    assert backend._pressed_buttons == set()


def test_constructor_does_not_import_or_start_playwright(tmp_path):
    calls = []

    def factory():
        calls.append(1)
        raise AssertionError("factory 不得在构造函数中调用")

    instance = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=factory)
    assert calls == []
    assert instance.target == "browser"
    assert instance.browser == "edge"


def test_unknown_browser_is_rejected(tmp_path):
    with pytest.raises(ComputerControlError) as raised:
        BrowserComputerBackend("firefox", tmp_path / "firefox", playwright_factory=FakePlaywrightRuntime)
    assert raised.value.code == "computer_invalid_config"


def test_chrome_maps_to_chrome_channel(tmp_path):
    runtime = FakePlaywrightRuntime()
    backend = BrowserComputerBackend("chrome", tmp_path / "chrome", playwright_factory=lambda: runtime)
    backend.start()
    try:
        assert runtime.requested_channels == ["chrome"]
        assert runtime.chromium.launch_persistent_context_calls[0]["channel"] == "chrome"
    finally:
        backend.close()


def test_commands_before_start_fail_explicitly(tmp_path):
    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=FakePlaywrightRuntime)
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_browser_unavailable"


def test_factory_failure_is_explicit_start_error(tmp_path):
    def factory():
        raise RuntimeError("playwright not installed")

    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=factory)
    with pytest.raises(ComputerControlError) as raised:
        backend.start()
    assert raised.value.code == "computer_backend_start_failed"


def test_runtime_start_failure_is_explicit(tmp_path):
    runtime = FakePlaywrightRuntime(start_error=RuntimeError("driver missing"))
    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    with pytest.raises(ComputerControlError) as raised:
        backend.start()
    assert raised.value.code == "computer_backend_start_failed"


def test_command_timeout_is_explicit(tmp_path):
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(2))
    backend = BrowserComputerBackend(
        "edge", tmp_path / "edge", playwright_factory=lambda: runtime, command_timeout=0.2
    )
    backend.start()
    gate = threading.Event()
    backend.active_page.screenshot_gate = gate
    try:
        with pytest.raises(ComputerControlError) as raised:
            backend.observe("run-timeout")
        assert raised.value.code == "computer_action_timeout"
    finally:
        gate.set()
        backend.close()


def test_all_playwright_calls_run_on_single_worker_thread(tmp_path):
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(1))
    factory_threads = []

    def factory():
        factory_threads.append(threading.get_ident())
        return runtime

    backend = BrowserComputerBackend("chrome", tmp_path / "chrome", playwright_factory=factory)
    backend.start()
    try:
        backend.observe("run-t")
        backend.execute("run-t", ComputerAction(kind="move", target="browser", x=10, y=10))
        backend.execute("run-t", ComputerAction(kind="click", target="browser", x=10, y=10))
        backend.release_inputs()
    finally:
        backend.close()
    assert factory_threads == [runtime.start_thread_id]
    assert runtime.start_thread_id is not None
    assert runtime.start_thread_id != threading.get_ident()
    assert runtime.call_thread_ids
    assert set(runtime.call_thread_ids) == {runtime.start_thread_id}


def test_concurrent_calls_are_serialized_on_worker(backend, runtime):
    page = backend.active_page
    page.screenshot_delay = 0.02
    errors = []

    def work():
        try:
            for _ in range(3):
                backend.observe("run-c")
        except Exception as error:  # noqa: BLE001 - 测试聚合所有并发错误
            errors.append(error)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert page.concurrency_violations == 0
    assert len(page.screenshot_calls) == 12
    assert set(runtime.call_thread_ids) == {runtime.start_thread_id}


def test_profile_lock_conflict_is_explicit(tmp_path):
    runtime_a = FakePlaywrightRuntime()
    runtime_b = FakePlaywrightRuntime()
    first = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime_a)
    first.start()
    try:
        second = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime_b)
        with pytest.raises(ComputerControlError) as raised:
            second.start()
        assert raised.value.code == "computer_profile_locked"
        assert runtime_b.started is False
    finally:
        first.close()
    third = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: FakePlaywrightRuntime())
    third.start()
    third.close()


def test_profile_lock_conflict_from_launch_error(tmp_path):
    runtime = FakePlaywrightRuntime(
        error=RuntimeError("Failed to create a ProcessSingleton for your profile directory: already in use")
    )
    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    with pytest.raises(ComputerControlError) as raised:
        backend.start()
    assert raised.value.code == "computer_profile_locked"


def test_zero_pages_creates_exactly_one_about_blank(backend, runtime):
    context = runtime.last_context
    assert context is not None
    assert context.new_page_calls == 1
    assert len(context.pages) == 1
    assert context.pages[0].goto_calls == ["about:blank"]
    assert backend.active_page is context.pages[0]


def test_existing_pages_are_reused_and_last_is_active(tmp_path):
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(3))
    first = FakePage(runtime, url="https://a.example/", title="A")
    second = FakePage(runtime, url="https://b.example/", title="B")
    runtime.initial_pages = [first, second]
    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    backend.start()
    try:
        context = runtime.last_context
        assert context.new_page_calls == 0
        assert len(context.pages) == 2
        assert backend.active_page is second
    finally:
        backend.close()


def test_popup_becomes_active_page_and_emits_event(tmp_path, runtime):
    changes = []
    backend = BrowserComputerBackend(
        "edge",
        tmp_path / "edge",
        playwright_factory=lambda: runtime,
        on_page_change=lambda: changes.append("changed"),
    )
    backend.start()
    try:
        original = backend.active_page
        assert original is not None
        popup = runtime.last_context.simulate_popup(url="https://popup.example/", title="Popup")
        assert changes == ["changed"]
        assert backend.active_page is original
        observation = backend.observe("run-p")
        assert observation.page_title == "Popup"
        assert observation.page_origin == "https://popup.example"
        assert backend.active_page is popup
        first_surface = observation.surface_id
        popup.visibility_state = "hidden"
        popup.focused = False
        original.visibility_state = "visible"
        original.focused = True
        with pytest.raises(ComputerControlError) as raised:
            backend.execute(
                "run-p",
                parse_computer_action({"kind": "click", "target": "browser", "x": 10, "y": 10}),
            )
        assert raised.value.code == "computer_surface_changed"
        assert original.mouse.calls == []
        refreshed = backend.observe("run-p")
        assert refreshed.page_title == original.page_title
        assert refreshed.surface_generation > observation.surface_generation
        assert refreshed.surface_id != first_surface
    finally:
        backend.close()


def test_context_close_marks_backend_closed(backend, runtime):
    runtime.last_context.close()
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_browser_closed"


def test_close_is_idempotent_and_safe_without_start(tmp_path):
    runtime = FakePlaywrightRuntime()
    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    backend.close()
    backend.start()
    backend.close()
    backend.close()
    assert runtime.stopped is True
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_browser_closed"


def test_observe_returns_png_sha256_and_fixed_viewport(backend):
    observation = backend.observe("run-a")
    png = fake_png_bytes(6)
    assert observation.screen_sha256 == hashlib.sha256(png).hexdigest()
    assert (observation.width, observation.height) == (1440, 900)
    assert (observation.original_width, observation.original_height) == (1440, 900)
    assert observation.target == "browser"
    assert observation.run_id == "run-a"
    assert observation.ok is True
    assert Path(observation.png_path).read_bytes() == png
    assert backend.active_page.screenshot_calls == [
        {"type": "png", "timeout": _SCREENSHOT_TIMEOUT_MS}
    ]


def test_provider_projection_only_sees_origin_and_truncated_title(backend):
    page = backend.active_page
    page.url = "https://example.com/path?token=secret#frag"
    page.page_title = "标" * 300
    observation = backend.observe("run-a")
    assert observation.page_origin == "https://example.com"
    assert len(observation.page_title) == 200
    projection = observation_provider_projection(observation)
    assert projection["origin_or_window"] == "https://example.com"
    assert "secret" not in repr(projection)
    assert "frag" not in repr(projection)


def test_screenshot_failure_degrades_to_dom_observation(backend):
    page = backend.active_page
    page.page_text = "示例页面正文"
    page.screenshot_error = RuntimeError("gpu gone")
    observation = backend.observe("run-a")
    assert observation.ok is True
    assert observation.png_path == ""
    assert observation.screen_sha256 == ""
    assert observation.page_text == "示例页面正文"
    assert "RuntimeError" in observation.screenshot_error
    assert "gpu gone" not in observation.screenshot_error
    assert "http" not in observation.screenshot_error  # 绝不带页面 URL
    page.screenshot_error = None
    observation = backend.observe("run-a")
    assert observation.screenshot_error == ""
    assert observation.screen_sha256 == hashlib.sha256(fake_png_bytes(6)).hexdigest()
    assert Path(observation.png_path).read_bytes() == fake_png_bytes(6)


def test_page_text_failure_does_not_break_observe(backend):
    page = backend.active_page
    page.page_text_error = RuntimeError("evaluate timeout")
    observation = backend.observe("run-a")
    assert observation.ok is True
    assert observation.page_text == ""
    assert observation.screenshot_error == ""
    assert Path(observation.png_path).read_bytes() == fake_png_bytes(6)


def test_surface_change_during_degraded_observation_is_still_discarded(backend):
    page = backend.active_page
    other = backend._context.new_page()
    other.visibility_state = "hidden"
    other.focused = False

    def switch_then_fail(**kwargs):
        page.visibility_state = "hidden"
        page.focused = False
        other.visibility_state = "visible"
        other.focused = True
        raise RuntimeError("frame not produced while occluded")

    page.screenshot = switch_then_fail
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_surface_changed"


def test_navigate_reuses_parse_layer_url_validation(backend):
    page = backend.active_page
    for bad in ("javascript:alert(1)", "ftp://example.com/", "file:///etc/passwd", ""):
        with pytest.raises(ComputerControlError) as raised:
            backend.execute("run-a", ComputerAction(kind="navigate", target="browser", url=bad))
        assert raised.value.code == "computer_invalid_action"
    backend.execute("run-a", ComputerAction(kind="navigate", target="browser", url="https://example.com/path?q=1"))
    assert page.goto_calls[-1] == "https://example.com/path?q=1"


def test_back_forward_reload_map_to_page(backend):
    page = backend.active_page
    backend.execute("run-a", ComputerAction(kind="back", target="browser"))
    backend.execute("run-a", ComputerAction(kind="forward", target="browser"))
    backend.execute("run-a", ComputerAction(kind="reload", target="browser"))
    assert page.navigation_calls == ["go_back", "go_forward", "reload"]


@pytest.mark.parametrize("x,y", [(-1, 0), (0, -1), (1440, 0), (0, 900), (1440, 900)])
def test_coordinate_out_of_bounds_rejected(backend, x, y):
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", ComputerAction(kind="click", target="browser", x=x, y=y))
    assert raised.value.code == "computer_coordinate_out_of_bounds"


@pytest.mark.parametrize("x,y", [(0, 0), (1439, 899)])
def test_coordinate_boundary_values_allowed(backend, x, y):
    backend.execute("run-a", ComputerAction(kind="click", target="browser", x=x, y=y))
    assert ("click", x, y, "left") in backend.active_page.mouse.calls


def test_mouse_and_keyboard_action_mapping(backend):
    page = backend.active_page
    backend.execute("r", ComputerAction(kind="click", target="browser", x=1, y=2, button="right"))
    backend.execute("r", ComputerAction(kind="double_click", target="browser", x=3, y=4))
    backend.execute("r", ComputerAction(kind="move", target="browser", x=5, y=6))
    backend.execute("r", ComputerAction(kind="scroll", target="browser", x=7, y=8, delta_x=10, delta_y=-20))
    backend.execute("r", ComputerAction(kind="type_text", target="browser", text="你好"))
    backend.execute("r", ComputerAction(kind="keypress", target="browser", keys=("CTRL", "A")))
    assert page.mouse.calls == [
        ("click", 1, 2, "right"),
        ("dblclick", 3, 4, "left"),
        ("move", 5, 6),
        ("move", 7, 8),
        ("wheel", 10, -20),
    ]
    assert page.keyboard.calls == [("insert_text", "你好"), ("press", "Control+a")]


def test_drag_releases_mouse_button_on_failure(backend):
    page = backend.active_page
    page.mouse.move_fail_after = 1
    with pytest.raises(ComputerControlError):
        backend.execute("r", ComputerAction(kind="drag", target="browser", path=((1, 1), (2, 2), (3, 3))))
    names = [call[0] for call in page.mouse.calls]
    assert names == ["move", "down", "move", "up"]


def test_release_inputs_releases_stuck_buttons(backend):
    page = backend.active_page
    page.mouse.up_error = RuntimeError("up failed once")
    with pytest.raises(ComputerControlError):
        backend.execute("r", ComputerAction(kind="drag", target="browser", path=((1, 1), (2, 2))))
    page.mouse.up_error = None
    backend.release_inputs()
    ups = [call for call in page.mouse.calls if call[0] == "up"]
    assert len(ups) == 2


def test_observe_action_kind_is_noop_in_execute(backend):
    backend.execute("r", ComputerAction(kind="observe", target="browser"))
    assert backend.active_page.screenshot_calls == []


def test_wait_action_sleeps_without_touching_page(backend, runtime):
    before = len(runtime.call_thread_ids)
    backend.execute("r", ComputerAction(kind="wait", target="browser", duration_ms=100))
    assert len(runtime.call_thread_ids) == before


def test_desktop_target_is_rejected(backend):
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("r", ComputerAction(kind="click", target="desktop", x=1, y=1))
    assert raised.value.code == "computer_invalid_action"


def test_request_user_takeover_is_not_a_backend_action(backend):
    with pytest.raises(ComputerControlError) as raised:
        backend.execute(
            "r",
            ComputerAction(kind="request_user_takeover", target="browser", reason_code="login_required"),
        )
    assert raised.value.code == "computer_invalid_action"


def test_start_timeout_posts_sentinel_so_late_boot_worker_exits(tmp_path):
    release = threading.Event()

    class SlowRuntime(FakePlaywrightRuntime):
        def start(self):
            release.wait(timeout=10)
            return super().start()

    runtime = SlowRuntime()
    backend = BrowserComputerBackend(
        "edge",
        tmp_path / "edge",
        playwright_factory=lambda: runtime,
        start_timeout=0.2,
    )
    with pytest.raises(ComputerControlError) as raised:
        backend.start()
    assert raised.value.code == "computer_backend_start_failed"

    release.set()
    worker = backend._worker
    worker.join(timeout=10)
    assert not worker.is_alive()  # worker 必须退出，绝不滞留命令循环
    assert runtime.stopped
    assert runtime.last_context.closed


def test_timed_out_queued_command_never_executes_later(tmp_path):
    runtime = FakePlaywrightRuntime()
    backend = BrowserComputerBackend(
        "edge",
        tmp_path / "edge",
        playwright_factory=lambda: runtime,
        command_timeout=2.0,
    )
    picked = threading.Event()
    gate = threading.Event()
    worker_finished = threading.Event()
    side_effect = threading.Event()

    def blocked():
        picked.set()
        gate.wait(timeout=5)
        worker_finished.set()

    backend.start()
    caller = threading.Thread(
        target=lambda: backend._call("blocked operation", blocked), daemon=True
    )
    try:
        caller.start()
        assert picked.wait(timeout=5), "worker 未领取阻塞命令"
        with pytest.raises(ComputerControlError) as raised:
            backend._call("queued input", side_effect.set, timeout=0.05)
        assert raised.value.code == "computer_action_timeout"
        gate.set()
        caller.join(timeout=5)
        assert not caller.is_alive()
        assert worker_finished.wait(timeout=5), "worker 未完成阻塞命令"
        backend._queue.join()
        assert not side_effect.is_set(), "已经返回超时的排队输入仍被 worker 执行"
        backend._call("still usable", lambda: None)
    finally:
        gate.set()
        backend.close()


def test_running_command_timeout_seals_instance_and_holds_profile(tmp_path):
    runtime = FakePlaywrightRuntime()
    profile = tmp_path / "edge"
    backend = BrowserComputerBackend(
        "edge", profile, playwright_factory=lambda: runtime, command_timeout=0.05
    )
    picked = threading.Event()
    gate = threading.Event()

    def stuck():
        picked.set()
        gate.wait(timeout=10)

    backend.start()
    try:
        with pytest.raises(ComputerControlError) as raised:
            backend._call("stuck input", stuck)
        assert picked.wait(timeout=5)
        assert raised.value.code == "computer_action_timeout"
        assert "结果未知" in raised.value.message
        with pytest.raises(ComputerControlError) as sealed:
            backend._call("post-seal input", lambda: None)
        assert sealed.value.code == "computer_backend_error"
        # worker 仍卡在 stuck：profile 执行权绝不提前释放给新实例。
        second_runtime = FakePlaywrightRuntime()
        second = BrowserComputerBackend(
            "edge", profile, playwright_factory=lambda: second_runtime
        )
        with pytest.raises(ComputerControlError) as locked:
            second.start()
        assert locked.value.code == "computer_profile_locked"
        gate.set()
        backend.close()
        third_runtime = FakePlaywrightRuntime()
        third = BrowserComputerBackend(
            "edge", profile, playwright_factory=lambda: third_runtime
        )
        third.start()
        third.close()
    finally:
        gate.set()
        backend.close()


def test_submit_lock_wait_counts_into_command_deadline(backend):
    assert backend._submit_lock.acquire(timeout=1)
    try:
        with pytest.raises(ComputerControlError) as raised:
            backend._call("blocked by submit lock", lambda: None, timeout=0.05)
        assert raised.value.code == "computer_action_timeout"
        assert "提交等待超时" in raised.value.message
    finally:
        backend._submit_lock.release()
    backend._call("after lock released", lambda: None)


def test_command_claim_and_cancel_are_atomic_states():
    from core.computer.browser_backend import _Command

    expired = _Command(1, lambda: None, deadline=time.monotonic() - 1)
    assert not expired.try_start(time.monotonic())
    assert expired.state == "cancelled"
    assert expired.done.is_set()

    command = _Command(2, lambda: None, deadline=time.monotonic() + 60)
    assert command.cancel_if_queued("computer_browser_closed", "已关闭")
    assert not command.try_start(time.monotonic())

    running = _Command(3, lambda: None, deadline=time.monotonic() + 60)
    assert running.try_start(time.monotonic())
    assert not running.cancel_if_queued("computer_browser_closed", "已关闭")
    running.finish()
    assert running.state == "succeeded"


def test_close_cancels_all_queued_commands_without_executing(tmp_path):
    runtime = FakePlaywrightRuntime()
    backend = BrowserComputerBackend(
        "edge",
        tmp_path / "edge",
        playwright_factory=lambda: runtime,
        command_timeout=10.0,
    )
    picked = threading.Event()
    gate = threading.Event()
    executed = []
    outcomes = []

    def blocked():
        picked.set()
        gate.wait(timeout=10)

    backend.start()
    leader = threading.Thread(
        target=lambda: backend._call("blocked", blocked), daemon=True
    )
    leader.start()
    assert picked.wait(timeout=5)
    assert backend._queue.empty()

    def submit(index):
        try:
            backend._call(f"queued-{index}", lambda: executed.append(index))
            outcomes.append("executed")
        except ComputerControlError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=submit, args=(index,), daemon=True) for index in range(8)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    while not backend._queue.full() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert backend._queue.full(), "排队命令未填满队列"
    overflow = backend._call
    with pytest.raises(ComputerControlError) as raised:
        overflow("overflow", lambda: executed.append(99), timeout=0.05)
    assert raised.value.code == "computer_action_timeout"

    closer = threading.Thread(target=backend.close, daemon=True)
    closer.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive(), "close 后调用线程仍悬挂"
    assert executed == [], "close 后排队命令仍被执行"
    assert outcomes and all(code == "computer_browser_closed" for code in outcomes)
    gate.set()
    closer.join(timeout=5)
    assert not closer.is_alive()
    leader.join(timeout=5)


def test_close_keeps_input_release_ahead_of_worker_shutdown(tmp_path):
    runtime = FakePlaywrightRuntime()
    backend = BrowserComputerBackend(
        "edge", tmp_path / "edge", playwright_factory=lambda: runtime
    )
    backend.start()
    running = threading.Event()
    allow_running_command_to_finish = threading.Event()
    release_executed = threading.Event()
    release_errors = []
    backend._cmd_release_inputs = release_executed.set

    def blocked_command():
        running.set()
        assert allow_running_command_to_finish.wait(timeout=3)

    leader = threading.Thread(
        target=lambda: backend._call("in-flight", blocked_command), daemon=True
    )
    leader.start()
    assert running.wait(timeout=2)
    backend.request_stop()

    def release():
        try:
            backend.release_inputs()
        except ComputerControlError as error:
            release_errors.append(error)

    releaser = threading.Thread(target=release, daemon=True)
    releaser.start()
    deadline = time.monotonic() + 2
    while backend._queue.empty() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert backend._queue.qsize() == 1

    close_errors = []

    def close():
        try:
            backend.close()
        except ComputerControlError as error:
            close_errors.append(error)

    closer = threading.Thread(target=close, daemon=True)
    closer.start()
    allow_running_command_to_finish.set()
    leader.join(timeout=3)
    releaser.join(timeout=3)
    closer.join(timeout=3)

    assert not leader.is_alive()
    assert not releaser.is_alive()
    assert not closer.is_alive()
    assert release_executed.is_set()
    assert release_errors == []
    assert close_errors == []


def test_close_reports_worker_that_has_not_exited_and_allows_confirmation_retry(
    tmp_path, monkeypatch
):
    import core.computer.browser_backend as browser_backend_module

    runtime = FakePlaywrightRuntime()
    backend = BrowserComputerBackend(
        "edge", tmp_path / "edge", playwright_factory=lambda: runtime
    )
    backend.start()
    worker_entered = threading.Event()
    allow_worker_to_exit = threading.Event()

    def blocked_command():
        worker_entered.set()
        assert allow_worker_to_exit.wait(timeout=3)

    caller = threading.Thread(
        target=lambda: backend._call("in-flight", blocked_command), daemon=True
    )
    caller.start()
    assert worker_entered.wait(timeout=2)
    monkeypatch.setattr(browser_backend_module, "_THREAD_CLOSE_TIMEOUT", 0.05)

    with pytest.raises(ComputerControlError) as raised:
        backend.close()

    assert raised.value.code == "computer_stop_unconfirmed"
    assert backend._worker is not None and backend._worker.is_alive()
    resolved_profile = str(backend._profile_dir.resolve())
    with browser_backend_module._PROFILE_REGISTRY_GUARD:
        assert resolved_profile in browser_backend_module._PROFILE_HOLDERS

    allow_worker_to_exit.set()
    caller.join(timeout=2)
    backend.close()
    assert backend._worker is not None and not backend._worker.is_alive()


def test_close_concurrent_with_submit_never_leaves_caller_hanging(tmp_path):
    runtime = FakePlaywrightRuntime()
    backend = BrowserComputerBackend(
        "edge", tmp_path / "edge", playwright_factory=lambda: runtime
    )
    backend.start()
    barrier = threading.Barrier(5)
    outcomes = []

    def submit():
        barrier.wait(timeout=5)
        try:
            backend._call("race", lambda: None)
            outcomes.append("ok")
        except ComputerControlError as error:
            outcomes.append(error.code)

    def close():
        barrier.wait(timeout=5)
        backend.close()

    threads = [threading.Thread(target=submit, daemon=True) for _ in range(4)]
    threads.append(threading.Thread(target=close, daemon=True))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "close 并发 submit 后调用线程悬挂"
    assert len(outcomes) == 4
    assert all(
        outcome in ("ok", "computer_browser_closed", "computer_action_timeout")
        for outcome in outcomes
    )


@pytest.mark.parametrize("kind", ["click", "double_click", "drag"])
def test_browser_mouse_action_preserves_ctrl(backend, kind):
    page = backend.active_page
    payload = {"kind": kind, "target": "browser", "keys": ["CTRL"]}
    payload.update({"path": [[10, 10], [20, 20]]} if kind == "drag" else {"x": 10, "y": 10})
    backend.execute("run-a", parse_computer_action(payload))
    assert page.keyboard.calls == [("down", "Control"), ("up", "Control")]
    mouse_names = [call[0] for call in page.mouse.calls]
    assert mouse_names == (["move", "down", "move", "up"] if kind == "drag" else
                           ["dblclick"] if kind == "double_click" else ["click"])


def test_ctrl_shift_click_presses_in_order_and_releases_reversed(backend):
    page = backend.active_page
    action = parse_computer_action(
        {"kind": "click", "target": "browser", "x": 10, "y": 10, "keys": ["CTRL", "SHIFT"]}
    )
    backend.execute("run-a", action)
    assert page.keyboard.calls == [
        ("down", "Control"),
        ("down", "Shift"),
        ("up", "Shift"),
        ("up", "Control"),
    ]


def test_second_modifier_down_failure_releases_first_and_propagates(backend):
    page = backend.active_page
    page.keyboard.down_fail_after = 1
    action = parse_computer_action(
        {"kind": "click", "target": "browser", "x": 10, "y": 10, "keys": ["CTRL", "SHIFT"]}
    )
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", action)
    assert raised.value.code == "computer_action_failed"
    assert page.keyboard.calls == [("down", "Control"), ("up", "Control")]
    assert page.mouse.calls == []
    assert backend._pressed_keys == []


def test_click_failure_still_releases_modifiers(backend):
    page = backend.active_page
    original_click = page.mouse.click

    def failing_click(*args, **kwargs):
        original_click(*args, **kwargs)
        raise RuntimeError("injected click failure")

    page.mouse.click = failing_click
    action = parse_computer_action(
        {"kind": "click", "target": "browser", "x": 10, "y": 10, "keys": ["CTRL"]}
    )
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", action)
    assert raised.value.code == "computer_action_failed"
    assert page.keyboard.calls == [("down", "Control"), ("up", "Control")]
    assert backend._pressed_keys == []


def test_modifier_up_failure_keeps_record_for_release_inputs(backend):
    page = backend.active_page
    page.keyboard.up_error = RuntimeError("injected up failure")
    action = parse_computer_action(
        {"kind": "click", "target": "browser", "x": 10, "y": 10, "keys": ["CTRL"]}
    )
    with pytest.raises(ComputerControlError):
        backend.execute("run-a", action)
    assert backend._pressed_keys == [(page, "Control")]
    page.keyboard.up_error = None
    backend.release_inputs()
    assert page.keyboard.calls.count(("up", "Control")) == 2
    assert backend._pressed_keys == []


def test_drag_failure_releases_modifiers_and_mouse_button(backend):
    page = backend.active_page
    page.mouse.move_fail_after = 2
    action = parse_computer_action(
        {
            "kind": "drag",
            "target": "browser",
            "path": [[10, 10], [20, 20], [30, 30]],
            "keys": ["SHIFT"],
        }
    )
    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", action)
    assert raised.value.code == "computer_action_failed"
    assert ("down", "Shift") in page.keyboard.calls
    assert page.keyboard.calls[-1] == ("up", "Shift")
    assert ("up", "left") in page.mouse.calls
    assert backend._pressed_keys == []
    assert backend._pressed_buttons == set()


def test_modifiers_released_on_original_page_after_popup(backend, runtime):
    page = backend.active_page
    original_click = page.mouse.click
    context = runtime.last_context

    def popup_click(*args, **kwargs):
        popup = context.simulate_popup(url="https://popup.example/", title="Popup")
        original_click(*args, **kwargs)
        return popup

    page.mouse.click = popup_click
    action = parse_computer_action(
        {"kind": "click", "target": "browser", "x": 10, "y": 10, "keys": ["CTRL"]}
    )
    backend.execute("run-a", action)
    popup = context.pages[-1]
    assert page.keyboard.calls == [("down", "Control"), ("up", "Control")]
    assert popup.keyboard.calls == []


def test_is_reusable_rejects_pending_commands_and_sealed_instance(tmp_path):
    runtime = FakePlaywrightRuntime()
    backend = BrowserComputerBackend(
        "edge", tmp_path / "edge", playwright_factory=lambda: runtime, command_timeout=2.0
    )
    backend.start()
    assert backend.is_reusable()
    picked = threading.Event()
    gate = threading.Event()

    def blocked():
        picked.set()
        gate.wait(timeout=5)

    caller = threading.Thread(
        target=lambda: backend._call("blocked", blocked), daemon=True
    )
    try:
        caller.start()
        assert picked.wait(timeout=5)
        queued = threading.Thread(
            target=lambda: backend._call("queued", lambda: None), daemon=True
        )
        queued.start()
        deadline = time.monotonic() + 5
        while backend._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not backend._queue.empty()
        assert not backend.is_reusable(), "有 pending 命令的实例不得进入 retained"
        gate.set()
        caller.join(timeout=5)
        queued.join(timeout=5)
        assert backend.is_reusable()
        backend._failed = True
        assert not backend.is_reusable(), "running 超时封闭的实例不得复用"
        backend._failed = False
        backend._context_closed = True
        assert not backend.is_reusable(), "context 被用户关闭后不得复用"
    finally:
        gate.set()
        backend.close()
    assert not backend.is_reusable()


def test_same_title_pages_have_distinct_surface_identity(tmp_path):
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(8))
    first = FakePage(runtime, url="https://same.example/a", title="Same Title")
    second = FakePage(runtime, url="https://same.example/b", title="Same Title")
    second.visibility_state = "hidden"
    second.focused = False
    runtime.initial_pages = [first, second]
    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    backend.start()
    try:
        obs_first = backend.observe("run-a")
        assert obs_first.surface_id
        first.visibility_state = "hidden"
        first.focused = False
        second.visibility_state = "visible"
        second.focused = True
        with pytest.raises(ComputerControlError) as raised:
            backend.execute(
                "run-a",
                parse_computer_action({"kind": "click", "target": "browser", "x": 5, "y": 5}),
            )
        assert raised.value.code == "computer_surface_changed"
        assert second.mouse.calls == [] and first.mouse.calls == []
        obs_second = backend.observe("run-a")
        assert obs_second.surface_id != obs_first.surface_id
        assert obs_second.surface_generation > obs_first.surface_generation
        backend.execute(
            "run-a",
            parse_computer_action({"kind": "click", "target": "browser", "x": 5, "y": 5}),
        )
        assert second.mouse.calls == [("click", 5, 5, "left")]
        assert first.mouse.calls == []
    finally:
        backend.close()


def test_single_visible_page_without_focus_still_resolvable(backend):
    page = backend.active_page
    page.focused = False
    observation = backend.observe("run-a")
    assert observation.surface_id
    backend.execute(
        "run-a", parse_computer_action({"kind": "click", "target": "browser", "x": 1, "y": 1})
    )
    assert ("click", 1, 1, "left") in page.mouse.calls


def test_multiple_visible_focused_pages_are_ambiguous(tmp_path):
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(9))
    first = FakePage(runtime, url="https://a.example/", title="A")
    second = FakePage(runtime, url="https://b.example/", title="B")
    runtime.initial_pages = [first, second]
    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    backend.start()
    try:
        with pytest.raises(ComputerControlError) as raised:
            backend.observe("run-a")
        assert raised.value.code == "computer_surface_ambiguous"
        second.focused = False
        observation = backend.observe("run-a")
        assert observation.page_title == "A"
    finally:
        backend.close()


def test_surface_change_between_screenshot_and_commit_discards_observation(backend):
    page = backend.active_page
    other = backend._context.new_page() if hasattr(backend, "_context") else None
    assert other is not None
    other.visibility_state = "hidden"
    other.focused = False
    original_screenshot = page.screenshot

    def switch_during_screenshot(**kwargs):
        result = original_screenshot(**kwargs)
        page.visibility_state = "hidden"
        page.focused = False
        other.visibility_state = "visible"
        other.focused = True
        return result

    page.screenshot = switch_during_screenshot
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_surface_changed"


def test_browser_scroll_positive_delta_passes_css_pixels_down(backend):
    page = backend.active_page
    backend.execute(
        "run-a",
        parse_computer_action(
            {"kind": "scroll", "target": "browser", "x": 7, "y": 8, "delta_x": 10, "delta_y": 500}
        ),
    )
    assert ("move", 7, 8) in page.mouse.calls
    assert ("wheel", 10, 500) in page.mouse.calls


def test_browser_scroll_rejects_wheel_step_unit_and_zero(backend):
    page = backend.active_page
    for payload in (
        {"kind": "scroll", "target": "browser", "delta_y": 10, "unit": "wheel_step"},
        {"kind": "scroll", "target": "browser", "delta_x": 0, "delta_y": 0},
        {"kind": "scroll", "target": "browser", "delta_y": 4001},
    ):
        with pytest.raises(ComputerControlError) as raised:
            backend.execute("run-a", parse_computer_action(payload))
        assert raised.value.code == "computer_invalid_action"
    assert not any(call[0] == "wheel" for call in page.mouse.calls)


def test_navigation_uses_domcontentloaded_with_remaining_command_budget(backend):
    page = backend.active_page
    backend.execute(
        "run-a",
        parse_computer_action(
            {"kind": "navigate", "target": "browser", "url": "https://example.com/"}
        ),
    )
    backend.execute("run-a", ComputerAction(kind="back", target="browser"))
    backend.execute("run-a", ComputerAction(kind="forward", target="browser"))
    backend.execute("run-a", ComputerAction(kind="reload", target="browser"))
    kinds = [entry[0] for entry in page.navigation_kwargs[1:]]
    assert kinds == ["goto", "go_back", "go_forward", "reload"]
    for _name, kwargs in page.navigation_kwargs[1:]:
        assert kwargs["wait_until"] == "domcontentloaded"
        assert 0 < kwargs["timeout"] <= 30_000


def test_explicit_wait_is_interruptible_by_close(tmp_path):
    runtime = FakePlaywrightRuntime()
    backend = BrowserComputerBackend(
        "edge", tmp_path / "edge", playwright_factory=lambda: runtime
    )
    backend.start()
    outcome = []
    waiter = threading.Thread(
        target=lambda: _record_wait(outcome, backend), daemon=True
    )
    try:
        waiter.start()
        deadline = time.monotonic() + 5
        while backend._current_deadline is None and time.monotonic() < deadline:
            time.sleep(0.005)
        started = time.monotonic()
        backend.close()
        waiter.join(timeout=3)
        assert not waiter.is_alive(), "close 后显式等待仍悬挂"
        assert time.monotonic() - started < 3
        assert outcome and outcome[0] == "computer_cancelled"
    finally:
        backend.close()


def _record_wait(outcome, backend):
    try:
        backend.execute(
            "run-a", ComputerAction(kind="wait", target="browser", duration_ms=10_000)
        )
        outcome.append("completed")
    except ComputerControlError as error:
        outcome.append(error.code)


def test_explicit_wait_completes_normally(backend):
    backend.execute("run-a", ComputerAction(kind="wait", target="browser", duration_ms=100))


def _hint_backend(tmp_path, runtime, hints):
    backend = BrowserComputerBackend(
        "edge",
        tmp_path / "edge",
        playwright_factory=lambda: runtime,
        window_hint_provider=lambda pid: hints(),
    )
    backend.start()
    backend._browser_pid = 4321
    return backend


def test_window_hint_resolves_foreground_tab(tmp_path):
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(11))
    first = FakePage(runtime, url="https://a.example/", title="Alpha")
    second = FakePage(runtime, url="https://b.example/", title="Beta")
    runtime.initial_pages = [first, second]
    hints = lambda: [(77, "Beta 和另外 1 个页面 - Microsoft Edge", True)]
    backend = _hint_backend(tmp_path, runtime, hints)
    try:
        observation = backend.observe("run-a")
        assert observation.page_title == "Beta"
        assert backend.active_page is second
    finally:
        backend.close()


def test_window_hint_single_visible_window_when_foreground_elsewhere(tmp_path):
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(12))
    first = FakePage(runtime, url="https://a.example/", title="Alpha")
    second = FakePage(runtime, url="https://b.example/", title="Beta")
    runtime.initial_pages = [first, second]
    hints = lambda: [(77, "Alpha 和另外 1 个页面 - Microsoft Edge", False)]
    backend = _hint_backend(tmp_path, runtime, hints)
    try:
        observation = backend.observe("run-a")
        assert observation.page_title == "Alpha"
    finally:
        backend.close()


def test_window_hint_same_title_pages_are_ambiguous(tmp_path):
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(13))
    first = FakePage(runtime, url="https://a.example/", title="Same")
    second = FakePage(runtime, url="https://b.example/", title="Same")
    runtime.initial_pages = [first, second]
    hints = lambda: [(77, "Same 和另外 1 个页面 - Microsoft Edge", True)]
    backend = _hint_backend(tmp_path, runtime, hints)
    try:
        with pytest.raises(ComputerControlError) as raised:
            backend.observe("run-a")
        assert raised.value.code == "computer_surface_ambiguous"
    finally:
        backend.close()


def test_window_hint_minimized_browser_falls_back_to_current_active(tmp_path):
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(14))
    first = FakePage(runtime, url="https://a.example/", title="Alpha")
    second = FakePage(runtime, url="https://b.example/", title="Beta")
    runtime.initial_pages = [first, second]
    hints = lambda: []
    backend = _hint_backend(tmp_path, runtime, hints)
    try:
        assert backend.active_page is second
        observation = backend.observe("run-a")
        assert observation.page_title == "Beta"
    finally:
        backend.close()


def test_window_hint_provider_failure_is_explicit_ambiguity(tmp_path):
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(15))
    first = FakePage(runtime, url="https://a.example/", title="Alpha")
    second = FakePage(runtime, url="https://b.example/", title="Beta")
    runtime.initial_pages = [first, second]

    def broken(pid):
        raise RuntimeError("injected hint failure")

    backend = BrowserComputerBackend(
        "edge",
        tmp_path / "edge",
        playwright_factory=lambda: runtime,
        window_hint_provider=broken,
    )
    backend.start()
    backend._browser_pid = 4321
    try:
        with pytest.raises(ComputerControlError) as raised:
            backend.observe("run-a")
        assert raised.value.code == "computer_surface_ambiguous"
    finally:
        backend.close()


def test_remote_call_checks_admission_atomically_inside_transport_send(tmp_path):
    backend = BrowserComputerBackend(
        "edge", tmp_path / "edge", use_process_worker=True
    )
    sent_ops = []

    class FakeTransport:
        def is_alive(self):
            return True

        def pending_count(self):
            return 0

        def request(self, op, payload, timeout, before_send=None):
            if before_send is not None:
                before_send()
            sent_ops.append((op, payload))
            return {"ok": True}

    backend._started = True
    backend._transport = FakeTransport()

    # 请求停止后，_remote_call 应在向 transport 写请求前被原子拦截
    backend.request_stop(error_code="computer_cancelled", message="停止")
    with pytest.raises(ComputerControlError) as raised:
        backend._remote_call(
            "execute",
            {"run_id": "run-1", "action": {"kind": "click"}},
            "execute:click",
        )
    assert raised.value.code == "computer_cancelled"
    assert sent_ops == []
