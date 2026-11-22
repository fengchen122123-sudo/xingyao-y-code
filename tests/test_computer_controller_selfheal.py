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


def test_activation_and_unchanged_frame_do_not_replay_click():
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
    assert observation.screen_sha256 == "sha-A"
    assert observation.action == "click"
    assert backend.execute_calls == [(run_id, "click")]
    assert service.snapshot().action_count == 2


def test_screen_changed_click_does_not_replay():
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


def test_cancellation_after_click_prevents_followup_input():
    cancel_event = threading.Event()
    backend = _ScriptedBackend(
        [
            {"screen_sha256": "sha-A"},
            {"screen_sha256": "sha-A", "input_activated": True},
            {"screen_sha256": "sha-B"},
        ],
        on_execute=lambda call_index: (
            cancel_event.set() if call_index == 1 else None
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
    assert backend.execute_calls == [(run_id, "click")]


def test_observe_failure_after_click_does_not_replay_click():
    class _FailSecondObserveBackend(_ScriptedBackend):
        def observe(self, run_id):
            if self.execute_calls:
                raise ComputerControlError("computer_action_failed", "截屏失败")
            return super().observe(run_id)

    backend = _FailSecondObserveBackend([{"screen_sha256": "sha-A"}])
    service = _service_with(backend)
    owner = _owner()
    run_id = _start_and_observe(service, owner)
    with pytest.raises(ComputerControlError) as raised:
        service.execute(
            run_id,
            owner,
            ComputerAction(kind="click", target="desktop", x=10, y=20),
        )
    assert raised.value.code == "computer_action_failed"
    assert backend.execute_calls == [(run_id, "click")]
