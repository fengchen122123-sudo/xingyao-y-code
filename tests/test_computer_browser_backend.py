"""Edge/Chrome 专用 Playwright backend（core/computer/browser_backend.py）测试（计划 Task 6 / 设计 12、21、22）。

所有测试只使用 tests.computer_fakes.FakePlaywrightRuntime，绝不启动真实浏览器；
scripts/smoke_computer_control.py 的 --probe-browser / --browser-observe 人工探针
不在 pytest 中执行。
"""

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
    """R5 新契约：popup 只登记不直接成为 active；观察到其可见焦点后才切换受控页。"""
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
        # 登记不等于切换：active 在解析（观察）前保持原页。
        assert backend.active_page is original
        observation = backend.observe("run-p")
        assert observation.page_title == "Popup"
        assert observation.page_origin == "https://popup.example"
        assert backend.active_page is popup  # popup 可见且聚焦 → 解析后成为受控页
        first_surface = observation.surface_id
        # 用户切回原页：旧坐标失效，输入前复核拒绝。
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
        assert original.mouse.calls == []  # 零输入
        # 重新观察后基线刷新，surface identity 与 generation 更新。
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
    # B 方案：截图显式带短超时（默认 30s 在窗口被遮挡时会把命令预算耗尽）。
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
    """B 方案：截图失败不再让观察报错，降级为纯 DOM 观察且绝不复用旧截图。

    为什么改：窗口被遮挡/最小化时 WebView2 渲染被节流，截图卡住或失败属于
    预期环境状态；旧行为（抛 computer_action_failed）会让整条观察链失败。
    新契约：观察成功返回、png_path/screen_sha256 为空、screenshot_error 给出
    简短中文原因（带异常类名、不带堆栈与页面 URL）。
    """
    page = backend.active_page
    page.page_text = "示例页面正文"
    page.screenshot_error = RuntimeError("gpu gone")
    observation = backend.observe("run-a")
    assert observation.ok is True
    assert observation.png_path == ""
    assert observation.screen_sha256 == ""
    assert observation.page_text == "示例页面正文"
    assert "RuntimeError" in observation.screenshot_error
    assert "gpu gone" not in observation.screenshot_error  # 只留异常类名，不带原始文案/堆栈
    assert "http" not in observation.screenshot_error  # 绝不带页面 URL
    # 恢复截图后：观察回到原语义（新截图、新 sha、无降级说明）。
    page.screenshot_error = None
    observation = backend.observe("run-a")
    assert observation.screenshot_error == ""
    assert observation.screen_sha256 == hashlib.sha256(fake_png_bytes(6)).hexdigest()
    assert Path(observation.png_path).read_bytes() == fake_png_bytes(6)


def test_page_text_failure_does_not_break_observe(backend):
    """正文提取失败只丢正文：观察仍然成功（缺正文比观察失败轻）。"""
    page = backend.active_page
    page.page_text_error = RuntimeError("evaluate timeout")
    observation = backend.observe("run-a")
    assert observation.ok is True
    assert observation.page_text == ""
    assert observation.screenshot_error == ""
    assert Path(observation.png_path).read_bytes() == fake_png_bytes(6)


def test_surface_change_during_degraded_observation_is_still_discarded(backend):
    """降级不放松页面身份复核：截图失败时切换页面，本次观察仍必须作废。

    为什么重要：降级路径让"没有图"变成合法结果，但绝不能顺带让"旧页文本
    冒充新页观察"变合法——computer_surface_changed 的裁决与是否有图无关。
    """
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


# ---------------------------------------------------------------------------
# Task 16（Task 6 M1 复核）：启动超时后的 worker 泄漏
# ---------------------------------------------------------------------------


def test_start_timeout_posts_sentinel_so_late_boot_worker_exits(tmp_path):
    """启动超时后 worker 仍完成慢启动：关闭哨兵让 worker 立即收尾。

    修复前：超时只置 _closed 就抛错，慢启动最终成功的 worker 会带着真实
    浏览器 context 进入命令循环（queue.get 永久阻塞）——线程、浏览器进程、
    profile 注册表锁与临时截图目录全部泄漏（close() 因 not started 而 no-op）。
    """
    release = threading.Event()

    class SlowRuntime(FakePlaywrightRuntime):
        def start(self):
            release.wait(timeout=10)  # 模拟慢浏览器启动：超过 start_timeout
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
    assert runtime.stopped  # Playwright runtime 已 stop
    assert runtime.last_context.closed  # context 已 close（浏览器进程退出）


# ---------------------------------------------------------------------------
# R2：命令生命周期（排队超时零执行、执行中超时封闭实例、close 撤销队列）
# ---------------------------------------------------------------------------


def test_timed_out_queued_command_never_executes_later(tmp_path):
    """审计复现迁移（tests/audits 同名用例，按 R2 契约重写）。

    已返回超时的排队输入绝不执行；drain 由测试夹具直接等待 worker 的已知
    完成事件 + queue.join 确定屏障，不再向同一实例投递额外命令。
    """
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
        backend._queue.join()  # 确定屏障：worker 已处理（跳过）全部排队命令
        assert not side_effect.is_set(), "已经返回超时的排队输入仍被 worker 执行"
        # 排队超时不封闭实例：后续命令正常工作。
        backend._call("still usable", lambda: None)
    finally:
        gate.set()
        backend.close()


def test_running_command_timeout_seals_instance_and_holds_profile(tmp_path):
    """running 超时：结果未知，实例永久封闭；worker 未退出前 profile 不释放。"""
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
        # 封闭后后续输入被拒绝。
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
        # worker 真实退出后 profile 释放，新实例可显式启动。
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
    """提交锁等待计入同一命令预算：锁被占用时按超时失败，不无限等待。"""
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
    """_Command 领取/撤销竞态的状态机裁决：cancelled 后 try_start 绝不执行。"""
    from core.computer.browser_backend import _Command

    expired = _Command(1, lambda: None, deadline=time.monotonic() - 1)
    assert not expired.try_start(time.monotonic())
    assert expired.state == "cancelled"
    assert expired.done.is_set()

    command = _Command(2, lambda: None, deadline=time.monotonic() + 60)
    assert command.cancel_if_queued("computer_browser_closed", "已关闭")
    assert not command.try_start(time.monotonic())  # 已撤销不得再领取

    running = _Command(3, lambda: None, deadline=time.monotonic() + 60)
    assert running.try_start(time.monotonic())
    assert not running.cancel_if_queued("computer_browser_closed", "已关闭")
    running.finish()
    assert running.state == "succeeded"


def test_close_cancels_all_queued_commands_without_executing(tmp_path):
    """队列满时 close：排队命令全部得到撤销终态（零副作用），调用线程不悬挂。"""
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

    # close 在独立线程执行：worker 仍被 gate 卡住时 join 会阻塞，
    # 但撤销排队命令发生在 join 之前，8 个调用线程必须先全部终止。
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


def test_close_concurrent_with_submit_never_leaves_caller_hanging(tmp_path):
    """close 与 submit 并发：每个调用线程以结果或领域错误终止，绝不悬挂。"""
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


# ---------------------------------------------------------------------------
# R3：修饰键生命周期（Ctrl/Shift 按下、逆序释放、绑定原 page）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["click", "double_click", "drag"])
def test_browser_mouse_action_preserves_ctrl(backend, kind):
    """审计复现迁移（tests/audits 同名用例）：修饰键经 Playwright down/up 包裹动作。"""
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
    """第 2 个 down 失败：已按下的第 1 个键逆序释放，鼠标动作零执行，错误传播。"""
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
    """鼠标动作失败：修饰键仍逆序释放，执行错误向上传播。"""
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
    """up 失败：按下记录保留，release_inputs 回到原 page 重试释放。"""
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
    """拖拽路径中途失败：mouse.up 与修饰键都释放，错误传播。"""
    page = backend.active_page
    page.mouse.move_fail_after = 2  # 起点移动 + 按下后第一步路径移动即失败
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
    """动作触发 popup：修饰键释放绑定按下时的原 page，绝不串到 popup 页。"""
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
    """retained 领取裁决：队列有 pending 命令、实例封闭、context 已关均不可复用。"""
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


# ---------------------------------------------------------------------------
# R5：受控页身份与旧坐标失效
# ---------------------------------------------------------------------------


def test_same_title_pages_have_distinct_surface_identity(tmp_path):
    """同标题同域两页互切：surface_id 不同，切页后旧坐标零输入。"""
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
        # 用户切到第二个同标题页。
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
        # 重新观察后允许输入，落在第二页。
        backend.execute(
            "run-a",
            parse_computer_action({"kind": "click", "target": "browser", "x": 5, "y": 5}),
        )
        assert second.mouse.calls == [("click", 5, 5, "left")]
        assert first.mouse.calls == []
    finally:
        backend.close()


def test_single_visible_page_without_focus_still_resolvable(backend):
    """浏览器整体失焦/地址栏聚焦：唯一可见页仍可确定，不误报歧义。"""
    page = backend.active_page
    page.focused = False
    observation = backend.observe("run-a")
    assert observation.surface_id
    backend.execute(
        "run-a", parse_computer_action({"kind": "click", "target": "browser", "x": 1, "y": 1})
    )
    assert ("click", 1, 1, "left") in page.mouse.calls


def test_multiple_visible_focused_pages_are_ambiguous(tmp_path):
    """多窗口均可见且焦点无法唯一确定：明确 ambiguous，不任意选页继续。"""
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(9))
    first = FakePage(runtime, url="https://a.example/", title="A")
    second = FakePage(runtime, url="https://b.example/", title="B")
    runtime.initial_pages = [first, second]  # 两页默认均 visible+focused（双窗口）
    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    backend.start()
    try:
        with pytest.raises(ComputerControlError) as raised:
            backend.observe("run-a")
        assert raised.value.code == "computer_surface_ambiguous"
        # 用户聚焦其一后恢复可确定。
        second.focused = False
        observation = backend.observe("run-a")
        assert observation.page_title == "A"
    finally:
        backend.close()


def test_surface_change_between_screenshot_and_commit_discards_observation(backend):
    """截图后受控页已切换：观察作废，绝不返回旧页截图充当新观察。"""
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
    """R7：浏览器滚动为 CSS 像素语义，+500 原样向下传递给 mouse.wheel。"""
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


# ---------------------------------------------------------------------------
# R8：导航就绪基线与可取消等待
# ---------------------------------------------------------------------------


def test_navigation_uses_domcontentloaded_with_remaining_command_budget(backend):
    """R8：导航以 domcontentloaded 为文档基线，timeout 取命令剩余预算。"""
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
    # 首条是启动时 about:blank 的 goto（无等待策略），其后才是动作导航。
    kinds = [entry[0] for entry in page.navigation_kwargs[1:]]
    assert kinds == ["goto", "go_back", "go_forward", "reload"]
    for _name, kwargs in page.navigation_kwargs[1:]:
        assert kwargs["wait_until"] == "domcontentloaded"
        # 剩余预算为正且不超过命令总预算（30s 默认）。
        assert 0 < kwargs["timeout"] <= 30_000


def test_explicit_wait_is_interruptible_by_close(tmp_path):
    """R8：显式 wait 服务停止事件；close 立即中断，不睡满整个时长。"""
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
        # 等 worker 进入 wait 命令（queue 已取空即已进入执行）。
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


# ---------------------------------------------------------------------------
# R5：系统侧窗口标题提示裁决（DOM 可见性信号不可区分时）
# ---------------------------------------------------------------------------


def _hint_backend(tmp_path, runtime, hints):
    """两页均 visible+focused（模拟 DOM 信号不可区分），窗口提示由 fake 提供。"""
    backend = BrowserComputerBackend(
        "edge",
        tmp_path / "edge",
        playwright_factory=lambda: runtime,
        window_hint_provider=lambda pid: hints(),
    )
    backend.start()
    backend._browser_pid = 4321  # 测试 seam：fake runtime 没有真实浏览器进程
    return backend


def test_window_hint_resolves_foreground_tab(tmp_path):
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(11))
    first = FakePage(runtime, url="https://a.example/", title="Alpha")
    second = FakePage(runtime, url="https://b.example/", title="Beta")
    runtime.initial_pages = [first, second]  # 两页默认 visible+focused
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
    # 前台是其他应用（无 fg 标记），本浏览器只有一个可见窗口。
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
    hints = lambda: []  # 浏览器最小化：无可见窗口
    backend = _hint_backend(tmp_path, runtime, hints)
    try:
        assert backend.active_page is second  # 启动约定：最后可见页
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
