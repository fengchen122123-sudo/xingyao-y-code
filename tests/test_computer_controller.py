"""Task 8：单租约 Controller、状态机、暂停恢复与停止顺序测试。

brief 的 6 个样例逐字保留；另覆盖：非法状态转换（含跳过 completing）、
owner/cancel/generation 检查、动作串行、错误不发成功、三次无变化停止、
observe/wait 不计 no-progress、停止事件只发一次、Turn 终态 finally 二选一
不留 running 租约、provider_safety_required 不可恢复、清理错误记为 failed。

所有 backend 均为 FakeComputerBackend，绝不启动真实浏览器/键鼠。
"""

import json
import threading
import time

import pytest

from core.computer.controller import ComputerControlService, get_computer_control_service
from core.computer.models import (
    ComputerAction,
    ComputerControlError,
    ComputerOwner,
)
from core.computer.settings import ComputerSettingsSnapshot
from tests.computer_fakes import (
    DISABLED_CAPABILITY,
    GENERIC_CAPABILITY,
    NATIVE_BROWSER_CAPABILITY,
    FakeComputerBackend,
)


class _ServiceFixture:
    """ComputerControlService 测试夹具：注入 fake backend factory / 设置源 / abort 记录。

    ``backend`` 返回最近创建的 browser fake（无 browser 时返回任意已创建 fake），
    供 brief 样例断言 release/close 计数。
    """

    def __init__(self, service, created, settings_state, abort_calls, loader_calls):
        self._service = service
        self.created = created
        self.settings_state = settings_state
        self.abort_calls = abort_calls
        self.loader_calls = loader_calls

    def __getattr__(self, name):
        return getattr(self._service, name)

    @property
    def backend(self):
        browsers = self.created.get("browser")
        if browsers:
            return browsers[-1]
        for backends in self.created.values():
            if backends:
                return backends[-1]
        return None


@pytest.fixture
def settings_state():
    """可变设置状态：generation 与开关由测试直接改写，模拟开启/关闭/损坏。"""

    return {
        "browser": True,
        "desktop": True,
        "platform": True,
        "generation": 1,
        "fail": False,
    }


@pytest.fixture
def service(settings_state):
    created = {}
    abort_calls = []
    loader_calls = []

    def factory(target):
        backend = FakeComputerBackend(target=target)
        created.setdefault(target, []).append(backend)
        return backend

    def loader():
        loader_calls.append(1)
        if settings_state["fail"]:
            raise ComputerControlError("computer_invalid_config", "配置损坏")
        return ComputerSettingsSnapshot(
            browser_control_enabled=settings_state["browser"],
            computer_control_enabled=settings_state["desktop"],
            computer_browser="edge",
            browser_options=(),
            profile_display_path="",
            platform_supported=settings_state["platform"],
            generation=settings_state["generation"],
        )

    instance = ComputerControlService(
        backend_factory=factory,
        settings_loader=loader,
        generation_provider=lambda: settings_state["generation"],
        abort_streams=abort_calls.append,
    )
    return _ServiceFixture(instance, created, settings_state, abort_calls, loader_calls)


@pytest.fixture
def capability():
    return GENERIC_CAPABILITY


@pytest.fixture
def owner_a():
    return ComputerOwner(
        session_id="session-a",
        turn_id="turn-a",
        session_label="会话A",
        cancel_event=threading.Event(),
    )


@pytest.fixture
def owner_b():
    return ComputerOwner(
        session_id="session-b",
        turn_id="turn-b",
        session_label="会话B",
        cancel_event=threading.Event(),
    )


@pytest.fixture
def running_run(service, owner_a, capability):
    """generic run 已通过第一次 observe 建立 browser 租约，状态 running。"""

    run = service.start_run(owner_a, capability, target=None)
    service.execute(run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    return run


# ---------------- brief Step 1 样例（逐字） ----------------


def test_second_owner_cannot_take_active_lease(service, owner_a, owner_b, capability):
    run = service.start_run(owner_a, capability, target="desktop")
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_b, capability, target="desktop")
    assert raised.value.code == "computer_session_busy"
    assert raised.value.details == {"owner_session_label": owner_a.session_label}
    assert service.snapshot().run_id == run.run_id


def test_generic_two_target_run_leases_on_first_observe(service, owner_a, capability):
    run = service.start_run(owner_a, capability, target=None)
    service.execute(run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    with pytest.raises(ComputerControlError):
        service.execute(run.run_id, owner_a, ComputerAction(kind="click", target="desktop", x=1, y=1))


def test_resume_requires_fresh_observation_before_action(service, running_run, owner_a):
    service.pause(running_run.run_id, "用户暂停")
    service.resume(running_run.run_id)
    with pytest.raises(ComputerControlError, match="先观察"):
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="desktop", x=1, y=1))


def test_request_user_takeover_pauses_for_user_then_requires_observe(service, running_run, owner_a):
    service.execute(
        running_run.run_id, owner_a,
        ComputerAction(kind="request_user_takeover", target="browser", reason_code="login_required"),
    )
    assert service.snapshot().status == "waiting_for_user"
    service.resume(running_run.run_id)
    with pytest.raises(ComputerControlError, match="先观察"):
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))


def test_complete_run_releases_lease_but_retains_browser_session(service, running_run, owner_a, owner_b, capability):
    first = service.complete_run(running_run.run_id, owner_a, retain_browser_session=True)
    second = service.complete_run(running_run.run_id, owner_a, retain_browser_session=True)
    assert first.status == second.status == "completed"
    assert service.backend.release_count == 0
    assert service.backend.close_count == 0  # stop 才关闭 backend
    new_run = service.start_run(owner_b, capability, target="browser")  # 租约已释放，可开新 run
    assert new_run.run_id != running_run.run_id


def test_stop_is_idempotent_and_releases_inputs_once(service, running_run):
    first = service.stop(running_run.run_id, "hotkey")
    second = service.stop(running_run.run_id, "hotkey")
    assert first.status == second.status == "stopped"
    assert service.backend.release_count == 1
    assert service.backend.close_count == 1


# ---------------- 状态机与租约 ----------------


def test_transition_table_rejects_skipping_completing(service, running_run):
    run = service._run
    with pytest.raises(ComputerControlError) as raised:
        service._transition(run, "completed")  # running 直接跳 completed
    assert raised.value.code == "computer_invalid_state"
    service._transition(run, "completing")
    service._transition(run, "completed")
    assert run.status == "completed"


def test_complete_run_from_paused_is_rejected(service, running_run, owner_a):
    service.pause(running_run.run_id, "用户暂停")
    with pytest.raises(ComputerControlError) as raised:
        service.complete_run(running_run.run_id, owner_a)
    assert raised.value.code == "computer_invalid_state"
    assert service.snapshot().status == "paused"


def test_start_run_rejects_disabled_capability(service, owner_a):
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_a, DISABLED_CAPABILITY, target="browser")
    assert raised.value.code == "computer_disabled"


def test_start_run_rejects_target_outside_capability(service, owner_a):
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_a, NATIVE_BROWSER_CAPABILITY, target="desktop")
    assert raised.value.code == "computer_disabled"


def test_start_run_rejects_cancelled_owner(service, owner_a, capability):
    owner_a.cancel_event.set()
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_a, capability, target="desktop")
    assert raised.value.code == "computer_cancelled"
    assert service.snapshot().status == "idle"


# ---------------- owner / cancel / generation 检查 ----------------


def test_execute_rejects_wrong_owner(service, running_run, owner_b):
    with pytest.raises(ComputerControlError) as raised:
        service.execute(running_run.run_id, owner_b, ComputerAction(kind="observe", target="browser"))
    assert raised.value.code == "computer_owner_mismatch"


def test_execute_rejects_cancelled_owner(service, running_run, owner_a):
    owner_a.cancel_event.set()
    with pytest.raises(ComputerControlError) as raised:
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    assert raised.value.code == "computer_cancelled"


def test_execute_rejects_unknown_run(service, owner_a):
    with pytest.raises(ComputerControlError):
        service.execute("no-such-run", owner_a, ComputerAction(kind="observe", target="browser"))


def test_resume_rejected_after_owner_turn_ended(service, running_run, owner_a):
    service.pause(running_run.run_id, "用户暂停")
    owner_a.cancel_event.set()
    with pytest.raises(ComputerControlError) as raised:
        service.resume(running_run.run_id)
    assert raised.value.code == "computer_cancelled"
    assert service.snapshot().status == "paused"


def test_generation_bump_with_target_still_allowed_continues(service, running_run, owner_a, settings_state):
    # 另一个开关变化导致 generation 递增，但 browser 仍开启：重读设置后继续运行
    settings_state["desktop"] = False
    settings_state["generation"] += 1
    observation = service.execute(
        running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser")
    )
    assert observation.ok
    assert service.loader_calls  # 确实重读了有效设置
    assert service.snapshot().status == "running"


def test_generation_bump_with_target_disabled_stops_run(service, running_run, owner_a, settings_state):
    settings_state["browser"] = False
    settings_state["generation"] += 1
    with pytest.raises(ComputerControlError) as raised:
        service.execute(
            running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser")
        )
    assert raised.value.code == "computer_disabled"
    assert service.snapshot().status == "stopped"
    assert service.backend.close_count == 1


def test_settings_load_failure_treated_as_disabled(service, running_run, owner_a, settings_state):
    settings_state["generation"] += 1
    settings_state["fail"] = True
    with pytest.raises(ComputerControlError) as raised:
        service.execute(
            running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser")
        )
    assert raised.value.code == "computer_disabled"
    assert service.snapshot().status == "stopped"


# ---------------- 动作顺序、串行与无进展 ----------------


def test_generic_run_first_action_must_be_observe(service, owner_a, capability):
    run = service.start_run(owner_a, capability, target=None)
    with pytest.raises(ComputerControlError, match="先观察"):
        service.execute(run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))


def test_actions_are_serialized_through_single_action_lock(service, running_run, owner_a):
    backend = service.backend
    gate = threading.Event()
    backend.execute_gate = gate
    results = []
    errors = []

    def click():
        try:
            results.append(
                service.execute(
                    running_run.run_id, owner_a,
                    ComputerAction(kind="click", target="browser", x=1, y=1),
                )
            )
        except Exception as error:  # noqa: BLE001 - 测试收集所有异常统一断言
            errors.append(error)

    first = threading.Thread(target=click)
    first.start()
    deadline = time.time() + 5
    while not backend.execute_calls and time.time() < deadline:
        time.sleep(0.01)
    assert backend.execute_calls, "第一个动作未进入 backend"
    second = threading.Thread(target=click)
    second.start()
    time.sleep(0.2)
    # 第一动作未结束前，第二动作不得进入 backend
    assert len(backend.execute_calls) == 1
    gate.set()
    first.join(5)
    second.join(5)
    assert not errors
    assert len(results) == 2
    assert backend.concurrent_execute_violations == 0
    enters = [i for i, item in enumerate(backend.execute_log) if item[0] == "enter"]
    exits = [i for i, item in enumerate(backend.execute_log) if item[0] == "exit"]
    assert enters[1] > exits[0]


def test_backend_error_does_not_emit_success(service, running_run, owner_a):
    events = []
    service.add_listener(events.append)
    service.backend.execute_error = ComputerControlError("computer_input_rejected", "fake 注入失败")
    with pytest.raises(ComputerControlError) as raised:
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    assert raised.value.code == "computer_input_rejected"
    finished = [event for event in events if event["type"] == "computer_action_finished"]
    assert finished and finished[-1]["ok"] is False
    assert finished[-1]["error_code"] == "computer_input_rejected"
    assert not any(event["type"] == "computer_observation" for event in events)


def test_observe_failure_propagates_without_stale_screenshot(service, running_run, owner_a):
    service.backend.observe_error = ComputerControlError("computer_browser_closed", "页面已关闭")
    with pytest.raises(ComputerControlError) as raised:
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    assert raised.value.code == "computer_browser_closed"


def test_three_identical_screens_stop_with_no_progress(service, running_run, owner_a):
    backend = service.backend
    backend._screen_hashes = ["h1", "h1", "h1"]
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    with pytest.raises(ComputerControlError) as raised:
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    assert raised.value.code == "computer_no_progress"
    assert service.snapshot().status == "stopped"
    assert service.backend.close_count == 1


def test_screen_change_resets_no_progress_count(service, running_run, owner_a):
    backend = service.backend
    backend._screen_hashes = ["h1", "h1", "h2", "h2"]
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    observation = service.execute(
        running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1)
    )
    assert observation.ok
    assert service.snapshot().status == "running"


def test_observe_and_wait_do_not_count_no_progress(service, running_run, owner_a):
    backend = service.backend
    backend._screen_hashes = ["same"] * 8
    for _ in range(3):
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
        service.execute(
            running_run.run_id, owner_a,
            ComputerAction(kind="wait", target="browser", duration_ms=100),
        )
    assert service.snapshot().status == "running"
    assert service.backend.close_count == 0


# ---------------- 事件 ----------------


def test_events_have_strictly_increasing_seq_and_no_sensitive_payload(service, running_run, owner_a):
    events = []
    service.add_listener(events.append)
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    types = [event["type"] for event in events]
    assert "computer_action_started" in types
    assert "computer_action_finished" in types
    assert "computer_observation" in types
    seqs = [event["event_seq"] for event in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert events[0]["action_ordinal"] == 2  # 第一次 observe 是 ordinal 1
    for event in events:
        json.dumps(event)
        assert "png_path" not in event
        assert "text" not in event
        assert "base64" not in event


def test_listener_exception_does_not_break_execute(service, running_run, owner_a):
    def bad_listener(_event):
        raise RuntimeError("listener boom")

    service.add_listener(bad_listener)
    observation = service.execute(
        running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser")
    )
    assert observation.ok
    service.remove_listener(bad_listener)


def test_stop_final_event_emitted_once(service, running_run):
    events = []
    service.add_listener(events.append)
    service.stop(running_run.run_id, "hotkey")
    service.stop(running_run.run_id, "hotkey")
    stopped = [
        event for event in events
        if event["type"] == "computer_state" and event.get("status") == "stopped"
    ]
    assert len(stopped) == 1


# ---------------- 停止顺序与清理 ----------------


def test_stop_sets_cancel_event_and_aborts_inflight_streams(service, owner_a, capability):
    run = service.start_run(owner_a, capability, target="desktop")
    snapshot = service.stop(run.run_id, "hotkey")
    assert snapshot.status == "stopped"
    assert snapshot.stop_reason == "hotkey"
    assert owner_a.cancel_event.is_set()
    # 先 set 事件再 abort：abort 收到的必须是同一个已 set 的 cancel_event
    assert service.abort_calls == [owner_a.cancel_event]


def test_stop_cleanup_error_marks_failed_not_stopped(service, running_run):
    service.backend.close_error = RuntimeError("close boom")
    snapshot = service.stop(running_run.run_id, "hotkey")
    assert snapshot.status == "failed"
    assert snapshot.error_code
    # 后续清理步骤仍然执行
    assert service.backend.release_count == 1
    assert service.backend.close_count == 1
    # 失败也释放租约：可以开新 run
    assert service.snapshot().run_id == running_run.run_id  # 终态快照仍可见


def test_turn_end_finally_complete_or_stop_leaves_no_running_lease(
    service, owner_a, owner_b, capability
):
    run = service.start_run(owner_a, capability, target="desktop")
    snapshot = service.complete_run(run.run_id, owner_a)
    assert snapshot.status == "completed"
    # desktop backend 立即释放输入并关闭
    desktop = service.created["desktop"][-1]
    assert desktop.release_count == 1
    assert desktop.close_count == 1
    # 租约已释放：其他 owner 可开新 run；新 run 走 stop 终态
    second = service.start_run(owner_b, capability, target="desktop")
    assert second.run_id != run.run_id
    stopped = service.stop(second.run_id, "turn_end")
    assert stopped.status == "stopped"
    assert service.snapshot().status == "stopped"
    # 再无可执行动作
    with pytest.raises(ComputerControlError):
        service.execute(second.run_id, owner_b, ComputerAction(kind="observe", target="desktop"))


def test_provider_safety_required_blocks_execute_and_resume(service, running_run, owner_a):
    snapshot = service.enter_provider_safety_required(running_run.run_id)
    assert snapshot.status == "provider_safety_required"
    with pytest.raises(ComputerControlError):
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    with pytest.raises(ComputerControlError) as raised:
        service.resume(running_run.run_id)
    assert raised.value.code == "computer_provider_safety_required"
    stopped = service.stop(running_run.run_id, "safety_stop")
    assert stopped.status == "stopped"


# ---------------- 审查修复：claim 竞态孤儿 backend 必须 best-effort 关闭 ----------------


def test_start_run_closes_backend_when_stopped_during_claim(service, owner_a, capability):
    """run 在 _claim_backend 期间被停止：刚启动的 backend 不得泄漏。

    场景：start_run 发出 starting 事件后、backend 启动完成前，另一个入口
    （如全局热键）停止了 run。此时 stop() 看到的 run.backend 仍是 None，
    关不到这个 backend；start_run 必须自己 best-effort 关闭它，否则遗留
    真实浏览器进程与被锁 profile。
    """
    stopped = []

    def listener(event):
        if (
            event["type"] == "computer_state"
            and event.get("reason") == "start"
            and not stopped
        ):
            stopped.append(event["run_id"])
            service.stop(event["run_id"], "claim_race")

    service.add_listener(listener)
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_a, capability, target="desktop")
    assert raised.value.code == "computer_cancelled"
    assert service.backend.close_count == 1
    assert service.snapshot().status == "stopped"


def test_start_run_orphan_close_failure_does_not_mask_error(owner_a, capability):
    """孤儿 backend 关闭自身失败只留痕，不得掩盖 computer_cancelled。"""
    backend = FakeComputerBackend(target="desktop", close_error=RuntimeError("close boom"))
    service = ComputerControlService(
        backend_factory=lambda target: backend,
        abort_streams=lambda event: None,
    )
    stopped = []

    def listener(event):
        if (
            event["type"] == "computer_state"
            and event.get("reason") == "start"
            and not stopped
        ):
            stopped.append(event["run_id"])
            service.stop(event["run_id"], "claim_race")

    service.add_listener(listener)
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_a, capability, target="desktop")
    assert raised.value.code == "computer_cancelled"


def test_execute_establishing_closes_backend_when_run_stopped_during_claim(
    owner_a, capability
):
    """generic 首次 observe 建租约期间 run 被停止：刚启动的 backend 不得泄漏。"""
    created = []
    holder = {}

    def factory(target):
        backend = FakeComputerBackend(target=target)
        created.append(backend)
        if holder.get("armed"):
            service.stop(holder["run_id"], "claim_race")
        return backend

    service = ComputerControlService(
        backend_factory=factory,
        generation_provider=lambda: 1,
        abort_streams=lambda event: None,
    )
    run = service.start_run(owner_a, capability, target=None)
    holder["run_id"] = run.run_id
    holder["armed"] = True
    with pytest.raises(ComputerControlError) as raised:
        service.execute(
            run.run_id, owner_a, ComputerAction(kind="observe", target="browser")
        )
    assert raised.value.code == "computer_cancelled"
    assert created[0].close_count == 1


def test_execute_establishing_closes_backend_when_settings_revoked_during_claim(
    owner_a, capability
):
    """建租约期间设置被关闭（generation 失配且能力撤销）：backend 不泄漏，run 停止。"""
    created = []
    state = {"generation": 1}

    def factory(target):
        backend = FakeComputerBackend(target=target)
        created.append(backend)
        state["generation"] += 1  # claim 期间设置被改写（关闭 browser 开关）
        return backend

    def loader():
        return ComputerSettingsSnapshot(
            browser_control_enabled=False,
            computer_control_enabled=True,
            computer_browser="edge",
            browser_options=(),
            profile_display_path="",
            platform_supported=True,
            generation=state["generation"],
        )

    service = ComputerControlService(
        backend_factory=factory,
        settings_loader=loader,
        generation_provider=lambda: state["generation"],
        abort_streams=lambda event: None,
    )
    run = service.start_run(owner_a, capability, target=None)
    with pytest.raises(ComputerControlError) as raised:
        service.execute(
            run.run_id, owner_a, ComputerAction(kind="observe", target="browser")
        )
    assert raised.value.code == "computer_disabled"
    assert created[0].close_count == 1
    assert service.snapshot().status == "stopped"


# ---------------- 单例 ----------------


def test_get_computer_control_service_lazy_singleton(monkeypatch):
    import core.computer.controller as controller

    monkeypatch.setattr(controller, "_SERVICE", None)
    first = get_computer_control_service()
    assert first is get_computer_control_service()
