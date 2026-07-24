from copy import deepcopy
from types import SimpleNamespace
import threading
import pytest

from core.llm import (
    _complete_thinking_blocks,
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
    assert captured["max_tokens"] == 32000
    assert captured["allowed_openai_params"] == ["thinking"]
    assert "reasoning_effort" not in captured


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
    assert "1/3" in status_text
    assert "2/3" in status_text


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
        ("manage_task", '{"action":"list"}'),
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


def test_root_with_pending_plan_only_exposes_plan_control_tools(mocker):
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
    # 计划模式下暴露计划控制工具 + 只读探索工具
    assert names <= {
        "plan_write", "plan_update", "plan_read", "plan_retry", "run_subagent", "ask_user",
        "read_file", "list_directory", "search_code", "glob_files",
        "fetch_url", "search_news", "get_weather",
    }


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


def test_completed_tool_turn_persists_one_ordered_batch(mocker, monkeypatch):
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


def test_cancelled_tool_turn_does_not_persist_partial_tool_chain(mocker, monkeypatch):
    import main

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

    monkeypatch.setattr(main, "manager", Manager())
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

    assert persisted == []


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


def test_stalled_plan_after_repeated_failures_allows_direct_fix(mocker):
    """回归：计划项多次失败（attempts>=2）全部停滞后，主 Agent 可直接调用
    工具修复，不再被计划锁逼入死局。"""
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
                "arguments": '{"command": "echo hi"}',
            },
        },
        event_callback=lambda *args, **kwargs: None,
        parent_session="stalled-plan-escape-test",
        parent_turn_id="turn-stalled-escape",
    )

    assert "必须通过 run_subagent" not in result["content"]
    execute.assert_called_once()
    bind_default_plan_session(previous_session)


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
