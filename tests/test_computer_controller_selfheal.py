"""通用化重构 Task D：激活吞击 no-op 点击自愈（controller 层）测试。

用脚本化 desktop backend fake 驱动真实 ComputerControlService，验证：
- click 后画面与点击前完全一致且发生前台切换 → 同一动作自动补击恰好一次；
- 画面已变化（非吞击）或未发生激活 → 不补击；
- 补击路径上取消事件置位 → 抛 computer_cancelled。
绝不触碰真实键鼠、浏览器或截屏。
"""

from __future__ import annotations

import threading

import pytest

from core.computer.controller import ComputerControlService
from core.computer.models import (
    ComputerAction,
    ComputerControlError,
    ComputerObservation,
    ComputerOwner,
)
from tests.computer_fakes import GENERIC_CAPABILITY


class _ScriptedBackend:
    """desktop backend fake：observe 按 script 产出，execute 记录并可挂钩子。"""

    def __init__(self, observe_script, on_execute=None):
        self.target = "desktop"
        self.observe_script = list(observe_script)
        self.execute_calls = []
        self.start_calls = 0
        self.release_count = 0
        self.close_count = 0
        self._on_execute = on_execute
        self._lock = threading.Lock()

    def start(self):
        self.start_calls += 1

    def observe(self, run_id):
        with self._lock:
            script = self.observe_script.pop(0)
        fields = {
            "run_id": run_id,
            "target": "desktop",
            "action": "observe",
            "width": 1280,
            "height": 720,
            "window_title": "Fake Window",
        }
        fields.update(script)
        return ComputerObservation(**fields)

    def execute(self, run_id, action):
        with self._lock:
            self.execute_calls.append((run_id, action.kind))
            hook = self._on_execute
        if hook is not None:
            hook(len(self.execute_calls))

    def release_inputs(self):
        self.release_count += 1

    def close(self):
        self.close_count += 1

    def is_reusable(self):
        return True


def _owner(cancel_event=None):
    return ComputerOwner(
        session_id="session-a",
        turn_id="turn-a",
        session_label="会话A",
        cancel_event=cancel_event or threading.Event(),
    )


def _service_with(backend):
    return ComputerControlService(
        backend_factory=lambda target, identity: backend,
        settings_loader=lambda: None,
        generation_provider=lambda: GENERIC_CAPABILITY.settings_generation,
        abort_streams=lambda event: None,
    )


def _start_and_observe(service, owner):
    snapshot = service.start_run(owner, GENERIC_CAPABILITY, target="desktop")
    service.execute(
        snapshot.run_id,
        owner,
        ComputerAction(kind="observe", target="desktop"),
    )
    return snapshot.run_id


def test_activation_swallowed_click_replays_exactly_once():
    """吞击场景：画面未变 + 发生激活 → 同一动作自动补击恰好一次。"""
    backend = _ScriptedBackend(
        [
            {"screen_sha256": "sha-A"},
            {"screen_sha256": "sha-A", "input_activated": True},
            {"screen_sha256": "sha-B"},
        ]
    )
    service = _service_with(backend)
    owner = _owner()
    run_id = _start_and_observe(service, owner)
    observation = service.execute(
        run_id,
        owner,
        ComputerAction(kind="click", target="desktop", x=10, y=20),
    )
    assert observation.screen_sha256 == "sha-B"
    assert observation.action == "click"
    assert backend.execute_calls == [(run_id, "click"), (run_id, "click")]
    # 补击不新增 ordinal：observe + click 仍共 2 个动作。
    assert service.snapshot().action_count == 2


def test_screen_changed_click_does_not_replay():
    """非吞击：点击后画面已变化 → 不补击。"""
    backend = _ScriptedBackend(
        [
            {"screen_sha256": "sha-A"},
            {"screen_sha256": "sha-B", "input_activated": True},
        ]
    )
    service = _service_with(backend)
    owner = _owner()
    run_id = _start_and_observe(service, owner)
    observation = service.execute(
        run_id,
        owner,
        ComputerAction(kind="click", target="desktop", x=10, y=20),
    )
    assert observation.screen_sha256 == "sha-B"
    assert backend.execute_calls == [(run_id, "click")]


def test_click_without_activation_does_not_replay():
    """画面未变但未发生前台切换：不满足吞击判定 → 不补击。"""
    backend = _ScriptedBackend(
        [
            {"screen_sha256": "sha-A"},
            {"screen_sha256": "sha-A", "input_activated": False},
        ]
    )
    service = _service_with(backend)
    owner = _owner()
    run_id = _start_and_observe(service, owner)
    service.execute(
        run_id, owner, ComputerAction(kind="click", target="desktop", x=10, y=20)
    )
    assert backend.execute_calls == [(run_id, "click")]


def test_replay_honours_cancellation_before_and_after():
    """补击路径复核取消：第二次执行后取消事件置位 → 抛 computer_cancelled。"""
    cancel_event = threading.Event()
    backend = _ScriptedBackend(
        [
            {"screen_sha256": "sha-A"},
            {"screen_sha256": "sha-A", "input_activated": True},
            {"screen_sha256": "sha-B"},
        ],
        on_execute=lambda call_index: (
            cancel_event.set() if call_index == 2 else None
        ),
    )
    service = _service_with(backend)
    owner = _owner(cancel_event)
    run_id = _start_and_observe(service, owner)
    with pytest.raises(ComputerControlError) as exc:
        service.execute(
            run_id,
            owner,
            ComputerAction(kind="click", target="desktop", x=10, y=20),
        )
    assert exc.value.code == "computer_cancelled"
    # 补击确实发生了（第二次 execute 进入），但未走到成功提交。
    assert backend.execute_calls == [(run_id, "click"), (run_id, "click")]
