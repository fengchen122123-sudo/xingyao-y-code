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

    run = service.start_run(owner_a, capability, target=None)
    service.execute(run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    return run


def test_every_event_carries_owner_session_id(service, owner_a, capability):
    events = []
    service.add_listener(events.append)
    run = service.start_run(owner_a, capability, target=None)
    service.execute(run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))

    assert events
    assert {"computer_state", "computer_observation"} <= {event["type"] for event in events}
    assert all(event["owner_session_id"] == "session-a" for event in events)


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
    assert service.backend.release_count == 1
    assert service.backend.close_count == 0
    new_run = service.start_run(owner_b, capability, target="browser")
    assert new_run.run_id != running_run.run_id


def test_complete_run_from_waiting_for_user_releases_lease(
    service, running_run, owner_a, owner_b, capability
):
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
    new_run_id = service.start_run(owner_b, capability, target="browser").run_id
    assert new_run_id != running_run.run_id


def test_stop_is_idempotent_and_releases_inputs_once(service, running_run):
    first = service.stop(running_run.run_id, "hotkey")
    second = service.stop(running_run.run_id, "hotkey")
    assert first.status == second.status == "stopped"
    assert service.backend.release_count == 1
    assert service.backend.close_count == 1


def test_stop_closes_backend_admission_before_releasing_inputs(
    service, running_run, owner_a
):
    backend = service.backend
    calls = []
    original_release = backend.release_inputs

    backend.request_stop = lambda: calls.append("request_stop")

    def record_release():
        calls.append("release_inputs")
        original_release()

    backend.release_inputs = record_release

    snapshot = service.stop(running_run.run_id, "user_stop", cancel_owner=False)

    assert snapshot.status == "stopped"
    assert calls == ["request_stop", "release_inputs"]
    assert not owner_a.cancel_event.is_set()


def test_stop_from_stopping_is_idempotent(service, running_run):
    service._transition(service._run, "stopping")
    service._run.stop_cleanup_in_progress = True
    snapshot = service.stop(running_run.run_id, "again")
    assert snapshot.status == "stopping"
    assert service.backend.release_count == 0
    assert service.backend.close_count == 0
    assert service.snapshot().run_id == running_run.run_id


def test_stop_during_backend_start_holds_lease_until_backend_is_closed(
    owner_a, owner_b, capability
):
    start_entered = threading.Event()
    allow_start_to_finish = threading.Event()
    backend = FakeComputerBackend(target="desktop")
    original_start = backend.start

    def blocked_start():
        original_start()
        start_entered.set()
        assert allow_start_to_finish.wait(timeout=2)

    backend.start = blocked_start
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 1,
        abort_streams=lambda event: None,
    )
    start_errors = []

    def start_run():
        try:
            service.start_run(owner_a, capability, target="desktop")
        except ComputerControlError as error:
            start_errors.append(error)

    starter = threading.Thread(target=start_run)
    starter.start()
    try:
        assert start_entered.wait(timeout=2)
        run_id = service.snapshot().run_id
        stopping = service.stop(run_id, "user_stop", cancel_owner=False)

        assert stopping.status == "stopping"
        assert not owner_a.cancel_event.is_set()
        with pytest.raises(ComputerControlError) as raised:
            service.start_run(owner_b, capability, target="desktop")
        assert raised.value.code == "computer_session_busy"
        assert backend.close_count == 0
    finally:
        allow_start_to_finish.set()
        starter.join(timeout=3)

    assert not starter.is_alive()
    assert start_errors and start_errors[0].code == "computer_cancelled"
    assert backend.release_count == 1
    assert backend.close_count == 1
    assert service.snapshot().status == "stopped"


def test_transition_table_rejects_skipping_completing(service, running_run):
    run = service._run
    with pytest.raises(ComputerControlError) as raised:
        service._transition(run, "completed")
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
    settings_state["desktop"] = False
    settings_state["generation"] += 1
    observation = service.execute(
        running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser")
    )
    assert observation.ok
    assert service.loader_calls
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
    backend._screen_hashes = ["h1", "h1", "h1", "h1"]
    for _ in range(3):
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    assert service.snapshot().status == "running"
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


def test_consecutive_wait_for_does_not_count_no_progress(service, owner_a, capability):
    run = service.start_run(owner_a, capability, target="desktop")
    service.execute(run.run_id, owner_a, ComputerAction(kind="observe", target="desktop"))
    backend = service.backend
    assert backend.target == "desktop"
    backend._screen_hashes = ["same"] * 12
    for _ in range(6):
        observation = service.execute(
            run.run_id,
            owner_a,
            ComputerAction(
                kind="wait_for",
                target="desktop",
                condition="text_exists",
                text="保存成功",
                timeout_ms=1000,
                interval_ms=100,
            ),
        )
        assert observation.ok
    assert service.snapshot().status == "running"
    assert service.backend.close_count == 0


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
    assert events[0]["action_ordinal"] == 2
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


def test_stop_sets_cancel_event_and_aborts_inflight_streams(service, owner_a, capability):
    run = service.start_run(owner_a, capability, target="desktop")
    snapshot = service.stop(run.run_id, "hotkey")
    assert snapshot.status == "stopped"
    assert snapshot.stop_reason == "hotkey"
    assert owner_a.cancel_event.is_set()
    assert service.abort_calls == [owner_a.cancel_event]


def test_stop_cleanup_error_holds_lease_until_cleanup_retry(
    service, running_run, owner_b, capability
):
    backend = service.backend
    service.backend.close_error = RuntimeError("close boom")
    snapshot = service.stop(running_run.run_id, "hotkey")
    assert snapshot.status == "failed"
    assert snapshot.error_code
    assert service._run is not None
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_b, capability, target="desktop")
    assert raised.value.code == "computer_session_busy"

    backend.close_error = None
    retried = service.stop(running_run.run_id, "retry_cleanup", cancel_owner=False)
    assert retried.status == "stopped"
    assert backend.release_count == 2
    assert backend.close_count == 2
    assert service._run is None


def test_turn_end_finally_complete_or_stop_leaves_no_running_lease(
    service, owner_a, owner_b, capability
):
    run = service.start_run(owner_a, capability, target="desktop")
    snapshot = service.complete_run(run.run_id, owner_a)
    assert snapshot.status == "completed"
    desktop = service.created["desktop"][-1]
    assert desktop.release_count == 1
    assert desktop.close_count == 1
    second = service.start_run(owner_b, capability, target="desktop")
    assert second.run_id != run.run_id
    stopped = service.stop(second.run_id, "turn_end")
    assert stopped.status == "stopped"
    assert service.snapshot().status == "stopped"
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


def test_start_run_closes_backend_when_stopped_during_claim(service, owner_a, capability):
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
    created = []
    state = {"generation": 1}

    def factory(target, identity):
        backend = FakeComputerBackend(target=target)
        created.append(backend)
        state["generation"] += 1
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


def test_cancel_during_action_must_not_publish_success(owner_a, capability):
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
        assert len(finished) == 1 and finished[0]["ok"] is False
        assert finished[0]["error_code"] == "computer_cancelled"
        assert not any(event["type"] == "computer_observation" for event in events)
    finally:
        service.stop()


def test_cancel_during_observe_must_not_publish_success(service, running_run, owner_a):
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
        with pytest.raises(ComputerControlError, match="暂停"):
            service.execute(
                running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=20, y=20)
            )
    finally:
        service.stop()


def test_native_batch_cancel_before_second_action_executes_nothing_further(
    owner_a,
):
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
        assert [entry[1] for entry in backend.execute_calls] == ["click"]
    finally:
        service.stop()


def test_get_computer_control_service_lazy_singleton(monkeypatch):
    import core.computer.controller as controller

    monkeypatch.setattr(controller, "_SERVICE", None)
    first = get_computer_control_service()
    assert first is get_computer_control_service()


def test_browser_change_does_not_reuse_previous_channel(owner_a, owner_b):
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
        assert created[0].close_count == 1
        assert created[1] is service._run.backend
        assert second.run_id != first.run_id
    finally:
        service.stop()


def test_same_identity_retained_session_is_reused(service, running_run, owner_a, owner_b, capability):
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
    backend = service.backend
    service.complete_run(running_run.run_id, owner_a)
    backend.close_error = ComputerControlError("computer_cleanup_failed", "注入关闭失败")
    chrome_capability = replace(GENERIC_CAPABILITY, browser="chrome", settings_generation=2)
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_b, chrome_capability, target="browser")
    assert raised.value.code == "computer_cleanup_failed"
    assert service.snapshot().status == "failed"


def test_unusable_retained_session_fails_explicitly(service, running_run, owner_a, owner_b, capability):
    backend = service.backend
    service.complete_run(running_run.run_id, owner_a)
    backend.reusable = False
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_b, capability, target="browser")
    assert raised.value.code == "computer_browser_closed"
    assert backend.close_count == 1
    assert service.snapshot().status == "failed"
    third_owner = ComputerOwner(
        session_id="session-c", turn_id="turn-c", session_label="会话C",
        cancel_event=threading.Event(),
    )
    third = service.start_run(third_owner, capability, target="browser")
    assert third.status == "running"
    service.stop()


def test_factory_receives_frozen_identity_not_current_settings(service, running_run, owner_a, owner_b, settings_state, capability):
    seen_identities = []
    original_factory = service._backend_factory

    def recording_factory(target, identity):
        seen_identities.append(identity)
        return original_factory(target, identity)

    service._backend_factory = recording_factory
    service.complete_run(running_run.run_id, owner_a)
    assert seen_identities == []
    with pytest.raises(ComputerControlError):
        service._resolve_backend_identity("browser", replace(GENERIC_CAPABILITY, browser=""))
    second = service.start_run(owner_b, capability, target="browser")
    assert service.backend is service.created["browser"][0]
    service.stop()


def test_complete_run_does_not_retain_unusable_backend(service, running_run, owner_a):
    backend = service.backend
    backend.reusable = False
    snapshot = service.complete_run(running_run.run_id, owner_a)
    assert snapshot.status == "completed"
    assert backend.close_count == 1
    assert service._retained == {}


def test_surface_changed_sets_require_observe_and_blocks_old_coordinates(service, running_run, owner_a):
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
    with pytest.raises(ComputerControlError) as blocked:
        service.execute(
            running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1)
        )
    assert blocked.value.code == "computer_invalid_action"
    assert [entry[1] for entry in backend.execute_calls] == ["click"]
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    service.execute(
        running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1)
    )
    assert [entry[1] for entry in backend.execute_calls] == ["click", "click"]
    service.stop()


def test_observation_event_carries_surface_identity(service, running_run, owner_a):
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


def test_three_valid_mouse_moves_do_not_stop_desktop(owner_a):
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
    backend = service.backend
    backend._screen_hashes = ["changed", "changed", "changed"]
    for x in (10, 20, 30):
        service.execute(
            running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=x, y=10)
        )
    assert service.snapshot().status == "running"
    service.stop()


def test_two_unchanged_clicks_remain_allowed(service, running_run, owner_a):
    for x in (10, 20):
        service.execute(
            running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=x, y=10)
        )
    assert service.snapshot().status == "running"
    service.stop()


def test_progress_then_zero_one_two_three_stops(service, running_run, owner_a):
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
    backend = service.backend
    backend._screen_hashes = ["a", "a", "b", "b", "b", "b"]
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    assert service.snapshot().status == "running"
    with pytest.raises(ComputerControlError) as raised:
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    assert raised.value.code == "computer_no_progress"
    service.stop()


def test_unchanged_observe_does_not_mask_existing_failures(service, running_run, owner_a):
    backend = service.backend
    backend._screen_hashes = ["same"] * 8
    for _ in range(3):
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    service.execute(running_run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    with pytest.raises(ComputerControlError) as raised:
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))
    assert raised.value.code == "computer_no_progress"
    service.stop()


def test_action_finished_supplies_measured_duration(service, running_run, owner_a):
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


def test_wait_for_finished_reports_wait_not_input(service, owner_a, capability):
    events = []
    service.add_listener(events.append)
    run = service.start_run(owner_a, capability, target="desktop")
    try:
        service.execute(
            run.run_id, owner_a, ComputerAction(kind="observe", target="desktop")
        )
        service.execute(
            run.run_id, owner_a,
            ComputerAction(
                kind="wait_for",
                target="desktop",
                condition="text_exists",
                text="登录完成",
                timeout_ms=8000,
                interval_ms=300,
            ),
        )
        finished = [
            event for event in events
            if event["type"] == "computer_action_finished"
            and event["action_kind"] == "wait_for"
        ]
        assert len(finished) == 1 and finished[0]["ok"] is True
        assert finished[0]["input_ms"] == 0
        assert isinstance(finished[0]["wait_ms"], (int, float))
        assert finished[0]["wait_ms"] >= 0
    finally:
        service.stop()


def test_wait_for_timeout_also_reports_wait_segment(service, owner_a, capability):
    events = []
    service.add_listener(events.append)
    run = service.start_run(owner_a, capability, target="desktop")
    try:
        service.execute(
            run.run_id, owner_a, ComputerAction(kind="observe", target="desktop")
        )
        service.backend.execute_error = ComputerControlError(
            "computer_wait_timeout", "条件等待超时"
        )
        with pytest.raises(ComputerControlError) as raised:
            service.execute(
                run.run_id, owner_a,
                ComputerAction(
                    kind="wait_for",
                    target="desktop",
                    condition="window_active",
                    window_name="记事本",
                ),
            )
        assert raised.value.code == "computer_wait_timeout"
        failed = [
            event for event in events
            if event["type"] == "computer_action_finished"
            and event["action_kind"] == "wait_for"
        ]
        assert failed and failed[-1]["ok"] is False
        assert failed[-1]["input_ms"] == 0
        assert isinstance(failed[-1]["wait_ms"], (int, float))
        assert failed[-1]["wait_ms"] >= 0
    finally:
        service.stop()


def test_run_generation_and_event_seq_sync_and_current_action_clears_on_finish(
    service, owner_a, owner_b, capability
):
    events = []
    service.add_listener(events.append)
    first = service.start_run(owner_a, capability, target="browser")
    try:
        assert first.run_generation >= 1
        assert first.event_seq >= 1
        service.execute(
            first.run_id, owner_a, ComputerAction(kind="observe", target="browser")
        )
        assert service.snapshot().current_action == ""
        service.backend.execute_error = ComputerControlError(
            "computer_action_failed", "注入动作失败"
        )
        with pytest.raises(ComputerControlError):
            service.execute(
                first.run_id,
                owner_a,
                ComputerAction(kind="click", target="browser", x=10, y=10),
            )
        assert service.snapshot().current_action == ""
    finally:
        service.stop(first.run_id, "done")

    second = service.start_run(owner_b, capability, target="browser")
    try:
        assert second.run_generation > first.run_generation
        second_events = [e for e in events if e.get("run_id") == second.run_id]
        assert second_events
        assert all(
            e.get("run_generation") == second.run_generation for e in second_events
        )
    finally:
        service.stop(second.run_id, "done")


def test_stop_unconfirmed_keeps_stopping_status_and_lease_until_retry(
    service, running_run, owner_b, capability
):
    backend = service.backend
    backend.close_error = ComputerControlError(
        "computer_stop_unconfirmed", "执行端未确认退出"
    )
    snapshot = service.stop(running_run.run_id, "user_stop", cancel_owner=False)
    assert snapshot.status == "stopping"
    assert snapshot.error_code == "computer_stop_unconfirmed"
    assert service._run is not None
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_b, capability, target="desktop")
    assert raised.value.code == "computer_session_busy"

    backend.close_error = None
    retried = service.stop(running_run.run_id, "retry_cleanup", cancel_owner=False)
    assert retried.status == "stopped"
    assert service._run is None


def test_stop_still_closes_backend_when_release_inputs_fails(
    service, running_run
):
    backend = service.backend
    fail_release = {"active": True}
    orig_release = backend.release_inputs

    def flaky_release():
        orig_release()
        if fail_release["active"]:
            raise ComputerControlError("computer_cleanup_failed", "释放输入失败")

    backend.release_inputs = flaky_release
    snapshot = service.stop(running_run.run_id, "hotkey")
    assert snapshot.status == "failed"
    assert snapshot.error_code == "computer_cleanup_failed"
    assert backend.release_count == 1
    assert backend.close_count == 1
    fail_release["active"] = False
    retried = service.stop(running_run.run_id, "retry_cleanup", cancel_owner=False)
    assert retried.status == "stopped"
    assert backend.release_count == 2
    assert backend.close_count == 2


@pytest.mark.parametrize(
    ("target", "browser", "expected_mode"),
    [
        ("desktop", "", ""),
        ("browser", "edge", "external"),
        ("browser", "embedded", "embedded"),
    ],
)
def test_snapshot_and_events_expose_frozen_browser_mode_under_lock(
    target, browser, expected_mode
):
    import threading
    from core.computer.controller import ComputerControlService
    from core.computer.models import ComputerCapability, ComputerOwner
    from tests.computer_fakes import FakeComputerBackend

    events = []
    backend = FakeComputerBackend(target=target)
    cap = ComputerCapability(
        enabled=True,
        protocol="generic",
        targets=(target,),
        browser=browser,
        settings_generation=1,
    )
    service = ComputerControlService(
        backend_factory=lambda _t, _i: backend,
        generation_provider=lambda: 1,
        embedded_endpoint_provider=(
            (lambda _sid: "http://127.0.0.1:9555") if browser == "embedded" else None
        ),
        abort_streams=lambda _ev: None,
    )
    service.add_listener(events.append)
    owner = ComputerOwner(
        session_id="sess-mode",
        turn_id="turn-1",
        session_label="sess-mode",
        cancel_event=threading.Event(),
    )

    snap = service.start_run(owner, cap, target=target)
    try:
        assert snap.target == target
        assert snap.browser == browser
        assert snap.browser_mode == expected_mode
        assert snap.to_public_dict()["browser_mode"] == expected_mode

        service.execute(
            snap.run_id,
            owner,
            ComputerAction(kind="observe", target=target),
        )
        state_events = [e for e in events if e.get("type") == "computer_state"]
        obs_events = [e for e in events if e.get("type") == "computer_observation"]
        assert state_events
        assert obs_events
        for ev in state_events:
            assert ev["target"] == target
            assert ev["browser"] == browser
            assert ev["browser_mode"] == expected_mode
        assert obs_events[-1]["target"] == target
        assert obs_events[-1]["browser"] == browser
        assert obs_events[-1]["browser_mode"] == expected_mode
    finally:
        service.stop(snap.run_id, "done")
