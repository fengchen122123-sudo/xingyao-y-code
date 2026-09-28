"""编排提醒（多步未建计划）行为守护：触发、阈值、豁免与去重。"""

import json
from types import SimpleNamespace

import pytest

import core.llm as llm
from core.llm import chat_with_model_events

NUDGE_PREFIX = "本回合已连续执行多步操作，仍未建立任务计划"


@pytest.fixture(autouse=True)
def _reset_orchestration_turn_dedup():
    """每个用例前清空"按 turn_id 去重"登记表，保证用例互不干扰。

    实现前该属性不存在（getattr 返回 None），跳过清理；实现在 red→green
    之间补齐后即可生效。
    """
    registry = getattr(llm, "_orchestration_nudged_turns", None)
    if registry is not None:
        registry.clear()
    yield
    if registry is not None:
        registry.clear()


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

    # 阈值口径：实质操作数需超过 3（即 ≥4）才提醒，恰好 3 步不提醒。
    base = dict(count=4, session_id="s", turn_id="t")
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
    # 边界：恰好 3 步不提醒
    assert (
        llm._should_nudge_orchestration(
            allow_subagents=True, already_sent=False, count=3, session_id="s", turn_id="t"
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


def test_three_ops_without_plan_does_not_inject_nudge(mocker):
    """恰好 3 步不提醒（与文案"3 条及以内直接执行"一致）。"""
    previous = _bind_empty_plan("orch-nudge-test")
    try:
        responses = [
            iter([_chunk(tool_calls=_write_calls("b1", 2))]),
            iter([_chunk(tool_calls=_write_calls("b2", 1))]),
            iter([_chunk("检查结论：文件已处理。")]),
        ]
        messages = [{"role": "user", "content": "把三个文件都维护一下"}]
        _run(mocker, messages, responses, parent_session="orch-nudge-test")

        assert _nudges(messages) == []
    finally:
        _restore_plan(previous)


def test_four_ops_without_plan_injects_single_nudge(mocker):
    previous = _bind_empty_plan("orch-four-nudge-test")
    try:
        responses = [
            iter([_chunk(tool_calls=_write_calls("b1", 2))]),
            iter([_chunk(tool_calls=_write_calls("b2", 2))]),
            iter([_chunk("检查结论：文件已处理。")]),
        ]
        messages = [{"role": "user", "content": "把四个文件都维护一下"}]
        _run(mocker, messages, responses, parent_session="orch-four-nudge-test")

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
            iter([_chunk(tool_calls=_write_calls("e1", 4))]),
            iter([_chunk("检查结论：改动已处理。")]),
        ]
        messages = [{"role": "user", "content": "批量改四个文件"}]
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


def _assert_tool_pairing_valid(messages):
    """校验消息序列合法：assistant(tool_calls) 之后必须紧跟 tool 消息。"""
    for index, message in enumerate(messages):
        if message.get("role") == "assistant" and message.get("tool_calls"):
            assert index + 1 < len(messages), (
                "assistant(tool_calls) 之后缺少配对的 tool 消息"
            )
            assert messages[index + 1].get("role") == "tool", (
                "assistant(tool_calls) 之后必须是 tool 消息，实际为 "
                f"{messages[index + 1].get('role')}: "
                f"{messages[index + 1].get('content')!r}"
            )


def test_nudge_injected_after_tool_results(mocker):
    """编排提醒必须追加在全部 tool 结果之后（Issue 2）。

    否则会形成 assistant(tool_calls) → user(提醒) → tool(结果)，
    违反 Anthropic/OpenAI 的 tool_use 必须紧跟 tool_result 约束。
    """
    previous = _bind_empty_plan("orch-order-test")
    try:
        responses = [
            iter([_chunk(tool_calls=_write_calls("f1", 4))]),
            iter([_chunk("检查结论：完成。")]),
        ]
        messages = [{"role": "user", "content": "改四个文件"}]
        _run(
            mocker,
            messages,
            responses,
            parent_session="orch-order-test",
            turn_id="order-turn",
        )

        assert len(_nudges(messages)) == 1
        _assert_tool_pairing_valid(messages)
    finally:
        _restore_plan(previous)


def test_same_turn_id_not_nudged_twice(mocker):
    """同一 turn_id 异常重入时不得重复注入（进程内按 turn_id 去重，Issue 6）。"""
    previous = _bind_empty_plan("orch-turn-dedup")
    try:
        captured = []
        for _ in range(2):
            responses = [
                iter([_chunk(tool_calls=_write_calls("g1", 4))]),
                iter([_chunk("检查结论：完成。")]),
            ]
            messages = [{"role": "user", "content": "改四个文件"}]
            _run(
                mocker,
                messages,
                responses,
                parent_session="orch-turn-dedup",
                turn_id="shared-turn",
            )
            captured.append(messages)

        assert len(_nudges(captured[0])) == 1
        assert _nudges(captured[1]) == []
    finally:
        _restore_plan(previous)
