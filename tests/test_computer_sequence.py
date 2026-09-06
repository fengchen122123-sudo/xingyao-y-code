"""sequence 批量动作（2026-09-06 协议扩展）的解析、执行、投影测试。"""

from __future__ import annotations

import json
import threading

import pytest

from core.computer.actions import parse_computer_action, sanitized_action_summary
from core.computer.models import (
    BackendIdentity,
    ComputerAction,
    ComputerCapability,
    ComputerControlError,
    ComputerObservation,
    ComputerOwner,
)


# ---------------- Task 1：解析 ----------------


def test_parse_sequence_ok():
    action = parse_computer_action({
        "kind": "sequence",
        "target": "desktop",
        "steps": [
            {"kind": "click", "target": "desktop", "x": 100, "y": 200},
            {"kind": "type_text", "target": "desktop", "text": "你好"},
            {"kind": "keypress", "target": "desktop", "keys": ["ENTER"]},
        ],
    })
    assert action.kind == "sequence"
    assert [s.kind for s in action.steps] == ["click", "type_text", "keypress"]


def test_parse_sequence_rejects_nested_sequence():
    with pytest.raises(ComputerControlError) as exc:
        parse_computer_action({
            "kind": "sequence", "target": "desktop",
            "steps": [{"kind": "sequence", "target": "desktop", "steps": []}],
        })
    assert exc.value.code == "computer_invalid_action"


def test_parse_sequence_rejects_observe_and_semantic():
    for nested in (
        {"kind": "observe", "target": "desktop"},
        {"kind": "request_user_takeover", "target": "desktop", "reason_code": "login_required"},
        {"kind": "list_surfaces", "target": "desktop"},
        {"kind": "navigate", "target": "browser", "url": "https://example.com"},
    ):
        with pytest.raises(ComputerControlError):
            parse_computer_action({"kind": "sequence", "target": "desktop", "steps": [nested]})


def test_parse_sequence_rejects_target_mismatch():
    with pytest.raises(ComputerControlError) as exc:
        parse_computer_action({
            "kind": "sequence", "target": "desktop",
            "steps": [{"kind": "click", "target": "browser", "x": 1, "y": 2}],
        })
    assert "target" in exc.value.message


def test_parse_sequence_rejects_step_count():
    steps = [{"kind": "wait", "target": "desktop", "duration_ms": 100}] * 11
    with pytest.raises(ComputerControlError):
        parse_computer_action({"kind": "sequence", "target": "desktop", "steps": steps})


def test_parse_sequence_rejects_empty_steps():
    with pytest.raises(ComputerControlError):
        parse_computer_action({"kind": "sequence", "target": "desktop", "steps": []})


# ---------------- Task 2：controller 执行 ----------------


class _RecordingBackend:
    target = "desktop"

    def __init__(self):
        self.calls = []

    def start(self):
        pass

    def execute(self, run_id, action):
        self.calls.append(("execute", action.kind))

    def observe(self, run_id):
        self.calls.append(("observe", None))
        return ComputerObservation(run_id=run_id, target="desktop", action="sequence")

    def release_inputs(self):
        pass

    def close(self):
        pass

    def is_reusable(self):
        return False


def test_controller_sequence_executes_all_steps_then_single_observe():
    """sequence：backend.execute 按序收到全部子动作，observe 恰一次。"""
    from core.computer.controller import ComputerControlService

    backend = _RecordingBackend()
    service = ComputerControlService(backend_factory=lambda target, identity: backend)
    owner = ComputerOwner(
        session_id="s1", turn_id="t1", session_label="测试",
        cancel_event=threading.Event(),
    )
    capability = ComputerCapability(
        enabled=True, protocol="generic", targets=("desktop",), settings_generation=0,
    )
    service.start_run(owner, capability, target="desktop")
    # 协议规则：run 建立后第一次必须先 observe 清除 require_observe 基线。
    service.execute(service.snapshot().run_id, owner, ComputerAction(kind="observe", target="desktop"))
    backend.calls.clear()
    action = parse_computer_action({
        "kind": "sequence", "target": "desktop",
        "steps": [
            {"kind": "click", "target": "desktop", "x": 1, "y": 2},
            {"kind": "keypress", "target": "desktop", "keys": ["ENTER"]},
        ],
    })
    observation = service.execute(service.snapshot().run_id, owner, action)
    assert [("execute", "click"), ("execute", "keypress"), ("observe", None)] == backend.calls
    assert observation.ok


def test_controller_sequence_stops_on_cancel():
    """取消事件置位后 sequence 剩余子动作不再执行。"""
    from core.computer.controller import ComputerControlService

    backend = _RecordingBackend()
    service = ComputerControlService(backend_factory=lambda target, identity: backend)
    cancel_event = threading.Event()
    owner = ComputerOwner(
        session_id="s1", turn_id="t1", session_label="测试", cancel_event=cancel_event,
    )
    capability = ComputerCapability(
        enabled=True, protocol="generic", targets=("desktop",), settings_generation=0,
    )
    service.start_run(owner, capability, target="desktop")
    service.execute(service.snapshot().run_id, owner, ComputerAction(kind="observe", target="desktop"))
    backend.calls.clear()

    def _cancel_on_first_step(run_id, action):
        backend.calls.append(("execute", action.kind))
        cancel_event.set()

    backend.execute = _cancel_on_first_step
    action = parse_computer_action({
        "kind": "sequence", "target": "desktop",
        "steps": [
            {"kind": "click", "target": "desktop", "x": 1, "y": 2},
            {"kind": "keypress", "target": "desktop", "keys": ["ENTER"]},
        ],
    })
    with pytest.raises(ComputerControlError) as exc:
        service.execute(service.snapshot().run_id, owner, action)
    assert exc.value.code == "computer_cancelled"
    assert ("execute", "keypress") not in backend.calls


# ---------------- Task 3：投影与提示 ----------------


def test_sequence_summary_is_sanitized():
    action = parse_computer_action({
        "kind": "sequence", "target": "desktop",
        "steps": [
            {"kind": "type_text", "target": "desktop", "text": "机密正文"},
            {"kind": "click", "target": "desktop", "x": 5, "y": 6},
        ],
    })
    summary = sanitized_action_summary(action)
    assert summary["steps_count"] == 2
    assert summary["steps"][0]["text_code_points"] == 4
    assert "机密正文" not in json.dumps(summary, ensure_ascii=False)
    assert summary["steps"][1]["x"] == 5


def test_generic_prompt_mentions_sequence():
    from core.computer.provider_generic import build_generic_dynamic_prompt

    cap = ComputerCapability(enabled=True, protocol="generic", targets=("desktop",))
    prompt = build_generic_dynamic_prompt(cap)
    assert "sequence" in prompt
