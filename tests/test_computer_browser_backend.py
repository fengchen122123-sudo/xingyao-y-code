"""Edge/Chrome 专用 Playwright backend（core/computer/browser_backend.py）测试（计划 Task 6 / 设计 12、21、22）。

所有测试只使用 tests.computer_fakes.FakePlaywrightRuntime，绝不启动真实浏览器；
scripts/smoke_computer_control.py 的 --probe-browser / --browser-observe 人工探针
不在 pytest 中执行。
"""

import hashlib
import threading
from pathlib import Path

import pytest

from core.computer.actions import observation_provider_projection
from core.computer.browser_backend import BrowserComputerBackend
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


# ---------------- brief 样例（逐字） ----------------


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


# ---------------- 构造与启动边界 ----------------


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


# ---------------- worker 线程归属与串行 ----------------


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


# ---------------- profile 锁 ----------------


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


# ---------------- 页面生命周期 ----------------


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
        assert backend.active_page is not None
        popup = runtime.last_context.simulate_popup(url="https://popup.example/", title="Popup")
        assert backend.active_page is popup
        assert changes == ["changed"]
        observation = backend.observe("run-p")
        assert observation.page_title == "Popup"
        assert observation.page_origin == "https://popup.example"
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
    backend.close()  # 未启动时 no-op
    backend.start()
    backend.close()
    backend.close()
    assert runtime.stopped is True
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_browser_closed"


# ---------------- 观察 ----------------


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
    assert backend.active_page.screenshot_calls == [{"type": "png"}]


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


def test_screenshot_failure_raises_and_never_reuses_old_png(backend):
    page = backend.active_page
    page.screenshot_error = RuntimeError("gpu gone")
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_action_failed"
    page.screenshot_error = None
    observation = backend.observe("run-a")
    assert observation.screen_sha256 == hashlib.sha256(fake_png_bytes(6)).hexdigest()


# ---------------- 动作映射与坐标边界 ----------------


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
