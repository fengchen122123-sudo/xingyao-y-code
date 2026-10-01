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
from dataclasses import replace

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

    def factory(target, identity):
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
    assert raised.value.details["owner_session_label"] == owner_a.session_label
    # L3 可诊断性：占用错误必须说明被占 run 当前处于什么状态，
    # 否则用户只看到"被占用"，无法判断该等它结束还是该手动停止。
    assert raised.value.details["owner_run_status"] == "running"
    assert "当前状态：running" in raised.value.message
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
    # R3：retain 前必须释放按下状态（修饰键/鼠标键不带入下一 run）；
    # backend 本身不 close，作为 retained session 供下一 run 显式复用。
    assert service.backend.release_count == 1
    assert service.backend.close_count == 0  # stop 才关闭 backend
    new_run = service.start_run(owner_b, capability, target="browser")  # 租约已释放，可开新 run
    assert new_run.run_id != running_run.run_id


def test_complete_run_from_waiting_for_user_releases_lease(
    service, running_run, owner_a, owner_b, capability
):
    """L1：用户接管态（waiting_for_user）的 run 也能按“完成”收尾并释放租约。

    回合以 completed 收尾时必须允许 waiting_for_user → completing；否则 run
    既不终结也不清理，租约留活，下一回合被 computer_session_busy 永久挡死。
    """
    service.execute(
        running_run.run_id, owner_a,
        ComputerAction(kind="request_user_takeover", target="browser", reason_code="login_required"),
    )
    assert service.snapshot().status == "waiting_for_user"
    snapshot = service.complete_run(running_run.run_id, owner_a, retain_browser_session=True)
    assert snapshot.status == "completed"
    # 接管态收尾与普通收尾同为 retain=True：绝不能关闭 backend —— 外部
    # Edge/Chrome 上 close() 会直接关掉用户此刻正在手动操作的浏览器窗口
    # （接管场景恰恰正在用那个窗口）；内置浏览器上 close() 虽只断 CDP
    # 连接，但保留会话还能让下一回合免去重连。
    assert service.backend.close_count == 0
    assert service.backend.release_count == 1
    # 租约已释放：另一 owner 立即可以开新 run，不抛 computer_session_busy。
    new_run_id = service.start_run(owner_b, capability, target="browser").run_id
    assert new_run_id != running_run.run_id


def test_stop_is_idempotent_and_releases_inputs_once(service, running_run):
    first = service.stop(running_run.run_id, "hotkey")
    second = service.stop(running_run.run_id, "hotkey")
    assert first.status == second.status == "stopped"
    assert service.backend.release_count == 1
    assert service.backend.close_count == 1


def test_stop_from_stopping_is_idempotent(service, running_run):
    """L2：停止在途（stopping）时再次 stop 必须幂等返回，而不是抛非法状态转换。

    现状 stopping → stopping 非法，会把正在进行的清理链打断；且幂等路径
    不得重复释放/关闭 backend，也不得终结 run。
    """
    service._transition(service._run, "stopping")
    snapshot = service.stop(running_run.run_id, "again")
    assert snapshot.status == "stopping"
    assert service.backend.release_count == 0
    assert service.backend.close_count == 0
    assert service.snapshot().run_id == running_run.run_id


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
    """连续 3 次无进展停止（R6 语义：变化后计数归零，再连续 3 次不变才停止）。"""
    backend = service.backend
    backend._screen_hashes = ["h1", "h1", "h1", "h1"]
    # 第 1 击相对观察基线有变化（计数 0），第 2/3 击不变（计数 1/2），仍继续。
    for _ in range(3):
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    assert service.snapshot().status == "running"
    # 第 4 击仍不变：计数达到 3，停止。
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
        backend_factory=lambda target, identity: backend,
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

    def factory(target, identity):
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

    def factory(target, identity):
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


# ---------------- R1：取消提交点（取消期间不得发布成功结果） ----------------


def test_cancel_during_action_must_not_publish_success(owner_a, capability):
    """backend.execute 期间 owner 取消：动作不得提交成功，恰好一条 finished(ok=False)。

    审计复现迁移（tests/audits/computer_control_cases.py 同名用例）。
    """
    backend = FakeComputerBackend(target="browser")
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 1,
        abort_streams=lambda event: None,
    )
    run = service.start_run(owner_a, capability, target="browser")
    service.execute(run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    events = []
    service.add_listener(events.append)
    backend.execute = lambda run_id, action: owner_a.cancel_event.set()
    try:
        with pytest.raises(ComputerControlError, match="取消|停止"):
            service.execute(
                run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=10, y=10)
            )
        finished = [
            event for event in events if event["type"] == "computer_action_finished"
        ]
        assert not any(event.get("ok") is True for event in events)
        # 已 action_started 的动作恰好产生一条 finished，取消时 ok=False。
        assert len(finished) == 1 and finished[0]["ok"] is False
        assert finished[0]["error_code"] == "computer_cancelled"
        # 取消后不得发布新观察事件。
        assert not any(event["type"] == "computer_observation" for event in events)
    finally:
        service.stop()


def test_cancel_during_observe_must_not_publish_success(service, running_run, owner_a):
    """backend.observe 期间取消：观察结果不得提交为成功。"""
    backend = service.backend
    original_observe = backend.observe

    def cancelling_observe(run_id):
        owner_a.cancel_event.set()
        return original_observe(run_id)

    backend.observe = cancelling_observe
    events = []
    service.add_listener(events.append)
    try:
        with pytest.raises(ComputerControlError, match="取消|停止"):
            service.execute(
                running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=10, y=10)
            )
        assert not any(event.get("ok") is True for event in events)
    finally:
        service.stop()


def test_cancel_during_preview_publish_must_not_commit_success(owner_a, capability):
    """预览发布期间取消：发布发生在提交点之前，取消后不得提交成功。"""
    backend = FakeComputerBackend(target="browser", png_path="virtual-shot.png")
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 1,
        abort_streams=lambda event: None,
    )
    run = service.start_run(owner_a, capability, target="browser")
    service.execute(run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    events = []
    service.add_listener(events.append)

    def cancelling_publisher(run_id, png_path):
        owner_a.cancel_event.set()
        return "/computer-preview/tok/1.png"

    service.set_preview_publisher(cancelling_publisher)
    try:
        with pytest.raises(ComputerControlError, match="取消|停止"):
            service.execute(
                run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=10, y=10)
            )
        assert not any(event.get("ok") is True for event in events)
        assert not any(event["type"] == "computer_observation" for event in events)
    finally:
        service.stop()


def test_settings_revoked_during_action_does_not_commit_success(
    service, running_run, owner_a, settings_state
):
    """动作执行期间设置关闭（generation 失配且能力撤销）：不提交成功，run 停止。"""
    backend = service.backend

    def revoke_during_execute(run_id, action):
        settings_state["browser"] = False
        settings_state["generation"] += 1

    backend.execute = revoke_during_execute
    events = []
    service.add_listener(events.append)
    try:
        with pytest.raises(ComputerControlError) as raised:
            service.execute(
                running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=10, y=10)
            )
        assert raised.value.code == "computer_disabled"
        assert not any(event.get("ok") is True for event in events)
        assert service.snapshot().status == "stopped"
    finally:
        service.stop()


def test_pause_during_action_still_commits_current_input(service, running_run, owner_a):
    """暂停只阻止下一动作：暂停在动作中途到达时，当前输入完成并提交成功。"""
    backend = service.backend

    def pause_during_execute(run_id, action):
        service.pause(running_run.run_id, "user_request")

    backend.execute = pause_during_execute
    events = []
    service.add_listener(events.append)
    try:
        observation = service.execute(
            running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=10, y=10)
        )
        assert observation.ok
        finished = [
            event for event in events if event["type"] == "computer_action_finished"
        ]
        assert len(finished) == 1 and finished[0]["ok"] is True
        assert service.snapshot().status == "paused"
        # 暂停后下一动作被拒绝，恢复后必须先 observe。
        with pytest.raises(ComputerControlError, match="暂停"):
            service.execute(
                running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=20, y=20)
            )
    finally:
        service.stop()


def test_native_batch_cancel_before_second_action_executes_nothing_further(
    owner_a,
):
    """native 批次：第一项成功后取消，第二项起零执行且不再发模型续推。"""
    from core.computer.provider_openai import (
        BoundNativeController,
        NativeComputerCall,
        execute_native_computer_call,
    )

    capability = NATIVE_BROWSER_CAPABILITY
    backend = FakeComputerBackend(target="browser")
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 1,
        abort_streams=lambda event: None,
    )
    run = service.start_run(owner_a, capability, target="browser")
    service.execute(run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))

    def cancel_on_first_observation(event):
        if event["type"] == "computer_observation":
            owner_a.cancel_event.set()

    service.add_listener(cancel_on_first_observation)
    call = NativeComputerCall(
        response_id="resp-a",
        call_id="call-a",
        actions=(
            ComputerAction(kind="click", target="browser", x=10, y=10),
            ComputerAction(kind="click", target="browser", x=20, y=20),
            ComputerAction(kind="click", target="browser", x=30, y=30),
        ),
    )
    bound = BoundNativeController(service, run.run_id, owner_a)
    try:
        with pytest.raises(ComputerControlError) as raised:
            execute_native_computer_call(call, bound)
        assert raised.value.code == "computer_cancelled"
        # 只有第一项真实执行；第二项在动作前检查即被拒绝，余项零执行。
        assert [entry[1] for entry in backend.execute_calls] == ["click"]
    finally:
        service.stop()


# ---------------- 单例 ----------------


def test_get_computer_control_service_lazy_singleton(monkeypatch):
    import core.computer.controller as controller

    monkeypatch.setattr(controller, "_SERVICE", None)
    first = get_computer_control_service()
    assert first is get_computer_control_service()


# ---------------- R4：留存 backend 身份匹配 ----------------


def test_browser_change_does_not_reuse_previous_channel(owner_a, owner_b):
    """审计复现迁移（tests/audits 同名用例）：Edge → Chrome 后不得复用留存实例。"""
    state = {"generation": 1, "browser": "edge"}
    created = []

    def factory(target, identity):
        backend = FakeComputerBackend(target=target)
        backend.browser = identity.browser
        created.append(backend)
        return backend

    service = ComputerControlService(
        backend_factory=factory,
        generation_provider=lambda: state["generation"],
        abort_streams=lambda event: None,
    )
    first = service.start_run(owner_a, GENERIC_CAPABILITY, target="browser")
    service.complete_run(first.run_id, owner_a)
    state.update(generation=2, browser="chrome")
    try:
        second = service.start_run(
            owner_b,
            replace(GENERIC_CAPABILITY, browser="chrome", settings_generation=2),
            target="browser",
        )
        assert service._run.backend.browser == "chrome"
        # 旧 Edge 实例被显式关闭，新实例按新身份创建。
        assert created[0].close_count == 1
        assert created[1] is service._run.backend
        assert second.run_id != first.run_id
    finally:
        service.stop()


def test_same_identity_retained_session_is_reused(service, running_run, owner_a, owner_b, capability):
    """相同身份：留存实例正常复用（同一 backend 对象），不重复创建。"""
    backend = service.backend
    service.complete_run(running_run.run_id, owner_a)
    second = service.start_run(owner_b, capability, target="browser")
    try:
        assert service.backend is backend
        assert backend.close_count == 0
        assert second.run_id != running_run.run_id
    finally:
        service.stop()


def test_retained_close_failure_fails_claim(service, running_run, owner_a, owner_b):
    """身份不匹配且旧实例 close 失败：本次启动显式失败，不静默丢弃旧对象。"""
    backend = service.backend
    service.complete_run(running_run.run_id, owner_a)
    backend.close_error = ComputerControlError("computer_cleanup_failed", "注入关闭失败")
    chrome_capability = replace(GENERIC_CAPABILITY, browser="chrome", settings_generation=2)
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_b, chrome_capability, target="browser")
    assert raised.value.code == "computer_cleanup_failed"
    assert service.snapshot().status == "failed"


def test_unusable_retained_session_fails_explicitly(service, running_run, owner_a, owner_b, capability):
    """身份相同但实例不可复用（用户已关闭窗口/不健康）：明确失败，不暗中复活。"""
    backend = service.backend
    service.complete_run(running_run.run_id, owner_a)
    backend.reusable = False
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_b, capability, target="browser")
    assert raised.value.code == "computer_browser_closed"
    assert backend.close_count == 1  # 旧实例已清理；下一轮可显式新建
    assert service.snapshot().status == "failed"
    third_owner = ComputerOwner(
        session_id="session-c", turn_id="turn-c", session_label="会话C",
        cancel_event=threading.Event(),
    )
    third = service.start_run(third_owner, capability, target="browser")
    assert third.status == "running"
    service.stop()


def test_factory_receives_frozen_identity_not_current_settings(service, running_run, owner_a, owner_b, settings_state, capability):
    """配置在 Turn 冻结后改变：工厂消费 run 冻结身份，不重读“当前选中浏览器”。"""
    seen_identities = []
    original_factory = service._backend_factory

    def recording_factory(target, identity):
        seen_identities.append(identity)
        return original_factory(target, identity)

    service._backend_factory = recording_factory
    service.complete_run(running_run.run_id, owner_a)
    # 留存身份匹配时复用，不调用工厂；改 profile 后身份失配，工厂收到新身份。
    assert seen_identities == []
    with pytest.raises(ComputerControlError):
        service._resolve_backend_identity("browser", replace(GENERIC_CAPABILITY, browser=""))
    second = service.start_run(owner_b, capability, target="browser")
    assert service.backend is service.created["browser"][0]  # 复用留存，未新建
    service.stop()


def test_complete_run_does_not_retain_unusable_backend(service, running_run, owner_a):
    """有未结束工作/不健康的实例不得进入 retained：complete 时按不留存关闭。"""
    backend = service.backend
    backend.reusable = False
    snapshot = service.complete_run(running_run.run_id, owner_a)
    assert snapshot.status == "completed"
    assert backend.close_count == 1
    assert service._retained == {}


# ---------------- R5：surface 身份变化接线 ----------------


def test_surface_changed_sets_require_observe_and_blocks_old_coordinates(service, running_run, owner_a):
    """backend 报 surface_changed：controller 立即使旧坐标作废，恢复必须先 observe。"""
    backend = service.backend
    backend.execute_error = ComputerControlError(
        "computer_surface_changed", "受控页面已切换"
    )
    with pytest.raises(ComputerControlError) as raised:
        service.execute(
            running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1)
        )
    assert raised.value.code == "computer_surface_changed"
    backend.execute_error = None
    # 不观察直接再点：被拒绝（旧坐标已作废）。
    with pytest.raises(ComputerControlError) as blocked:
        service.execute(
            running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1)
        )
    assert blocked.value.code == "computer_invalid_action"
    assert [entry[1] for entry in backend.execute_calls] == ["click"]
    # observe 后恢复可输入。
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    service.execute(
        running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1)
    )
    assert [entry[1] for entry in backend.execute_calls] == ["click", "click"]
    service.stop()


def test_observation_event_carries_surface_identity(service, running_run, owner_a):
    """computer_observation 事件携带本地 surface_id/generation（GUI 区分受控页）。"""
    events = []
    service.add_listener(events.append)
    try:
        service.execute(
            running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=5, y=5)
        )
        observations = [
            event for event in events if event["type"] == "computer_observation"
        ]
        assert len(observations) == 1
        assert "surface_id" in observations[0]
        assert "surface_generation" in observations[0]
    finally:
        service.stop()


def test_native_batch_surface_change_aborts_remaining_actions(owner_a):
    """native 批次中途切页：第一项后即中断，余项零执行。"""
    from core.computer.provider_openai import (
        BoundNativeController,
        NativeComputerCall,
        execute_native_computer_call,
    )

    backend = FakeComputerBackend(target="browser")
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 1,
        abort_streams=lambda event: None,
    )
    run = service.start_run(owner_a, NATIVE_BROWSER_CAPABILITY, target="browser")
    service.execute(run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    backend.execute_error = None

    original_execute = backend.execute
    state = {"calls": 0}

    def fail_from_second(run_id, action):
        state["calls"] += 1
        if state["calls"] >= 2:
            raise ComputerControlError("computer_surface_changed", "受控页面已切换")
        return original_execute(run_id, action)

    backend.execute = fail_from_second

    call = NativeComputerCall(
        response_id="resp-s",
        call_id="call-s",
        actions=(
            ComputerAction(kind="click", target="browser", x=10, y=10),
            ComputerAction(kind="click", target="browser", x=20, y=20),
            ComputerAction(kind="click", target="browser", x=30, y=30),
        ),
    )
    bound = BoundNativeController(service, run.run_id, owner_a)
    try:
        with pytest.raises(ComputerControlError) as raised:
            execute_native_computer_call(call, bound)
        assert raised.value.code == "computer_surface_changed"
        assert state["calls"] == 2  # 第二项被拒（零输入），第三项绝不执行
    finally:
        service.stop()


# ---------------- R6：无进展计数 ----------------


def test_three_valid_mouse_moves_do_not_stop_desktop(owner_a):
    """审计复现迁移（tests/audits 同名用例）：move 不进入无进展计数。"""
    backend = FakeComputerBackend(target="desktop")
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 1,
        abort_streams=lambda event: None,
    )
    run = service.start_run(owner_a, GENERIC_CAPABILITY, target="desktop")
    service.execute(run.run_id, owner_a, ComputerAction(kind="observe", target="desktop"))
    try:
        for x in (10, 20, 30):
            service.execute(
                run.run_id, owner_a, ComputerAction(kind="move", target="desktop", x=x, y=10)
            )
        assert service.snapshot().status == "running"
        assert [entry[1] for entry in backend.execute_calls] == ["move"] * 3
    finally:
        service.stop()


def test_changed_frame_resets_no_progress_count_to_zero(service, running_run, owner_a):
    """审计复现迁移（tests/audits 同名用例）：有进展后计数归零而非从 1 起算。"""
    backend = service.backend
    backend._screen_hashes = ["changed", "changed", "changed"]
    # 第一击已产生进展，随后只有两击无变化，不得按三次无进展停止。
    for x in (10, 20, 30):
        service.execute(
            running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=x, y=10)
        )
    assert service.snapshot().status == "running"
    service.stop()


def test_two_unchanged_clicks_remain_allowed(service, running_run, owner_a):
    """对照（审计保留）：仅两次不变点击允许继续。"""
    for x in (10, 20):
        service.execute(
            running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=x, y=10)
        )
    assert service.snapshot().status == "running"
    service.stop()


def test_progress_then_zero_one_two_three_stops(service, running_run, owner_a):
    """进展后计数序列 0→1→2→3：变化一击 + 三击不变才停止。

    每次动作后 controller 都会取新观察：p1 之后首个 s 仍是变化（计 0），
    随后三次 s 不变依次计 1/2/3，第 5 击停止。
    """
    backend = service.backend
    backend._screen_hashes = ["p1", "s", "s", "s", "s"]
    for _ in range(4):
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    assert service.snapshot().status == "running"
    with pytest.raises(ComputerControlError) as raised:
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    assert raised.value.code == "computer_no_progress"
    service.stop()


def test_observe_with_real_change_resets_count_and_baseline(service, running_run, owner_a):
    """豁免动作（observe）观察到真实变化：计数归零且基线更新，后续重新累计。"""
    backend = service.backend
    backend._screen_hashes = ["a", "a", "b", "b", "b", "b"]
    # 两击不变（计数 1、2）。
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    # observe 看到变化 b：计数归零、基线更新（不留过期基线）。
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    # 三击不变才停止（1、2、3）。
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    assert service.snapshot().status == "running"
    with pytest.raises(ComputerControlError) as raised:
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    assert raised.value.code == "computer_no_progress"
    service.stop()


def test_unchanged_observe_does_not_mask_existing_failures(service, running_run, owner_a):
    """豁免动作且画面不变：保留既有失败次数，不得靠插入 observe 掩盖重复失败。"""
    backend = service.backend
    backend._screen_hashes = ["same"] * 8
    # 首击相对基线有变化计 0，第 2/3 击不变计 1/2。
    for _ in range(3):
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    # 插入不变 observe：计数保留（2），下一击不变即达 3 停止。
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    with pytest.raises(ComputerControlError) as raised:
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    assert raised.value.code == "computer_no_progress"
    service.stop()


# ---------------- R9：动作耗时测量 ----------------


def test_action_finished_supplies_measured_duration(service, running_run, owner_a):
    """审计复现迁移（tests/audits 同名用例）：finished 事件携带真实耗时字段。"""
    events = []
    service.add_listener(events.append)
    try:
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
        finished = next(
            event for event in events if event["type"] == "computer_action_finished"
        )
        assert isinstance(finished.get("duration_ms"), (int, float))
        assert finished["duration_ms"] >= 0
        assert isinstance(finished.get("queue_ms"), (int, float))
        assert isinstance(finished.get("observe_ms"), (int, float))
        # observe 动作没有输入阶段：字段缺省（“未测量”），绝不默认 0ms。
        assert "input_ms" not in finished
        assert "wait_ms" not in finished
    finally:
        service.stop()


def test_action_finished_reports_input_and_wait_segments(service, running_run, owner_a):
    """输入/显式等待/观察分段真实出现；失败与取消同样携带耗时。"""
    events = []
    service.add_listener(events.append)
    try:
        service.execute(
            running_run.run_id, owner_a,
            ComputerAction(kind="wait", target="browser", duration_ms=100),
        )
        service.execute(
            running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1)
        )
        finished = [
            event for event in events if event["type"] == "computer_action_finished"
        ]
        wait_event, click_event = finished
        assert wait_event["wait_ms"] >= 0 and wait_event["input_ms"] == wait_event["wait_ms"]
        assert "observe_ms" in wait_event
        assert click_event["input_ms"] >= 0 and click_event["observe_ms"] >= 0
        assert "wait_ms" not in click_event
        # 失败路径同样携带耗时。
        service.backend.observe_error = ComputerControlError(
            "computer_action_failed", "注入观察失败"
        )
        with pytest.raises(ComputerControlError):
            service.execute(
                running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=2, y=2)
            )
        failed = [
            event for event in events
            if event["type"] == "computer_action_finished" and event["ok"] is False
        ]
        assert failed and isinstance(failed[-1].get("duration_ms"), (int, float))
    finally:
        service.stop()
