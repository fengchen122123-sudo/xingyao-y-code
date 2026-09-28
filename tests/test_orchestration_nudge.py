"""编排提醒（多步未建计划）行为守护：触发、阈值、豁免与去重。"""

import json
from types import SimpleNamespace

import core.llm as llm
from core.llm import chat_with_model_events

NUDGE_PREFIX = "本回合已连续执行多步操作，仍未建立任务计划"


def _chunk(content="", tool_calls=None, reasoning=""):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        provider_specific_fields=None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _tool_call(index, call_id, name, arguments):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _write_calls(batch, count):
    return [
        _tool_call(
            i,
            f"{batch}-{i}",
            "write_file",
            json.dumps({"file_path": f"{batch}-{i}.txt", "content": "x"}),
        )
        for i in range(count)
    ]


def _fake_execute(tool_call, **_kwargs):
    return {
        "role": "tool",
        "tool_call_id": tool_call["id"],
        "name": tool_call["function"]["name"],
        "content": "ok",
    }


def _run(mocker, messages, responses, **kwargs):
    mocker.patch("core.llm.completion", side_effect=responses)
    mocker.patch("core.llm._execute_single_tool_call", side_effect=_fake_execute)
    return list(
        chat_with_model_events(
            "fake-model",
            "fake-key",
            messages,
            persist=False,
            turn_id=kwargs.pop("turn_id", "turn-orch-nudge"),
            consume_background_notifications=False,
            **kwargs,
        )
    )


def _nudges(messages):
    return [
        m
        for m in messages
        if m.get("role") == "user"
        and str(m.get("content", "")).startswith(NUDGE_PREFIX)
    ]


def _bind_empty_plan(session_id):
    from core.plan import bind_default_plan_session, get_default_plan_manager

    plan = get_default_plan_manager()
    previous = plan.session_id
    bind_default_plan_session(session_id)
    plan.replace([])
    return previous


def _restore_plan(previous):
    from core.plan import bind_default_plan_session

    bind_default_plan_session(previous)


def test_count_substantive_ops_classifies_by_name_and_risk(mocker):
    from core.permission_policy import PermissionRisk

    mocker.patch(
        "core.llm.classify_command_risk",
        side_effect=lambda command: (
            PermissionRisk.QUERY
            if "status" in command
            else PermissionRisk.SENSITIVE_MUTATION
        ),
    )
    tool_calls = [
        {"function": {"name": "write_file", "arguments": "{}"}},
        {"function": {"name": "edit_file", "arguments": "{}"}},
        {"function": {"name": "read_file", "arguments": "{}"}},
        {
            "function": {
                "name": "execute_command",
                "arguments": json.dumps({"command": "git status"}),
            }
        },
        {
            "function": {
                "name": "execute_command",
                "arguments": json.dumps({"command": "mkdir out"}),
            }
        },
        {"function": {"name": "execute_command", "arguments": "{broken json"}},
    ]
    assert llm._count_substantive_ops(tool_calls) == 4


def test_should_nudge_orchestration_conditions(mocker):
    mocker.patch("core.llm._has_pending_plan", return_value=False)
    mocker.patch("core.llm._is_plan_turn", return_value=False)

    base = dict(count=3, session_id="s", turn_id="t")
    assert (
        llm._should_nudge_orchestration(allow_subagents=True, already_sent=False, **base)
        is True
    )
    assert (
        llm._should_nudge_orchestration(allow_subagents=True, already_sent=True, **base)
        is False
    )
    assert (
        llm._should_nudge_orchestration(allow_subagents=False, already_sent=False, **base)
        is False
    )
    assert (
        llm._should_nudge_orchestration(
            allow_subagents=True, already_sent=False, count=2, session_id="s", turn_id="t"
        )
        is False
    )

    mocker.patch("core.llm._has_pending_plan", return_value=True)
    assert (
        llm._should_nudge_orchestration(allow_subagents=True, already_sent=False, **base)
        is False
    )

    mocker.patch("core.llm._has_pending_plan", return_value=False)
    mocker.patch("core.llm._is_plan_turn", return_value=True)
    assert (
        llm._should_nudge_orchestration(allow_subagents=True, already_sent=False, **base)
        is False
    )


def test_three_ops_without_plan_injects_single_nudge(mocker):
    previous = _bind_empty_plan("orch-nudge-test")
    try:
        responses = [
            iter([_chunk(tool_calls=_write_calls("b1", 2))]),
            iter([_chunk(tool_calls=_write_calls("b2", 1))]),
            iter([_chunk("检查结论：文件已处理。")]),
        ]
        messages = [{"role": "user", "content": "把三个文件都维护一下"}]
        _run(mocker, messages, responses, parent_session="orch-nudge-test")

        assert len(_nudges(messages)) == 1
    finally:
        _restore_plan(previous)


def test_five_ops_without_plan_still_injects_only_once(mocker):
    previous = _bind_empty_plan("orch-nudge-once-test")
    try:
        responses = [
            iter([_chunk(tool_calls=_write_calls("c1", 3))]),
            iter([_chunk(tool_calls=_write_calls("c2", 2))]),
            iter([_chunk("检查结论：文件已处理。")]),
        ]
        messages = [{"role": "user", "content": "继续维护剩下两个"}]
        _run(mocker, messages, responses, parent_session="orch-nudge-once-test")

        assert len(_nudges(messages)) == 1
    finally:
        _restore_plan(previous)


def test_two_ops_do_not_inject_nudge(mocker):
    previous = _bind_empty_plan("orch-no-nudge-test")
    try:
        responses = [
            iter([_chunk(tool_calls=_write_calls("d1", 2))]),
            iter([_chunk("检查结论：改动已处理。")]),
        ]
        messages = [{"role": "user", "content": "改两个文件"}]
        _run(mocker, messages, responses, parent_session="orch-no-nudge-test")

        assert _nudges(messages) == []
    finally:
        _restore_plan(previous)


def test_subagent_turn_does_not_inject_nudge(mocker):
    previous = _bind_empty_plan("orch-subagent-test")
    try:
        responses = [
            iter([_chunk(tool_calls=_write_calls("e1", 3))]),
            iter([_chunk("检查结论：改动已处理。")]),
        ]
        messages = [{"role": "user", "content": "批量改三个文件"}]
        _run(
            mocker,
            messages,
            responses,
            parent_session="orch-subagent-test",
            allow_subagents=False,
        )

        assert _nudges(messages) == []
    finally:
        _restore_plan(previous)
