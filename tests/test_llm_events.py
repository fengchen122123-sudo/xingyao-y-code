from copy import deepcopy
from types import SimpleNamespace
import threading
import time
import pytest

from core.llm import (
    _complete_thinking_blocks,
    _tool_event_payload,
    _tool_finished_event_payload,
    _tool_output_preview,
    chat_with_model_events,
)


def _chunk(content="", tool_calls=None, reasoning="", provider_fields=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        provider_specific_fields=provider_fields,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta)],
        usage=None,
    )


def _tool_call(index, call_id, name, arguments):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_tool_start_payload_has_structured_display():
    payload = _tool_event_payload({
        "id": "cmd-start",
        "function": {
            "name": "execute_command",
            "arguments": '{"command":"pytest -q"}',
        },
    })

    assert payload["display"]["category"] == "command"
    assert payload["display"]["command"] == "pytest -q"


def test_tool_finish_payload_has_duration_result_and_display():
    tc = {
        "id": "read-1",
        "function": {
            "name": "read_file",
            "arguments": '{"file_path":"core/llm.py"}',
        },
    }
    result = {
        "role": "tool",
        "tool_call_id": "read-1",
        "name": "read_file",
        "content": "one\ntwo",
    }

    payload = _tool_finished_event_payload(tc, result, duration_ms=125)

    assert payload["duration_ms"] == 125
    assert payload["display"]["metrics"] == {"line_count": 2}
    assert payload["ok"] is True
    assert payload["result"] == "one\ntwo"


def test_repaired_arguments_are_shared_by_event_payload_and_executor(mocker):
    import core.llm as llm

    tc = {
        "id": "command-repaired",
        "function": {
            "name": "execute_command",
            "arguments": '{"command":"echo first\necho second"}',
        },
    }
    captured = {}

    class FakeToolResult:
        def to_message_content(self):
            return "done"

    def fake_execute(name, **arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return FakeToolResult()

    mocker.patch.object(llm, "check_permission_and_ask", return_value=True)
    mocker.patch.object(llm.tool_registry, "execute", side_effect=fake_execute)

    payload = llm._tool_event_payload(tc)
    llm._execute_single_tool_call(tc, event_callback=lambda *_args: None)

    assert payload["arguments"]["command"] == "echo first\necho second"
    assert payload["display"]["command"] == "echo first echo second"
    assert captured == {
        "name": "execute_command",
        "arguments": {"command": "echo first\necho second"},
    }


def test_tool_event_payload_rejects_unparseable_arguments():
    malformed_arguments = '{"items": [{"id": "a", "stage": "" Received Messages=[]'

    with pytest.raises(ValueError, match="工具参数 JSON 解析失败"):
        _tool_event_payload({
            "id": "bad-event-arguments",
            "function": {
                "name": "plan_write",
                "arguments": malformed_arguments,
            },
        })


def _takeover_scheduler_for_turn_tests(mocker):
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "跨回合接管"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item(
        "item", "subagent-1234abcd", "subagent:implement", None
    )
    assert scheduler.handoff_to_main(
        "item", "subagent-1234abcd", "old-turn", "foreground timeout"
    ) is not None
    assert scheduler.release_takeover_for_turn(
        "old-turn", cancelled=False, reason="派发回合已结束"
    ) is not None
    mocker.patch("core.task_scheduler.get_default_scheduler", return_value=scheduler)
    return plan, scheduler


def test_main_turn_claims_takeover_before_provider_and_requeues_on_normal_end(mocker):
    plan, _scheduler = _takeover_scheduler_for_turn_tests(mocker)
    state_at_request = []

    def completion(**_kwargs):
        state_at_request.append(deepcopy(plan.snapshot()[0]))
        return iter([_chunk("done")])

    mocker.patch("core.llm.completion", side_effect=completion)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "继续接手"}],
        persist=False,
        turn_id="new-turn",
        parent_session="session",
        parent_turn_id="new-turn",
    ))

    assert state_at_request[0]["status"] == "in_progress"
    assert state_at_request[0]["execution_mode"] == "main_takeover"
    assert state_at_request[0]["takeover_turn_id"] == "new-turn"
    released = plan.snapshot()[0]
    assert released["status"] == "pending"
    assert released["execution_mode"] == "takeover_queued"
    assert released["takeover_turn_id"] == ""
    kinds = [event.kind for event in events]
    assert kinds.index("plan_item_handoff") < kinds.index("turn_finished")
    assert kinds.count("plan_updated") >= 2
    handoff = next(event for event in events if event.kind == "plan_item_handoff")
    assert handoff.payload["item"]["id"] == "item"
    assert handoff.payload["mode"] == "main_takeover"
    assert handoff.payload["queue_position"] == 0
    assert handoff.payload["source_task_id"] == "subagent-1234abcd"


def test_cancelled_main_turn_cancels_claimed_takeover(mocker):
    plan, _scheduler = _takeover_scheduler_for_turn_tests(mocker)
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(KeyboardInterrupt):
        list(chat_with_model_events(
            "fake-model",
            "fake-key",
            [{"role": "user", "content": "取消接手"}],
            persist=False,
            turn_id="cancel-turn",
            parent_session="session",
            parent_turn_id="cancel-turn",
            cancel_event=cancel_event,
        ))

    cancelled = plan.snapshot()[0]
    assert cancelled["status"] == "cancelled"
    assert cancelled["execution_mode"] == ""
    assert cancelled["takeover_required"] is True


def _install_mid_turn_takeover(mocker, *, cancel=False):
    """让子 Agent 在当前主回合已开始后才超时移交。"""
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "回合中途移交"}])
    scheduler = TaskScheduler(plan)
    mocker.patch("core.task_scheduler.get_default_scheduler", return_value=scheduler)

    def fake_generator(*_args, **_kwargs):
        assert scheduler.start_delegated_item(
            "item", "subagent-1234abcd", "subagent:implement", None
        )
        assert scheduler.handoff_to_main(
            "item", "subagent-1234abcd", "mid-turn", "foreground timeout"
        ) is not None
        if cancel:
            raise KeyboardInterrupt("用户取消")
        yield "done"

    mocker.patch("core.llm._chat_with_model_generator", side_effect=fake_generator)
    return plan


def test_normal_end_requeues_takeover_created_mid_turn(mocker):
    """回合中途产生的接管租约也必须在自然结束时释放。"""
    plan = _install_mid_turn_takeover(mocker)

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "执行并结束"}],
        persist=False,
        turn_id="mid-turn",
        parent_session="session",
        parent_turn_id="mid-turn",
    ))

    released = plan.snapshot()[0]
    assert released["status"] == "pending"
    assert released["execution_mode"] == "takeover_queued"
    assert released["takeover_turn_id"] == ""


def test_cancelled_turn_cancels_takeover_created_mid_turn(mocker):
    """用户取消不得遗留 active 接管项给下一回合自动复活。"""
    plan = _install_mid_turn_takeover(mocker, cancel=True)

    with pytest.raises(KeyboardInterrupt):
        list(chat_with_model_events(
            "fake-model",
            "fake-key",
            [{"role": "user", "content": "执行后取消"}],
            persist=False,
            turn_id="mid-turn",
            parent_session="session",
            parent_turn_id="mid-turn",
        ))

    cancelled = plan.snapshot()[0]
    assert cancelled["status"] == "cancelled"
    assert cancelled["execution_mode"] == ""
    assert cancelled["takeover_required"] is True


def test_subagent_turn_never_claims_main_takeover(mocker):
    plan, _scheduler = _takeover_scheduler_for_turn_tests(mocker)
    mocker.patch("core.llm.completion", return_value=iter([_chunk("done")]))

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "子代理自己的回合"}],
        persist=False,
        turn_id="child-turn",
        parent_session="session",
        parent_turn_id="child-turn",
        allow_subagents=False,
    ))

    item = plan.snapshot()[0]
    assert item["status"] == "pending"
    assert item["execution_mode"] == "takeover_queued"


def test_completed_takeover_is_not_released_back_to_queue(mocker):
    plan, scheduler = _takeover_scheduler_for_turn_tests(mocker)

    def completion(**_kwargs):
        active = scheduler.active_takeover("finish-turn")
        assert active is not None
        assert scheduler.complete(active["id"], "verified") is True
        return iter([_chunk("done")])

    mocker.patch("core.llm.completion", side_effect=completion)

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "完成接手"}],
        persist=False,
        turn_id="finish-turn",
        parent_session="session",
        parent_turn_id="finish-turn",
    ))

    item = plan.snapshot()[0]
    assert item["status"] == "completed"
    assert item["execution_mode"] == "main_takeover"
    assert item["takeover_required"] is True


def test_parallel_tool_finished_events_keep_each_call_duration(mocker):
    first_response = iter([_chunk(tool_calls=[
        _tool_call(0, "call-slow", "glob_files", '{"pattern":"*.py"}'),
        _tool_call(1, "call-fast", "glob_files", '{"pattern":"*.md"}'),
    ])])
    final_response = iter([_chunk("done")])
    mocker.patch("core.llm.completion", side_effect=[first_response, final_response])

    def fake_execute(tc, **_kwargs):
        if tc["id"] == "call-slow":
            time.sleep(0.08)
        return {
            "role": "tool",
            "tool_call_id": tc["id"],
            "name": tc["function"]["name"],
            "content": tc["id"],
        }

    mocker.patch("core.llm._execute_single_tool_call", side_effect=fake_execute)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "run tools"}],
        persist=False,
        turn_id="turn-duration-per-call",
    ))
    finished = {
        event.payload["id"]: event.payload
        for event in events
        if event.kind == "tool_finished"
    }

    assert finished["call-slow"]["duration_ms"] >= 60
    assert finished["call-fast"]["duration_ms"] < 50
    assert finished["call-slow"]["duration_ms"] > finished["call-fast"]["duration_ms"]


def test_empty_tool_call_ids_get_unique_fallbacks(mocker):
    first_response = iter([_chunk(tool_calls=[
        _tool_call(0, "", "glob_files", '{"pattern":"*.py"}'),
        _tool_call(1, "", "glob_files", '{"pattern":"*.md"}'),
    ])])
    final_response = iter([_chunk("done")])
    mocker.patch("core.llm.completion", side_effect=[first_response, final_response])

    def fake_execute(tc, **_kwargs):
        return {
            "role": "tool",
            "tool_call_id": tc["id"],
            "name": tc["function"]["name"],
            "content": "ok",
        }

    mocker.patch("core.llm._execute_single_tool_call", side_effect=fake_execute)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "inspect"}],
        persist=False,
        turn_id="turn-empty-ids",
    ))

    assert [
        event.payload["id"] for event in events
        if event.kind == "tool_requested"
    ] == ["call_0", "call_1"]
    assert [
        event.payload["id"] for event in events
        if event.kind == "tool_finished"
    ] == ["call_0", "call_1"]


def test_event_stream_orders_parallel_read_tools(mocker):
    first_response = iter([
        _chunk(
            "先查看文件。",
            [
                _tool_call(0, "call-1", "glob_files", '{"pattern":"*.py"}'),
                _tool_call(1, "call-2", "glob_files", '{"pattern":"*.md"}'),
            ],
        )
    ])
    final_response = iter([_chunk("检查完成。")])
    mocker.patch("core.llm.completion", side_effect=[first_response, final_response])

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "检查项目"}],
        persist=False,
        turn_id="turn-test",
    ))

    kinds = [event.kind for event in events]
    requested = [
        event.payload["id"]
        for event in events
        if event.kind == "tool_requested"
    ]
    finished = [
        event.payload["id"]
        for event in events
        if event.kind == "tool_finished"
    ]

    assert requested == ["call-1", "call-2"]
    assert finished == ["call-1", "call-2"]
    assert kinds.index("tool_requested") < kinds.index("tool_finished")
    assert kinds[-1] == "turn_finished"
    assert all(event.payload["summary"] for event in events if event.kind == "tool_finished")


def test_event_stream_marks_real_reasoning_boundaries(mocker):
    mocker.patch("core.llm.completion", return_value=iter([
        _chunk(reasoning="private reasoning"),
        _chunk("结论"),
    ]))

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "分析"}],
        persist=False,
        turn_id="turn-reasoning",
    ))

    kinds = [event.kind for event in events]
    assert kinds.index("thinking_started") < kinds.index("thinking_finished")
    assert kinds.index("thinking_finished") < kinds.index("turn_finished")


def test_event_stream_finishes_reasoning_before_tool_execution(mocker):
    first_response = iter([
        _chunk(
            reasoning="先分析调用哪个工具",
            tool_calls=[
                _tool_call(0, "call-files", "glob_files", '{"pattern":"*.py"}'),
            ],
        ),
    ])
    final_response = iter([_chunk("查询完成")])
    mocker.patch("core.llm.completion", side_effect=[first_response, final_response])

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "查询"}],
        persist=False,
        turn_id="turn-reasoning-tool",
    ))

    kinds = [event.kind for event in events]
    assert kinds.index("thinking_started") < kinds.index("thinking_finished")
    executing_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "status" and event.payload.get("state") == "executing"
    )
    assert kinds.index("thinking_finished") < executing_index
    assert kinds.index("thinking_finished") < kinds.index("tool_started")
    assert [event.sequence for event in events] == sorted(
        event.sequence for event in events
    )


def test_event_stream_emits_reasoning_only_as_thinking_delta(mocker):
    mocker.patch("core.llm.completion", return_value=iter([
        _chunk(reasoning="分析过程"),
        _chunk("最终回答"),
    ]))

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "分析"}],
        persist=False,
        turn_id="turn-separated-reasoning",
    ))

    thinking = "".join(
        event.payload["text"] for event in events if event.kind == "thinking_delta"
    )
    answer = "".join(
        event.payload["text"] for event in events if event.kind == "assistant_delta"
    )
    assert thinking == "分析过程"
    assert answer == "最终回答"
    assert "分析过程" not in answer
    assert "<think>" not in answer


def test_kimi_k3_request_uses_anthropic_high_thinking_budget(mocker):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "anthropic/K3",
        "fake-key",
        [{"role": "user", "content": "回答"}],
        "https://api.kimi.com/coding",
        persist=False,
        turn_id="turn-k3-options",
    ))

    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert captured["max_tokens"] == 131072
    # litellm 客户端参数校验只把 Claude 系列识别为支持 thinking 的 anthropic 模型，
    # 对 "K3" 这类陌生模型名会直接抛 UnsupportedParamsError（请求根本发不出去），
    # 必须显式声明 allowed_openai_params 豁免校验。
    assert captured["allowed_openai_params"] == ["thinking"]
    assert "reasoning_effort" not in captured


def test_chat_request_uses_explicit_cache_scope_and_actual_tool_contract(mocker):
    """实际聊天入口必须把角色、父会话和最终 tools schema 纳入缓存通道。"""
    from core.llm import _provider_request_options, _tool_contract_fingerprint

    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "openai/kimi-for-coding",
        "fake-key",
        [{"role": "user", "content": "回答"}],
        "https://api.kimi.com/coding/v1",
        persist=False,
        turn_id="turn-cache-lane",
        cache_scope="subagent:test",
        cache_session_id="captured-parent-session",
    ))

    expected = _provider_request_options(
        "openai/kimi-for-coding",
        "https://api.kimi.com/coding/v1",
        cache_scope="subagent:test",
        cache_session_id="captured-parent-session",
        tool_contract=_tool_contract_fingerprint(captured["tools"]),
    )["extra_body"]["prompt_cache_key"]
    assert captured["extra_body"]["prompt_cache_key"] == expected


def test_kimi_k3_generic_anthropic_route_also_bypasses_litellm_param_check(mocker):
    """非 kimi coding 的 anthropic 兼容端点（经销商网关等）同样必须带豁免参数。"""
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "anthropic/K3",
        "fake-key",
        [{"role": "user", "content": "回答"}],
        "https://anthropic-proxy.example.com",
        persist=False,
        turn_id="turn-k3-generic-anthropic",
    ))

    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert captured["max_tokens"] == 131072
    assert captured["allowed_openai_params"] == ["thinking"]


def test_non_kimi_provider_does_not_receive_kimi_reasoning_options(mocker):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "deepseek/deepseek-chat",
        "fake-key",
        [{"role": "user", "content": "回答"}],
        "https://api.deepseek.com",
        persist=False,
        turn_id="turn-deepseek-options",
    ))

    assert "reasoning_effort" not in captured
    assert "thinking" not in captured
    assert "allowed_openai_params" not in captured


def test_tool_round_preserves_signed_thinking_blocks_without_think_text(mocker):
    signed = [{"type": "thinking", "thinking": "分析过程", "signature": "sig"}]
    requests = []
    responses = iter([
        iter([
            _chunk(reasoning="分析"),
            _chunk(reasoning="过程"),
            _chunk(provider_fields={"thinking_blocks": signed}),
            _chunk(tool_calls=[
                _tool_call(0, "files", "glob_files", '{"pattern":"*.missing"}'),
            ]),
        ]),
        iter([_chunk("最终回答")]),
    ])

    def fake_completion(**kwargs):
        requests.append(deepcopy(kwargs["messages"]))
        return next(responses)

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "anthropic/K3",
        "fake-key",
        [{"role": "user", "content": "检查"}],
        "https://api.kimi.com/coding",
        persist=False,
        turn_id="turn-signed-reasoning",
    ))

    assistant = next(
        message
        for message in requests[1]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert assistant["content"] in (None, "")
    assert assistant["reasoning_content"] == "分析过程"
    assert assistant["thinking_blocks"] == signed
    assert "<think>" not in str(assistant["content"])


def test_incomplete_redacted_thinking_block_is_not_round_tripped():
    invalid = SimpleNamespace(
        provider_specific_fields={
            "thinking_blocks": [{"type": "redacted_thinking"}],
        }
    )
    valid = SimpleNamespace(
        provider_specific_fields={
            "thinking_blocks": [
                {"type": "redacted_thinking", "data": "encrypted"},
            ],
        }
    )

    assert _complete_thinking_blocks(invalid) is None
    assert _complete_thinking_blocks(valid) == [
        {"type": "redacted_thinking", "data": "encrypted"},
    ]


def test_literal_split_think_tags_become_reasoning_events(mocker):
    mocker.patch("core.llm.completion", return_value=iter([
        _chunk("<thi"),
        _chunk("nk>分析</think>结论"),
    ]))

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "检查"}],
        persist=False,
        turn_id="turn-tagged-reasoning",
    ))

    thinking = "".join(
        event.payload["text"] for event in events if event.kind == "thinking_delta"
    )
    answer = "".join(
        event.payload["text"] for event in events if event.kind == "assistant_delta"
    )
    assert thinking == "分析"
    assert answer == "结论"


def test_read_file_preview_only_reports_path_and_line_count():
    preview = _tool_output_preview(
        "read_file",
        {"file_path": "core/llm.py"},
        {"content": "first line\nsecond line\nthird line"},
    )

    assert preview == "星瑶已查看文件：core/llm.py（3 行）"
    assert "first line" not in preview


def test_tool_result_error_prefix_is_not_reported_as_success():
    from core.llm import _tool_result_ok

    assert _tool_result_ok("get_weather", {"content": "Tool execution error: missing city"}) is False
    assert _tool_result_ok("ask_user", {"content": "无法提问：选项数量错误。"}) is False
    assert _tool_result_ok(
        "ask_user",
        {"content": '{"status":"blocked","error":"计划项尚未就绪"}'},
    ) is False
    assert _tool_result_ok(
        "plan_update",
        {"content": "计划运行状态由真实调度器管理，不能通过 plan_update 伪造。"},
    ) is False
    assert _tool_result_ok(
        "plan_write",
        {"content": "计划执行约束：不能重建已有执行记录。"},
    ) is False
    assert _tool_result_ok(
        "plan_write",
        {"content": "计划配置错误：verification_type=command 必须填写可执行命令。"},
    ) is False


def test_repeat_blocked_result_commits_journal_error_status():
    # 重复调用拦截结果（🚨 前缀）必须被识别为失败：
    # llm.py 提交路径用 _tool_result_ok 判定 ok，
    # build_tool_result_event_draft 据此写 result_status="error"。
    from core.harness.tool_pipeline import (
        REPEAT_CALL_BLOCKED_TEXT,
        ToolExecutionRecord,
        build_tool_result_event_draft,
    )
    from core.llm import _tool_result_ok

    message = {
        "role": "tool",
        "tool_call_id": "kill-third",
        "name": "manage_task",
        "content": REPEAT_CALL_BLOCKED_TEXT,
    }
    ok = _tool_result_ok("manage_task", message)
    assert ok is False

    tool_call = {
        "id": "kill-third",
        "function": {
            "name": "manage_task",
            "arguments": '{"action":"kill","task_id":"t1"}',
        },
    }
    draft = build_tool_result_event_draft(
        "step-1",
        tool_call,
        ToolExecutionRecord(call_ordinal=0, message=message, duration_ms=0),
        message_index=1,
        ok=ok,
    )
    assert draft.result_status == "error"


def test_generator_exception_does_not_leak_raw_error_as_public_answer(mocker):
    mocker.patch(
        "core.llm.completion",
        side_effect=RuntimeError("secret provider traceback"),
    )

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "执行检查"}],
        persist=False,
        consume_background_notifications=False,
    ))

    public_text = "".join(
        str(event.payload.get("text", ""))
        for event in events
        if event.kind == "assistant_delta"
    )
    error_events = [event for event in events if event.kind == "error"]
    assert "secret provider traceback" not in public_text
    assert error_events
    assert "secret provider traceback" in error_events[0].payload["message"]


def test_api_retry_status_does_not_leak_raw_error(mocker):
    mocker.patch(
        "core.llm.completion",
        side_effect=RuntimeError("secret provider traceback"),
    )
    mocker.patch("time.sleep")

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "执行检查"}],
        persist=False,
        consume_background_notifications=False,
    ))

    status_text = "".join(
        str(event.payload.get("message", ""))
        for event in events
        if event.kind == "status"
    )
    assert "secret provider traceback" not in status_text
    assert "1/5" in status_text
    assert "2/5" in status_text


def test_ask_user_plan_item_completes_after_real_selection(monkeypatch):
    import core.ask_user as ask_user_module
    from core.plan import bind_default_plan_session, get_default_plan_manager
    import core.llm as llm

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("ask-user-plan-test")
    plan.replace([{"id": "city", "subject": "确认城市", "kind": "input_preparation"}])
    monkeypatch.setattr(
        ask_user_module,
        "ask_user",
        lambda *args, **kwargs: '{"selected":"北京"}',
    )

    result = llm._execute_single_tool_call(
        {
            "id": "ask-city",
            "function": {
                "name": "ask_user",
                "arguments": '{"question":"城市？","options":["北京"],"plan_item_id":"city"}',
            },
        },
        event_callback=lambda *args, **kwargs: None,
        parent_session="ask-user-plan-test",
        parent_turn_id="turn-ask-city",
    )

    assert "北京" in result["content"]
    assert plan.snapshot()[0]["status"] == "completed"
    bind_default_plan_session(previous_session)


def test_invalid_ask_user_arguments_do_not_fail_the_plan_item():
    from core.plan import bind_default_plan_session, get_default_plan_manager
    import core.llm as llm

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("ask-user-invalid-options-test")
    plan.replace([{"id": "city", "subject": "确认城市"}])

    result = llm._execute_single_tool_call(
        {
            "id": "ask-city",
            "function": {
                "name": "ask_user",
                "arguments": (
                    '{"question":"城市？","options":'
                    '["北京","上海","广州","深圳"],"plan_item_id":"city"}'
                ),
            },
        },
        event_callback=lambda *args, **kwargs: None,
        parent_session="ask-user-invalid-options-test",
        parent_turn_id="turn-ask-city",
    )

    assert "选项数量必须在 1 到 3 个之间" in result["content"]
    assert plan.snapshot()[0]["status"] == "pending"
    assert plan.snapshot()[0]["attempts"] == 0
    bind_default_plan_session(previous_session)


def test_ask_user_cannot_complete_an_execution_plan_item():
    from core.plan import bind_default_plan_session, get_default_plan_manager
    import core.llm as llm

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("ask-user-execution-item-test")
    plan.replace([{"id": "weather", "subject": "查询天气"}])

    result = llm._execute_single_tool_call(
        {
            "id": "ask-city",
            "function": {
                "name": "ask_user",
                "arguments": '{"question":"城市？","options":["北京"],"plan_item_id":"weather"}',
            },
        },
        event_callback=lambda *args, **kwargs: None,
        parent_session="ask-user-execution-item-test",
        parent_turn_id="turn-ask-city",
    )

    assert "不是 input_preparation" in result["content"]
    assert plan.snapshot()[0]["status"] == "pending"
    bind_default_plan_session(previous_session)


def test_root_plan_turn_rejects_direct_tool_bypass(mocker):
    from core.plan import bind_default_plan_session, get_default_plan_manager
    import core.llm as llm

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("root-plan-tool-bypass-test")
    plan.replace([{"id": "weather", "subject": "查询天气"}])
    mocker.patch("core.llm.check_permission_and_ask", return_value=True)

    llm._execute_single_tool_call(
        {
            "id": "write-plan",
            "function": {
                "name": "plan_write",
                "arguments": '{"items":[{"id":"weather","subject":"查询天气","status":"pending"}]}',
            },
        },
        event_callback=lambda *args, **kwargs: None,
        parent_session="root-plan-tool-bypass-test",
        parent_turn_id="turn-plan-bypass",
    )
    from core.task_scheduler import get_default_scheduler

    scheduler = get_default_scheduler()
    assert scheduler.start_item("weather") is True
    assert scheduler.fail("weather", "测试失败") is True
    # 使用 write_file 测试计划锁：只读工具已被放行，而写文件仍必须走 run_subagent
    result = llm._execute_single_tool_call(
        {
            "id": "write-direct",
            "function": {
                "name": "write_file",
                "arguments": '{"file_path": "/tmp/x.txt", "content": "x"}',
            },
        },
        event_callback=lambda *args, **kwargs: None,
        parent_session="root-plan-tool-bypass-test",
        parent_turn_id="turn-plan-bypass",
    )

    assert "必须通过 run_subagent" in result["content"]
    assert llm._tool_result_ok("write_file", result) is False
    bind_default_plan_session(previous_session)


def test_root_plan_turn_cannot_rebuild_after_execution_started(mocker):
    from core.plan import bind_default_plan_session, get_default_plan_manager
    from core.task_scheduler import get_default_scheduler
    import core.llm as llm

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("plan-rebuild-guard-test")
    plan.replace([{"id": "weather", "subject": "查询天气"}])
    mocker.patch("core.llm.check_permission_and_ask", return_value=True)

    first = llm._execute_single_tool_call(
        {
            "id": "write-plan-1",
            "function": {
                "name": "plan_write",
                "arguments": '{"items":[{"id":"weather","subject":"查询天气","status":"pending"}]}',
            },
        },
        event_callback=lambda *args, **kwargs: None,
        parent_session="plan-rebuild-guard-test",
        parent_turn_id="turn-plan-rebuild",
    )
    assert first["content"] == "计划已更新。"
    scheduler = get_default_scheduler()
    assert scheduler.start_item("weather") is True
    assert scheduler.fail("weather", "网络失败") is True

    second = llm._execute_single_tool_call(
        {
            "id": "write-plan-2",
            "function": {
                "name": "plan_write",
                "arguments": '{"items":[{"id":"weather","subject":"查询天气","status":"pending"}]}',
            },
        },
        event_callback=lambda *args, **kwargs: None,
        parent_session="plan-rebuild-guard-test",
        parent_turn_id="turn-plan-rebuild",
    )

    assert "不能重建已有执行记录" in second["content"]
    assert plan.snapshot()[0]["status"] == "failed"
    bind_default_plan_session(previous_session)


def test_existing_pending_plan_rejects_direct_root_tool(mocker):
    from core.plan import bind_default_plan_session, get_default_plan_manager
    import core.llm as llm

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("existing-pending-plan-test")
    plan.replace([{"id": "weather", "subject": "查询天气"}])
    execute = mocker.patch.object(llm.tool_registry, "execute")

    result = llm._execute_single_tool_call(
        {
            "id": "write-direct",
            "function": {
                "name": "write_file",
                "arguments": '{"file_path": "/tmp/x.txt", "content": "x"}',
            },
        },
        event_callback=lambda *args, **kwargs: None,
        parent_session="existing-pending-plan-test",
        parent_turn_id="turn-existing-plan",
    )

    assert "必须通过 run_subagent" in result["content"]
    execute.assert_not_called()
    bind_default_plan_session(previous_session)


@pytest.mark.parametrize(
    ("function_name", "arguments"),
    [
        ("run_subagent", '{"task_description":"查询天气"}'),
        ("ask_user", '{"question":"城市？","options":["北京"]}'),
        ("mcp_demo", '{}'),
    ],
)
def test_plan_turn_requires_bound_orchestrators_and_blocks_other_tools(
    function_name,
    arguments,
):
    from core.plan import bind_default_plan_session, get_default_plan_manager
    import core.llm as llm

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session(f"plan-bound-{function_name}-test")
    plan.replace([{"id": "weather", "subject": "查询天气"}])

    result = llm._execute_single_tool_call(
        {
            "id": f"call-{function_name}",
            "function": {"name": function_name, "arguments": arguments},
        },
        event_callback=lambda *args, **kwargs: None,
        parent_session=f"plan-bound-{function_name}-test",
        parent_turn_id=f"turn-{function_name}",
    )

    if function_name in {"run_subagent", "ask_user"}:
        assert "必须填写 plan_item_id" in result["content"]
    else:
        assert "必须通过 run_subagent" in result["content"]
    bind_default_plan_session(previous_session)


def test_plan_turn_allows_manage_task_passthrough():
    """回归：计划激活时 manage_task（后台任务监控）必须放行，不被计划门禁拦截。

    模型需要用 manage_task 查看/管理后台子代理任务；kill 操作有独立的用户确认，
    拦截它只会让模型在计划期间对后台任务失明。
    """
    from core.plan import bind_default_plan_session, get_default_plan_manager
    import core.llm as llm

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("plan-bound-manage-task-pass-test")
    plan.replace([{"id": "weather", "subject": "查询天气"}])

    try:
        result = llm._execute_single_tool_call(
            {
                "id": "call-manage-task-pass",
                "function": {"name": "manage_task", "arguments": '{"action":"list"}'},
            },
            event_callback=lambda *args, **kwargs: None,
            parent_session="plan-bound-manage-task-pass-test",
            parent_turn_id="turn-manage-task-pass",
        )
    finally:
        bind_default_plan_session(previous_session)

    assert "必须通过 run_subagent" not in result["content"]


def test_parallel_subagents_overlap_and_results_keep_call_order(mocker):
    barrier = threading.Barrier(2)
    completed = []

    def fake_run(task_description, **context):
        barrier.wait(timeout=1)
        completed.append(task_description)
        return f"结果:{task_description}"

    run = mocker.patch("core.llm.run_subagent", side_effect=fake_run)
    first_response = iter([
        _chunk(tool_calls=[
            _tool_call(
                0,
                "weather",
                "run_subagent",
                '{"task_description":"天气"}',
            ),
            _tool_call(
                1,
                "time",
                "run_subagent",
                '{"task_description":"时间"}',
            ),
        ])
    ])
    final_response = iter([_chunk("汇总：天气和时间")])
    mocker.patch(
        "core.llm.completion",
        side_effect=[first_response, final_response],
    )
    messages = [{"role": "user", "content": "并行查询并汇总"}]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-parallel-subagents",
        parent_session="session-1",
        parent_turn_id="turn-parallel-subagents",
        consume_background_notifications=False,
    ))

    assert run.call_count == 2
    assert sorted(completed) == ["天气", "时间"]
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["weather", "time"]
    assert [message["content"] for message in tool_messages] == ["结果:天气", "结果:时间"]
    answer = "".join(
        event.payload.get("text", "")
        for event in events
        if event.kind == "assistant_delta"
    )
    assert answer == "汇总：天气和时间"


def test_subagent_start_events_are_visible_while_children_are_running(mocker):
    children_started = threading.Barrier(3)
    release = threading.Event()
    collected = []
    errors = []

    def fake_run(task_description, **context):
        children_started.wait(timeout=1)
        release.wait(1)
        return f"结果:{task_description}"

    mocker.patch("core.llm.run_subagent", side_effect=fake_run)
    mocker.patch("core.llm.completion", side_effect=[
        iter([_chunk(tool_calls=[
            _tool_call(0, "one", "run_subagent", '{"task_description":"一"}'),
            _tool_call(1, "two", "run_subagent", '{"task_description":"二"}'),
        ])]),
        iter([_chunk("汇总完成")]),
    ])

    def consume():
        try:
            collected.extend(chat_with_model_events(
                "fake-model",
                "fake-key",
                [{"role": "user", "content": "并行"}],
                persist=False,
                turn_id="turn-live-starts",
                parent_session="session-1",
                consume_background_notifications=False,
            ))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=consume)
    thread.start()
    children_started.wait(timeout=1)
    try:
        started_ids = [
            event.payload["id"]
            for event in collected
            if event.kind == "tool_started"
        ]
        assert started_ids == ["one", "two"]
    finally:
        release.set()
        thread.join(timeout=2)

    assert thread.is_alive() is False
    assert errors == []


def test_child_schema_excludes_run_subagent(mocker):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [],
        persist=False,
        allow_subagents=False,
        consume_background_notifications=False,
    ))

    names = {item["function"]["name"] for item in captured["tools"]}
    assert "run_subagent" not in names
    assert "plan_write" not in names
    assert "plan_update" not in names


def test_root_with_pending_plan_keeps_full_tools_for_cache_stability(mocker):
    """计划进行中不再裁剪 tools：计划约束由执行层 plan_enforced 拦截统一保证。
    tools 列表逐字节稳定是 DeepSeek/Kimi 前缀缓存命中的前提，
    计划创建↔完成之间切换工具集会让缓存从头部整体失效。"""
    from core.plan import bind_default_plan_session, get_default_plan_manager

    captured = {}

    def fake_completion(**kwargs):
        captured["tools"] = kwargs["tools"]
        return iter([_chunk("执行中")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)
    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("root-pending-schema-test")
    plan.replace([{"id": "weather", "subject": "查询天气", "status": "pending"}])

    try:
        list(chat_with_model_events(
            "fake-model",
            "fake-key",
            [{"role": "user", "content": "继续执行计划"}],
            persist=False,
            allow_subagents=True,
            parent_session="root-pending-schema-test",
            turn_id="root-pending-schema-turn",
            consume_background_notifications=False,
        ))
    finally:
        bind_default_plan_session(previous_session)

    names = {item["function"]["name"] for item in captured["tools"]}
    # 计划控制工具与只读探索工具必须仍在（供主 Agent 推进计划）
    assert {
        "plan_write", "plan_update", "plan_read", "plan_retry", "run_subagent", "ask_user",
        "read_file", "list_directory", "search_code", "glob_files", "find_definition",
        "fetch_url", "search_news", "get_weather", "manage_task",
    } <= names
    # 写工具也必须保留：请求形状跨轮稳定优先，越权调用由执行层拦截
    assert "write_file" in names


def test_child_does_not_drain_root_background_notifications(mocker):
    drain = mocker.patch("core.tools.subagent_manager.drain_notifications")
    mocker.patch("core.llm.completion", return_value=iter([_chunk("完成")]))

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [],
        persist=False,
        allow_subagents=False,
        consume_background_notifications=False,
    ))

    drain.assert_not_called()


def test_root_drains_only_its_session_background_notifications(mocker):
    drain = mocker.patch(
        "core.tools.subagent_manager.drain_notifications",
        return_value=[],
    )
    mocker.patch("core.llm.completion", return_value=iter([_chunk("完成")]))

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [],
        persist=False,
        parent_session="session-owned",
    ))

    drain.assert_called_once_with("session-owned")


def test_persistence_uses_captured_parent_session_not_current_session(mocker, monkeypatch):
    import main

    persisted = []

    class Manager:
        @staticmethod
        def add_message(session, role, message):
            persisted.append(("single", session, role, message))

        @staticmethod
        def add_messages(session, messages):
            persisted.extend(
                ("batch", session, role, message)
                for role, message in messages
            )

    monkeypatch.setattr(main, "manager", Manager())
    monkeypatch.setattr(main, "current_session", "session-switched")
    mocker.patch("core.llm.completion", return_value=iter([_chunk("最终回答")]))

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "回答"}],
        persist=True,
        turn_id="turn-owned-persistence",
        parent_session="session-owned",
    ))

    assert [(kind, session, role) for kind, session, role, _ in persisted] == [
        ("batch", "session-owned", "assistant")
    ]


def test_completed_tool_turn_persists_one_ordered_batch(mocker):
    persisted = []

    class Manager:
        @staticmethod
        def add_message(session, role, message):
            persisted.append(("single", session, role, message))

        @staticmethod
        def add_messages(session, messages):
            persisted.extend(
                ("batch", session, role, message)
                for role, message in messages
            )

    # 直接 patch 生产持久化依赖边界，绝不依赖 main/gui 入口的导入副作用
    mocker.patch("core.app_context.get_manager", return_value=Manager())
    mocker.patch("core.llm.completion", side_effect=[
        iter([_chunk(tool_calls=[
            _tool_call(0, "read", "read_file", '{"file_path":"x.py"}'),
        ])]),
        iter([_chunk("完成")]),
    ])
    mocker.patch("core.llm.read_file", return_value="content")

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "读取"}],
        persist=True,
        turn_id="turn-batch-persistence",
        parent_session="session-owned",
    ))

    assert [(kind, session, role) for kind, session, role, _ in persisted] == [
        ("batch", "session-owned", "assistant"),
        ("batch", "session-owned", "tool"),
        ("batch", "session-owned", "assistant"),
    ]


def test_cancelled_tool_turn_salvages_completed_messages(mocker):
    """取消回合时，已产生的 assistant 消息必须打捞落库，悬空的 tool_calls
    组补取消占位结果（否则重开会话看不到终止前进度，且下一轮 API 会 400）。"""
    persisted = []
    cancel_event = threading.Event()

    class Manager:
        @staticmethod
        def add_message(session, role, message):
            persisted.append((session, role, message))

        @staticmethod
        def add_messages(session, messages):
            persisted.append((session, "batch", list(messages)))

    def cancel_during_tool(*args, **kwargs):
        cancel_event.set()
        raise KeyboardInterrupt("cancel tool")

    # 直接 patch 生产持久化依赖边界，绝不依赖 main/gui 入口的导入副作用
    mocker.patch("core.app_context.get_manager", return_value=Manager())
    mocker.patch("core.llm._execute_single_tool_call", side_effect=cancel_during_tool)
    mocker.patch("core.llm.completion", return_value=iter([
        _chunk(tool_calls=[
            _tool_call(0, "read", "read_file", '{"file_path":"x.py"}'),
        ])
    ]))

    try:
        list(chat_with_model_events(
            "fake-model",
            "fake-key",
            [{"role": "user", "content": "读取"}],
            persist=True,
            turn_id="turn-cancel-persistence",
            cancel_event=cancel_event,
            parent_session="session-owned",
        ))
    except KeyboardInterrupt:
        pass

    # 一笔打捞 batch：assistant（带 tool_calls）+ 合成的取消占位 tool 结果
    batches = [p for p in persisted if p[1] == "batch"]
    assert len(batches) == 1
    session, _kind, batch = batches[0]
    assert session == "session-owned"
    assert [role for role, _msg in batch] == ["assistant", "tool"]
    assistant_msg = batch[0][1]
    assert assistant_msg["tool_calls"][0]["id"] == "read"
    tool_msg = batch[1][1]
    assert tool_msg["tool_call_id"] == "read"
    assert tool_msg["name"] == "read_file"
    assert "终止" in tool_msg["content"]


def test_edit_file_on_missing_file_gives_clear_error(mocker):
    """回归：edit_file 对不存在的文件必须提示改用 write_file，
    不得误报"JSON 修复后不完整"（掩盖真实原因，曾导致子代理白白失败）。"""
    import core.llm as llm

    mocker.patch("core.llm.check_permission_and_ask", return_value=True)

    result = llm._execute_single_tool_call(
        {
            "id": "edit-missing",
            "function": {
                "name": "edit_file",
                "arguments": (
                    '{"file_path": "mytemp/definitely-not-exists-xyz.py",'
                    ' "target_content": "a", "replacement_content": "b"}'
                ),
            },
        },
        event_callback=lambda *args, **kwargs: None,
        parent_session="edit-missing-file-test",
        parent_turn_id="turn-edit-missing",
    )

    assert "目标文件不存在" in result["content"]
    assert "write_file" in result["content"]
    assert "JSON 修复后不完整" not in result["content"]


def test_stalled_plan_after_repeated_failures_allows_direct_diagnosis(mocker):
    """回归：计划项多次失败全部停滞后，只读/查询类诊断命令仍可直接执行，
    主 Agent 不被计划锁逼入死局（修改类工具改由超时接管状态机放行）。"""
    from core.plan import bind_default_plan_session, get_default_plan_manager
    from core.task_scheduler import get_default_scheduler
    import core.llm as llm

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("stalled-plan-escape-test")
    plan.replace([{"id": "impl", "subject": "实现功能", "stage": "build"}])
    mocker.patch("core.llm.check_permission_and_ask", return_value=True)
    execute = mocker.patch.object(
        llm.tool_registry, "execute", return_value=mocker.Mock(
            to_message_content=lambda: "写入成功"
        )
    )

    scheduler = get_default_scheduler()
    # 模拟两次执行失败：failed -> retry -> failed（attempts=2，计划停滞）
    assert scheduler.start_item("impl") is True
    assert scheduler.fail("impl", "第一次失败") is True
    assert scheduler.retry("impl") is True
    assert scheduler.start_item("impl") is True
    assert scheduler.fail("impl", "第二次失败") is True

    result = llm._execute_single_tool_call(
        {
            "id": "exec-direct",
            "function": {
                "name": "execute_command",
                "arguments": '{"command": "git status"}',
            },
        },
        event_callback=lambda *args, **kwargs: None,
        parent_session="stalled-plan-escape-test",
        parent_turn_id="turn-stalled-escape",
    )

    assert "必须通过 run_subagent" not in result["content"]
    execute.assert_called_once()
    bind_default_plan_session(previous_session)


def test_cleared_plan_turn_releases_tool_gate(mocker):
    """回归：计划在同一回合内被清空后，执行工具门禁必须解除。

    plan_write 成功会把回合标记为计划回合；若模型随后清空计划（snapshot 为空），
    门禁不能再以“必须通过 run_subagent(plan_item_id=...)”拦截主 Agent，
    否则没有计划项可提供 plan_item_id，主 Agent 被锁死（GUI 实测死锁）。
    """
    from core.plan import bind_default_plan_session, get_default_plan_manager
    import core.llm as llm

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("cleared-plan-gate-test")
    plan.replace([])
    llm._mark_plan_turn("turn-cleared-gate")
    mocker.patch("core.llm.check_permission_and_ask", return_value=True)
    execute = mocker.patch.object(
        llm.tool_registry, "execute", return_value=mocker.Mock(
            to_message_content=lambda: "执行成功"
        )
    )
    try:
        result = llm._execute_single_tool_call(
            {
                "id": "exec-direct",
                "function": {
                    "name": "execute_command",
                    "arguments": '{"command": "echo hi"}',
                },
            },
            event_callback=lambda *args, **kwargs: None,
            parent_session="cleared-plan-gate-test",
            parent_turn_id="turn-cleared-gate",
        )

        assert "必须通过 run_subagent" not in result["content"]
        execute.assert_called_once()
    finally:
        bind_default_plan_session(previous_session)


def test_completed_plan_turn_releases_tool_gate(mocker):
    """回归：计划项全部 completed 后，主 Agent 必须能直接接手执行。

    子代理可能在产物未落盘时错误结束计划项；如果终态计划仍维持工具门禁，
    主 Agent 会被锁在无法重试、无法重建、也无法直接修复的死循环里。
    """
    from core.plan import bind_default_plan_session, get_default_plan_manager
    import core.llm as llm

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("terminal-plan-gate-test")
    plan.replace([{
        "id": "impl",
        "subject": "实现功能",
        "status": "completed",
    }])
    llm._mark_plan_turn("turn-terminal-gate")
    mocker.patch("core.llm.check_permission_and_ask", return_value=True)
    execute = mocker.patch.object(
        llm.tool_registry, "execute", return_value=mocker.Mock(
            to_message_content=lambda: "执行成功"
        )
    )
    try:
        result = llm._execute_single_tool_call(
            {
                "id": "exec-direct",
                "function": {
                    "name": "execute_command",
                    "arguments": '{"command": "echo hi"}',
                },
            },
            event_callback=lambda *args, **kwargs: None,
            parent_session="terminal-plan-gate-test",
            parent_turn_id="turn-terminal-gate",
        )

        assert "必须通过 run_subagent" not in result["content"]
        execute.assert_called_once()
    finally:
        bind_default_plan_session(previous_session)


@pytest.mark.parametrize("statuses", [
    ["failed"],
    ["blocked"],
    ["cancelled"],
    ["completed", "failed"],
])
def test_unresolved_plan_keeps_tool_gate_across_turns(mocker, statuses):
    """回归：新回合仍须恢复未解决计划，不能直接绕过失败类终态。"""
    from core.plan import bind_default_plan_session, get_default_plan_manager
    import core.llm as llm

    session = "unresolved-plan-gate-" + "-".join(statuses)
    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session(session)
    plan.replace([
        {
            "id": f"item-{index}",
            "subject": f"任务 {index}",
            "status": status,
        }
        for index, status in enumerate(statuses)
    ])
    mocker.patch("core.llm.check_permission_and_ask", return_value=True)
    execute = mocker.patch.object(
        llm.tool_registry, "execute", return_value=mocker.Mock(
            to_message_content=lambda: "不应执行"
        )
    )
    try:
        result = llm._execute_single_tool_call(
            {
                "id": "exec-direct",
                "function": {
                    "name": "execute_command",
                    "arguments": '{"command": "echo hi"}',
                },
            },
            event_callback=lambda *args, **kwargs: None,
            parent_session=session,
            parent_turn_id="turn-new-unmarked",
        )

        assert "必须通过 run_subagent" in result["content"]
        execute.assert_not_called()
    finally:
        bind_default_plan_session(previous_session)


def test_plan_retry_repeat_calls_are_not_suppressed(mocker):
    """回归：plan_retry 等计划状态机工具不受重复调用抑制。

    plan_retry 的设计流程是“首次返回根因分析提示、再次调用强制重置”；
    若被 _RepeatCallGuard 以“相同调用已成功 2 次”拦截，调度器会把计划项
    锁死在 failed 态（run_subagent 要求先 plan_retry，plan_retry 又被守卫拦）。
    """
    from core.plan import bind_default_plan_session, get_default_plan_manager
    import core.llm as llm

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("plan-retry-repeat-guard-test")
    plan.replace([{"id": "t1", "subject": "任务"}])
    mocker.patch("core.llm.check_permission_and_ask", return_value=True)
    execute = mocker.patch.object(
        llm.tool_registry, "execute",
        return_value=mocker.Mock(to_message_content=lambda: "计划项已重置为 pending。"),
    )
    responses = [
        iter([_chunk(tool_calls=[
            _tool_call(0, f"retry-{i}", "plan_retry", '{"item_id":"t1"}')
        ])])
        for i in range(3)
    ]
    responses.append(iter([_chunk("完成")]))
    mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "重试计划项"}]
    try:
        list(chat_with_model_events(
            "fake-model",
            "fake-key",
            messages,
            persist=False,
            turn_id="turn-plan-retry-repeat",
            parent_session="plan-retry-repeat-guard-test",
            parent_turn_id="turn-plan-retry-repeat",
            consume_background_notifications=False,
        ))
    finally:
        bind_default_plan_session(previous_session)

    assert execute.call_count == 3
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert all("🛑" not in m["content"] for m in tool_msgs)


def test_unparseable_tool_arguments_return_error_without_executing():
    """当 tool_call arguments 是完全无法解析的畸形 JSON 时，应直接返回解析错误，
    而不是用空参数继续执行工具。"""
    from core.llm import _execute_single_tool_call

    malformed_arguments = '{"items": [{"id": "a", "stage": "" Received Messages=[]'
    result = _execute_single_tool_call(
        {
            "id": "call-bad-json",
            "function": {
                "name": "plan_write",
                "arguments": malformed_arguments,
            },
        },
        event_callback=lambda *args, **kwargs: None,
    )

    assert result["role"] == "tool"
    assert result["tool_call_id"] == "call-bad-json"
    assert result["name"] == "plan_write"
    assert "JSON 解析失败" in result["content"]


def test_execute_command_preview_is_truncated_in_tool_event_payload():
    """execute_command 的长命令 preview 应该被截断，避免 UI 刷屏。"""
    from core.llm import _tool_event_payload

    long_command = "powershell -Command " + "x" * 200
    payload = _tool_event_payload({
        "id": "call-long-cmd",
        "function": {
            "name": "execute_command",
            "arguments": f'{{"command": "{long_command}"}}',
        },
    })

    preview = payload["preview"]
    assert len(preview) <= 40
    assert preview.endswith("...")
    assert long_command.startswith(preview.replace("...", ""))


def test_summarize_execute_command_started_truncates_long_command():
    """summarize_tool_started 对 execute_command 的显示应截断到 30 字符。"""
    from core.tool_presenter import summarize_tool_started

    long_command = "powershell -Command " + "x" * 200
    summary = summarize_tool_started("execute_command", {"command": long_command})

    assert "星瑶正在执行命令" in summary
    assert "..." in summary
    assert len(summary) <= len("星瑶正在执行命令：") + 30


_NO_PROGRESS_MESSAGE = (
    "🚨 连续 12 轮没有检测到后台任务、计划或工具结果状态变化，"
    "回合已终止，避免继续循环消耗。"
)


def _progress_record(call_id, name, content, ordinal=0):
    from core.harness.tool_pipeline import ToolExecutionRecord

    return ToolExecutionRecord(
        call_ordinal=ordinal,
        message={
            "role": "tool",
            "tool_call_id": call_id,
            "name": name,
            "content": content,
        },
        duration_ms=0,
    )


def test_no_progress_abort_emits_fixed_user_visible_error(mocker):
    request_count = 0

    def next_tool_stream(**_kwargs):
        nonlocal request_count
        request_count += 1
        return iter([_chunk(tool_calls=[
            _tool_call(
                0,
                f"call-{request_count}",
                "manage_task",
                '{"action":"status","task_id":"t1"}',
            ),
        ])])

    def fixed_tool_result(tc, **_kwargs):
        return {
            "role": "tool",
            "tool_call_id": tc["id"],
            "name": tc["function"]["name"],
            "content": '{"task_id":"t1","status":"running"}',
        }

    mocker.patch("core.llm.completion", side_effect=next_tool_stream)
    mocker.patch("core.llm._execute_single_tool_call", side_effect=fixed_tool_result)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "盯着后台任务"}],
        persist=False,
        turn_id="turn-no-progress",
    ))

    error_events = [event for event in events if event.kind == "error"]
    assert error_events, "无进展熔断必须产生用户可见 error 事件"
    assert error_events[0].payload["message"] == _NO_PROGRESS_MESSAGE
    # 去重回归：终止文案只走 error/turn_error 事件通道（GUI 横幅 / CLI 红行），
    # 不得再以 assistant_delta 正文形式渲染，否则对话流出现两遍相同文案。
    public_text = "".join(
        str(event.payload.get("text", ""))
        for event in events
        if event.kind == "assistant_delta"
    )
    assert _NO_PROGRESS_MESSAGE not in public_text
    # 有界读取进展预算：首次 manage_task 新读取签名算一次进展，此后同一签名
    # 重复调用不再算，熔断由第 12 次请求推迟到第 13 次（12 步停滞 + 1 步读取进展）
    assert request_count == 13


def test_no_progress_abort_works_without_harness_runtime(mocker):
    # legacy chat_with_model（无 Harness）场景：guard 照常观察、门禁照常生效，
    # 终止消息写入历史并作为回合回复返回。
    from core.llm import chat_with_model

    request_count = 0

    def next_tool_stream(**_kwargs):
        nonlocal request_count
        request_count += 1
        return iter([_chunk(tool_calls=[
            _tool_call(
                0,
                f"call-{request_count}",
                "manage_task",
                '{"action":"status","task_id":"t1"}',
            ),
        ])])

    def fixed_tool_result(tc, **_kwargs):
        return {
            "role": "tool",
            "tool_call_id": tc["id"],
            "name": tc["function"]["name"],
            "content": '{"task_id":"t1","status":"running"}',
        }

    mocker.patch("core.llm.completion", side_effect=next_tool_stream)
    mocker.patch("core.llm._execute_single_tool_call", side_effect=fixed_tool_result)

    messages = [{"role": "user", "content": "盯着后台任务"}]
    reply = chat_with_model("fake-model", "fake-key", messages)

    assert reply == _NO_PROGRESS_MESSAGE
    # 同上：首次新读取签名消耗一格读取预算，熔断推迟到第 13 次请求
    assert request_count == 13
    assert messages[-1] == {"role": "assistant", "content": _NO_PROGRESS_MESSAGE}


def test_step_progress_observation_ignores_call_ids_and_plan_placeholder():
    import hashlib

    from core.llm import _step_progress_observation

    records_a = [_progress_record("call-1", "write_file", "写入完成")]
    records_b = [_progress_record("call-xyz", "write_file", "写入完成")]

    obs_a = _step_progress_observation(records_a, {})
    obs_b = _step_progress_observation(records_b, {})

    # 只改变工具 call id 不算进展：结果哈希必须一致
    assert obs_a.tool_results_sha256 == obs_b.tool_results_sha256
    # 计划快照为真实计划指纹：不传 session 时走默认路由，同状态两次调用稳定一致
    from core.plan import plan_state_fingerprint

    assert obs_a.plan_snapshot_sha256 == plan_state_fingerprint(None)
    assert obs_a.plan_snapshot_sha256 == obs_b.plan_snapshot_sha256


def test_step_progress_observation_skips_observation_tools_and_blocked_results():
    from core.harness.tool_pipeline import REPEAT_CALL_BLOCKED_TEXT
    from core.llm import _step_progress_observation

    baseline = _step_progress_observation(None, None)

    # 观察类工具（manage_task 等）结果不纳入结果哈希
    obs = _step_progress_observation(
        [_progress_record("c1", "manage_task", '{"status":"running"}')],
        {},
    )
    assert obs.tool_results_sha256 == baseline.tool_results_sha256
    assert obs.mutation_committed is False

    # RepeatCallGuard 拦截结果跳过：不纳入哈希、不算 mutation
    obs_blocked = _step_progress_observation(
        [_progress_record("c2", "write_file", REPEAT_CALL_BLOCKED_TEXT)],
        {},
    )
    assert obs_blocked.tool_results_sha256 == baseline.tool_results_sha256
    assert obs_blocked.mutation_committed is False

    # 非观察类工具结果纳入结果哈希
    obs_written = _step_progress_observation(
        [_progress_record("c3", "write_file", "写入完成")],
        {},
    )
    assert obs_written.tool_results_sha256 != baseline.tool_results_sha256


def test_step_progress_observation_mutation_rules():
    from core.llm import _step_progress_observation

    # write_file/edit_file 结果 ok → mutation
    obs = _step_progress_observation(
        [_progress_record("c1", "write_file", "写入完成")],
        {},
    )
    assert obs.mutation_committed is True

    # 失败结果不算 mutation
    obs_fail = _step_progress_observation(
        [_progress_record("c2", "edit_file", "🚨 文件不存在")],
        {},
    )
    assert obs_fail.mutation_committed is False

    # execute_command 查询类命令（QUERY）不算 mutation
    query_calls = {
        "c3": {
            "id": "c3",
            "function": {
                "name": "execute_command",
                "arguments": '{"command":"ls"}',
            },
        },
    }
    obs_query = _step_progress_observation(
        [_progress_record("c3", "execute_command", "ok")],
        query_calls,
    )
    assert obs_query.mutation_committed is False

    # execute_command 修改类命令且结果 ok → mutation
    mutation_calls = {
        "c4": {
            "id": "c4",
            "function": {
                "name": "execute_command",
                "arguments": '{"command":"mkdir progress_guard_tmp"}',
            },
        },
    }
    obs_mutation = _step_progress_observation(
        [_progress_record("c4", "execute_command", "ok")],
        mutation_calls,
    )
    assert obs_mutation.mutation_committed is True


def test_different_subagent_task_ids_do_not_reset_stall():
    """子代理动态 task_id 不得把超时移交结果当成业务进展。"""
    import json

    from core.llm import _step_progress_observation

    first = _step_progress_observation([
        _progress_record("c1", "run_subagent", json.dumps({
            "status": "timed_out", "handoff": "main_takeover",
            "task_id": "subagent-a", "plan_item_id": "item",
        }))
    ])
    second = _step_progress_observation([
        _progress_record("c2", "run_subagent", json.dumps({
            "status": "timed_out", "handoff": "main_takeover",
            "task_id": "subagent-b", "plan_item_id": "item",
        }))
    ])
    assert first.tool_results_sha256 == second.tool_results_sha256


def test_unplanned_subagent_progress_uses_arguments_but_ignores_dynamic_task_id():
    """无计划子代理的不同工作算进展；同一工作只换 task_id 不算。"""
    import json

    from core.llm import _step_progress_observation

    def observe(call_id, task_description, task_id):
        calls = {
            call_id: {
                "id": call_id,
                "function": {
                    "name": "run_subagent",
                    "arguments": json.dumps({
                        "task_description": task_description,
                        "subagent_type": "explore",
                    }),
                },
            },
        }
        return _step_progress_observation([
            _progress_record(call_id, "run_subagent", json.dumps({
                "status": "completed",
                "task_id": task_id,
                "result": "核查完成",
                "execution_elapsed": 1.25,
            }, ensure_ascii=False))
        ], calls)

    first = observe("c1", "核查模块 A", "subagent-11111111")
    same_work = observe("c2", "核查模块 A", "subagent-22222222")
    different_work = observe("c3", "核查模块 B", "subagent-33333333")

    assert first.tool_results_sha256 == same_work.tool_results_sha256
    assert first.tool_results_sha256 != different_work.tool_results_sha256


def test_plan_bound_subagent_result_is_delegated_to_plan_fingerprint():
    """计划绑定子代理的参数和动态结果都不绕过计划状态指纹。"""
    import json

    from core.llm import _step_progress_observation

    def observe(call_id, description, task_id):
        calls = {
            call_id: {
                "id": call_id,
                "function": {
                    "name": "run_subagent",
                    "arguments": json.dumps({
                        "task_description": description,
                        "plan_item_id": "item",
                    }),
                },
            },
        }
        return _step_progress_observation([
            _progress_record(call_id, "run_subagent", json.dumps({
                "status": "timed_out",
                "task_id": task_id,
                "plan_item_id": "item",
                "handoff": "main_takeover",
            }))
        ], calls)

    first = observe("c1", "第一次描述", "subagent-11111111")
    second = observe("c2", "第二次描述", "subagent-22222222")
    assert first.tool_results_sha256 == second.tool_results_sha256


def test_plan_state_tools_do_not_contribute_result_progress():
    """所有计划控制工具只依据 canonical 计划快照计进展，正文不算。"""
    from core.llm import _step_progress_observation

    observations = [
        _step_progress_observation([
            _progress_record(f"c{index}", name, content)
        ])
        for index, (name, content) in enumerate([
            ("plan_retry", "无法重置 completed 项"),
            ("plan_read", "计划共 1 项: completed"),
            ("plan_update", "计划项已更新"),
            ("plan_write", "不能重建已有执行记录"),
            ("plan_resume_takeover", "不是超时接管项"),
            ("plan_finish", "不是当前回合接管项"),
            ("ask_user", '{"selected": ["继续执行"], "cancelled": false}'),
        ], start=1)
    ]

    assert len({item.tool_results_sha256 for item in observations}) == 1


def test_alternating_plan_control_failures_abort_after_twelve_stalled_steps():
    """回归：切换计划工具和返回文案不能伪造进展、绕过无进展熔断。"""
    from core.harness.progress_guard import TurnProgressGuard
    from core.llm import _step_progress_observation

    guard = TurnProgressGuard()
    cycle = (
        ("plan_update", "计划项已更新"),
        ("plan_retry", "无法重置 completed 项"),
        ("plan_finish", "不是当前回合接管项"),
    )
    for index in range(11):
        name, content = cycle[index % len(cycle)]
        progressed = guard.observe(_step_progress_observation([
            _progress_record(f"cycle-{index}", name, content)
        ]))
        assert progressed is False
        assert guard.should_abort() is False

    name, content = cycle[11 % len(cycle)]
    assert guard.observe(_step_progress_observation([
        _progress_record("cycle-11", name, content)
    ])) is False
    assert guard.should_abort() is True
