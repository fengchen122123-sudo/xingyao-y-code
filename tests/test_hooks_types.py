"""C1：Hooks 契约层测试（事件 / 注册项 / stdin-stdout 契约 / 合并规则）。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core.hooks.types import (
    HookEvent,
    HookInput,
    HookOutput,
    HookRegistration,
    HookRunResult,
)


def test_hook_events_are_four():
    assert {e.value for e in HookEvent} == {
        "SessionStart",
        "PreToolUse",
        "PostToolUse",
        "Stop",
    }


def test_registration_is_frozen():
    reg = HookRegistration(
        event=HookEvent.STOP,
        matcher="*",
        command="echo",
        timeout_ms=1000,
        status_message="",
        source_path="x",
        scope="user",
        digest="d",
    )
    with pytest.raises(FrozenInstanceError):
        reg.command = "other"


def _hook_input(**overrides):
    values = dict(
        hook_event_name="PreToolUse",
        session_id="s1",
        turn_id="t1",
        cwd="D:/w",
        permission_mode="full",
        timestamp="2026-09-21T10:00:00+08:00",
    )
    values.update(overrides)
    return HookInput(**values)


def test_hook_input_wire_omits_empty_event_fields():
    payload = _hook_input().to_wire()
    assert "tool_name" not in payload and "tool_input" not in payload

    payload2 = _hook_input(
        tool_name="write_file", tool_input={"file_path": "a.py"}
    ).to_wire()
    assert payload2["tool_name"] == "write_file"
    assert payload2["tool_input"] == {"file_path": "a.py"}


def test_hook_output_from_wire_tolerates_malformed_fields():
    out = HookOutput.from_wire({"continue": "yes", "decision": "maybe", "reason": 5})
    assert out.continue_ is True and out.decision == "" and out.reason == ""

    assert HookOutput.from_wire("not-a-dict").continue_ is True

    ok = HookOutput.from_wire({"continue": False, "decision": "block", "reason": "no"})
    assert ok.requests_block() is True and ok.reason == "no"
    assert HookOutput().requests_block() is False


def test_merge_output_collects_contexts_and_strictest_permission():
    result = HookRunResult()
    result.merge_output(HookOutput(permission_decision="allow"), HookEvent.PRE_TOOL_USE)
    result.merge_output(HookOutput(additional_context="A"), HookEvent.PRE_TOOL_USE)
    result.merge_output(
        HookOutput(permission_decision="ask", additional_context="B"),
        HookEvent.PRE_TOOL_USE,
    )
    assert result.additional_contexts == ["A", "B"]
    assert result.permission_override == "ask"  # deny > ask > allow

    result.merge_output(HookOutput(permission_decision="deny"), HookEvent.PRE_TOOL_USE)
    assert result.permission_override == "deny"


def test_merge_output_block_semantics_by_event():
    # PreToolUse 阻断 → block_requested
    r1 = HookRunResult()
    r1.merge_output(HookOutput(continue_=False, reason="危险"), HookEvent.PRE_TOOL_USE)
    assert r1.block_requested is True and r1.block_reason == "危险"

    # Stop 阻断 → continue_after_stop（阻止结束回合 = 续跑）
    r2 = HookRunResult()
    r2.merge_output(HookOutput(continue_=False, reason="没做完"), HookEvent.STOP)
    assert r2.continue_after_stop is True and r2.block_requested is False

    # SessionStart / PostToolUse 不支持阻断：请求被忽略
    r3 = HookRunResult()
    r3.merge_output(HookOutput(continue_=False), HookEvent.POST_TOOL_USE)
    assert r3.block_requested is False and r3.continue_after_stop is False

    # permission_override 只在 PreToolUse 收集
    r4 = HookRunResult()
    r4.merge_output(HookOutput(permission_decision="deny"), HookEvent.STOP)
    assert r4.permission_override is None

    # reason 取第一个非空值（先到先得，不覆盖）
    r5 = HookRunResult()
    r5.merge_output(HookOutput(continue_=False, reason="first"), HookEvent.PRE_TOOL_USE)
    r5.merge_output(HookOutput(continue_=False, reason="second"), HookEvent.PRE_TOOL_USE)
    assert r5.block_reason == "first"
