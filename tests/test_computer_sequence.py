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


def test_parse_sequence_accepts_element_actions():
    action = parse_computer_action({
        "kind": "sequence",
        "target": "desktop",
        "steps": [
            {"kind": "click_element", "target": "desktop", "element_ref": "el-1"},
            {"kind": "set_value_element", "target": "desktop",
             "element_ref": "el-2", "text": "发票号 A-001"},
        ],
    })
    assert action.kind == "sequence"
    assert [s.kind for s in action.steps] == ["click_element", "set_value_element"]
    assert [s.element_ref for s in action.steps] == ["el-1", "el-2"]
    assert action.steps[1].text == "发票号 A-001"


def test_parse_sequence_still_rejects_list_elements():
    with pytest.raises(ComputerControlError) as exc:
        parse_computer_action({
            "kind": "sequence",
            "target": "desktop",
            "steps": [{"kind": "list_elements", "target": "desktop"}],
        })
    assert exc.value.code == "computer_invalid_action"
    assert "list_elements" in exc.value.message


def test_parse_sequence_element_step_target_must_match():
    with pytest.raises(ComputerControlError) as exc:
        parse_computer_action({
            "kind": "sequence",
            "target": "desktop",
            "steps": [{"kind": "click_element", "target": "browser", "element_ref": "el-1"}],
        })
    assert "target" in exc.value.message


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
    from core.computer.controller import ComputerControlService

    backend = _RecordingBackend()
    # 注入固定 generation provider：用例与全局电脑操控设置状态解耦，
    # 否则同套件内更早的设置写入（如 /computer browser）会让 capability 的
    # settings_generation=0 失配而被误判 settings_disabled
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 0,
    )
    owner = ComputerOwner(
        session_id="s1", turn_id="t1", session_label="测试",
        cancel_event=threading.Event(),
    )
    capability = ComputerCapability(
        enabled=True, protocol="generic", targets=("desktop",), settings_generation=0,
    )
    service.start_run(owner, capability, target="desktop")
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
    from core.computer.controller import ComputerControlService

    backend = _RecordingBackend()
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 0,
    )
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


def test_sequence_reports_failed_step_index_and_not_submitted_on_step0_precheck():
    from core.computer.controller import ComputerControlService

    class _StepFailBackend(_RecordingBackend):
        def execute(self, run_id, action):
            self.calls.append(("execute", action.kind))
            raise ComputerControlError("computer_element_stale", "旧引用失效")

    backend = _StepFailBackend()
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 0,
    )
    owner = ComputerOwner("s1", "t1", "测试", threading.Event())
    cap = ComputerCapability(enabled=True, protocol="generic", targets=("desktop",), settings_generation=0)
    snap = service.start_run(owner, cap, target="desktop")
    service.execute(snap.run_id, owner, ComputerAction(kind="observe", target="desktop"))
    backend.calls.clear()
    action = parse_computer_action({
        "kind": "sequence", "target": "desktop",
        "steps": [
            {"kind": "click_element", "target": "desktop", "element_ref": "el-1"},
            {"kind": "keypress", "target": "desktop", "keys": ["ENTER"]},
        ],
    })
    with pytest.raises(ComputerControlError) as raised:
        service.execute(snap.run_id, owner, action)
    assert raised.value.details["failed_step_index"] == 0
    assert raised.value.details["submission_state"] == "not_submitted"
    assert raised.value.details["completed_steps"] == 0
    assert raised.value.details["total_steps"] == 2
    service.stop(snap.run_id, "test")


def test_sequence_reports_failed_step_index_and_submitted_after_partial_progress():
    from core.computer.controller import ComputerControlService

    class _SecondPreFailBackend(_RecordingBackend):
        def execute(self, run_id, action):
            self.calls.append(("execute", action.kind))
            if action.kind == "click_element":
                raise ComputerControlError("computer_element_stale", "第2步元素失效")

    backend = _SecondPreFailBackend()
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 0,
    )
    owner = ComputerOwner("s1", "t1", "测试", threading.Event())
    cap = ComputerCapability(enabled=True, protocol="generic", targets=("desktop",), settings_generation=0)
    snap = service.start_run(owner, cap, target="desktop")
    service.execute(snap.run_id, owner, ComputerAction(kind="observe", target="desktop"))
    backend.calls.clear()
    action = parse_computer_action({
        "kind": "sequence", "target": "desktop",
        "steps": [
            {"kind": "click", "target": "desktop", "x": 1, "y": 2},
            {"kind": "click_element", "target": "desktop", "element_ref": "el-9"},
            {"kind": "keypress", "target": "desktop", "keys": ["ENTER"]},
        ],
    })
    with pytest.raises(ComputerControlError) as raised:
        service.execute(snap.run_id, owner, action)
    assert raised.value.details["failed_step_index"] == 1
    assert raised.value.details["submission_state"] == "submitted"
    assert raised.value.details["completed_steps"] == 1
    assert raised.value.details["total_steps"] == 3
    service.stop(snap.run_id, "test")


def test_sequence_reports_failed_step_index_and_outcome_unknown_on_timeout():
    from core.computer.controller import ComputerControlService

    class _TimeoutBackend(_RecordingBackend):
        def execute(self, run_id, action):
            self.calls.append(("execute", action.kind))
            if action.kind == "keypress":
                raise ComputerControlError("computer_action_timeout", "按键超时")

    backend = _TimeoutBackend()
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        generation_provider=lambda: 0,
    )
    owner = ComputerOwner("s1", "t1", "测试", threading.Event())
    cap = ComputerCapability(enabled=True, protocol="generic", targets=("desktop",), settings_generation=0)
    snap = service.start_run(owner, cap, target="desktop")
    service.execute(snap.run_id, owner, ComputerAction(kind="observe", target="desktop"))
    action = parse_computer_action({
        "kind": "sequence", "target": "desktop",
        "steps": [
            {"kind": "click", "target": "desktop", "x": 1, "y": 2},
            {"kind": "keypress", "target": "desktop", "keys": ["ENTER"]},
        ],
    })
    with pytest.raises(ComputerControlError) as raised:
        service.execute(snap.run_id, owner, action)
    assert raised.value.details["failed_step_index"] == 1
    assert raised.value.details["submission_state"] == "outcome_unknown"
    service.stop(snap.run_id, "test")


def test_generic_error_result_keeps_9_keys_for_non_sequence_and_reports_sequence_fields():
    from core.computer import provider_generic

    single = json.loads(
        provider_generic._error_result(
            "browser",
            "click",
            ComputerControlError("computer_element_stale", "旧引用"),
        ).content
    )
    assert set(single) == {
        "ok", "target", "action", "screen_sha256", "width", "height",
        "origin_or_window", "error_code", "error_message",
    }

    seq = json.loads(
        provider_generic._error_result(
            "browser",
            "sequence",
            ComputerControlError(
                "computer_action_timeout",
                "超时期限已到",
                details={
                    "failed_step_index": 2,
                    "submission_state": "outcome_unknown",
                    "completed_steps": 2,
                    "total_steps": 4,
                },
            ),
        ).content
    )
    assert seq["failed_step_index"] == 2
    assert seq["submission_state"] == "outcome_unknown"
    assert seq["completed_steps"] == 2
    assert seq["total_steps"] == 4


def test_native_multi_action_call_attaches_failed_step_index_and_submission_state():
    from core.computer.provider_openai import (
        NativeComputerCall,
        execute_native_computer_call,
    )

    class _Ctrl:
        def __init__(self):
            self.calls = 0

        def execute(self, action):
            self.calls += 1
            if self.calls == 2:
                raise ComputerControlError("computer_action_timeout", "第二步超时")
            return ComputerObservation(run_id="r", target="browser", action=action.kind)

    call = NativeComputerCall(
        "resp_1",
        "call_1",
        (
            ComputerAction(kind="click", target="browser", x=1, y=2),
            ComputerAction(kind="type_text", target="browser", text="hi"),
        ),
        (),
    )
    with pytest.raises(ComputerControlError) as raised:
        execute_native_computer_call(call, _Ctrl())
    assert raised.value.details["failed_step_index"] == 1
    assert raised.value.details["submission_state"] == "outcome_unknown"
    assert raised.value.details["total_steps"] == 2
