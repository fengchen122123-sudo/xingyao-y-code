from types import SimpleNamespace

from core.llm import chat_with_model_events

SLOW_NUDGE_MARKER = "禁止继续只输出思考内容"


def _chunk(content="", tool_calls=None, reasoning="", usage=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        provider_specific_fields=None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=usage)


def _slow_nudges(messages):
    return [
        m for m in messages
        if m.get("role") == "user" and SLOW_NUDGE_MARKER in str(m.get("content") or "")
    ]


def _text(events):
    return "".join(
        event.payload["text"] for event in events if event.kind == "assistant_delta"
    )


def test_normal_duration_step_does_not_nudge(mocker):
    import core.llm as llm

    assert llm.SLOW_STEP_ABORT_SECONDS == 60.0

    responses = [iter([_chunk("")])]
    completion = mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "开始"}]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-guard-fast",
        consume_background_notifications=False,
    ))

    assert completion.call_count == 1
    assert _slow_nudges(messages) == [], "正常耗时的空步不得触发慢步熔断催促"
    assert not any(
        event.kind == "notice" and "长时间无产出" in str(event.payload.get("message") or "")
        for event in events
    )


def test_slow_step_without_output_triggers_single_nudge(mocker, monkeypatch):
    import core.llm as llm

    monkeypatch.setattr(llm, "SLOW_STEP_ABORT_SECONDS", -1.0)

    responses = [
        iter([_chunk("")]),
        iter([_chunk("最终结论：任务已经完成。")]),
    ]
    completion = mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "开始"}]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-guard-nudge",
        consume_background_notifications=False,
    ))

    assert len(_slow_nudges(messages)) == 1
    assert completion.call_count == 2
    assert any(
        event.kind == "notice" and "长时间无产出" in str(event.payload.get("message") or "")
        for event in events
    )
    assert "最终结论" in _text(events)


def test_slow_step_aborts_after_nudge_limit_and_returns_content(mocker, monkeypatch):
    import core.llm as llm

    monkeypatch.setattr(llm, "SLOW_STEP_ABORT_SECONDS", -1.0)
    assert llm.SLOW_STEP_MAX_NUDGES == 1

    responses = [
        iter([_chunk("")]),
        iter([_chunk("", reasoning="仍在反复自检思考过程……")]),
        iter([_chunk("这一步不应被请求。")]),                    # 兜底：绝不应被消费
    ]
    completion = mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "开始"}]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-guard-abort",
        consume_background_notifications=False,
    ))

    # 上限 1：最多 1 次催促 + 1 次熔断结束，绝不发起第 3 次请求
    assert completion.call_count == 2
    assert len(_slow_nudges(messages)) == 1
    assert any(
        event.kind == "error" and "慢步熔断" in str(event.payload.get("message") or "")
        for event in events
    )
    assert "反复自检" in _text(events)


EXTRA_INSTRUCTION = "始终用中文回答，并在末尾给出一句下一步建议。"


def _stub_model_entry(monkeypatch, entry):
    import core.model_manager as model_manager

    monkeypatch.setattr(
        model_manager,
        "get_model_config_by_litellm_model",
        lambda *args, **kwargs: entry,
    )


def _silence_kairos_memory(monkeypatch):
    import core.memory_kairos as memory_kairos

    monkeypatch.setattr(
        memory_kairos, "build_memory_system_section", lambda *args, **kwargs: ""
    )


def _run_capture(mocker, model_name, messages, turn_id):
    captured = []

    def _fake_completion(**kwargs):
        captured.append(list(kwargs.get("messages") or []))
        return iter([_chunk("结论：任务已完成。")])

    mocker.patch("core.llm.completion", side_effect=_fake_completion)
    events = list(chat_with_model_events(
        model_name,
        "fake-key",
        messages,
        persist=False,
        turn_id=turn_id,
        consume_background_notifications=False,
    ))
    return captured, events


def test_model_system_prompt_extra_pure_function_is_defensive(monkeypatch):
    import core.llm as llm
    import core.model_manager as model_manager

    _stub_model_entry(monkeypatch, None)
    assert llm._model_system_prompt_extra("fake-model") == ""

    _stub_model_entry(monkeypatch, {"litellm_model": "fake-model"})
    assert llm._model_system_prompt_extra("fake-model") == ""

    _stub_model_entry(monkeypatch, {"system_prompt_extra": 123})
    assert llm._model_system_prompt_extra("fake-model") == ""

    _stub_model_entry(monkeypatch, {"system_prompt_extra": "   "})
    assert llm._model_system_prompt_extra("fake-model") == ""

    _stub_model_entry(monkeypatch, {"system_prompt_extra": EXTRA_INSTRUCTION})
    assert llm._model_system_prompt_extra("fake-model") == EXTRA_INSTRUCTION

    def _boom(*args, **kwargs):
        raise RuntimeError("配置查询爆炸")

    monkeypatch.setattr(model_manager, "get_model_config_by_litellm_model", _boom)
    assert llm._model_system_prompt_extra("fake-model") == ""


def test_model_without_system_prompt_extra_keeps_request_unchanged(mocker, monkeypatch):
    _silence_kairos_memory(monkeypatch)
    _stub_model_entry(monkeypatch, {"id": "mid-plain", "litellm_model": "fake-model"})
    messages = [{"role": "user", "content": "开始"}]

    captured, events = _run_capture(mocker, "fake-model", messages, "turn-extra-off")

    assert captured, "必须发起至少一次请求"
    assert captured[0] == [{"role": "user", "content": "开始"}]
    assert all(message.get("role") != "system" for message in captured[0])
    assert all(message.get("role") != "system" for message in messages)
    assert "结论：任务已完成。" in _text(events)


def test_configured_system_prompt_extra_is_injected_as_leading_system(mocker, monkeypatch):
    _silence_kairos_memory(monkeypatch)
    _stub_model_entry(monkeypatch, {
        "id": "mid-extra",
        "litellm_model": "fake-model",
        "system_prompt_extra": EXTRA_INSTRUCTION,
    })
    messages = [{"role": "user", "content": "开始"}]

    captured, events = _run_capture(mocker, "fake-model", messages, "turn-extra-on")

    assert captured, "必须发起至少一次请求"
    request_messages = captured[0]
    assert request_messages[0] == {"role": "system", "content": EXTRA_INSTRUCTION}
    assert request_messages[1:] == [{"role": "user", "content": "开始"}]
    assert len([m for m in request_messages if m.get("role") == "system"]) == 1
    assert all(message.get("role") != "system" for message in messages)
    assert messages[-1]["role"] == "assistant"
    assert "结论：任务已完成。" in _text(events)


def test_system_prompt_extra_is_byte_stable_across_consecutive_requests(mocker, monkeypatch):
    _silence_kairos_memory(monkeypatch)
    _stub_model_entry(monkeypatch, {
        "id": "mid-extra",
        "litellm_model": "fake-model",
        "system_prompt_extra": EXTRA_INSTRUCTION,
    })

    captured_first, _ = _run_capture(
        mocker, "fake-model", [{"role": "user", "content": "第一轮"}], "turn-extra-t1",
    )
    captured_second, _ = _run_capture(
        mocker, "fake-model", [{"role": "user", "content": "第二轮"}], "turn-extra-t2",
    )

    first_head = captured_first[0][0]
    second_head = captured_second[0][0]
    assert first_head == second_head == {"role": "system", "content": EXTRA_INSTRUCTION}
    assert first_head["content"].encode("utf-8") == second_head["content"].encode("utf-8")


NEOHORSE_GUARDRAIL = (
    "你是执行器：直接给出结果，禁止逐项自检、反复确认或复述思考过程。"
    "能用工具就用工具：每一步尽量落到一次工具调用，不要用长段文字描述你打算怎么做。"
    "任何验证结论（测试通过、功能正常等）必须附上你实际执行命令的原始输出；"
    "没有实际运行就不要声称已通过。"
    "最终答复保持简短，先说结论再给必要细节。"
)


def test_preset_catalog_carries_no_models():
    from core.model_catalog import get_preset_providers

    for provider in get_preset_providers():
        assert "models" not in provider


def test_preset_write_paths_carry_guardrail_only_when_declared():
    import core.model_manager as model_manager

    catalog_model = {
        "name": "guard-model",
        "context_window": 256000,
        "system_prompt_extra": NEOHORSE_GUARDRAIL,
    }

    created = {"id": "mid-new", "name": "guard-model", "context_window": 256000}
    model_manager._apply_preset_capability_fields(created, catalog_model)
    assert created["system_prompt_extra"] == NEOHORSE_GUARDRAIL

    plain_catalog = {"name": "plain-model", "context_window": 128000}
    plain_created = {"id": "mid-plain-1", "name": "plain-model", "context_window": 128000}
    model_manager._apply_preset_capability_fields(plain_created, plain_catalog)
    assert "system_prompt_extra" not in plain_created



def test_verify_claim_pure_function_matrix():
    from core.llm import _claims_verification_without_evidence as f

    assert f("代码已写入，测试已通过。", True, False) is True
    assert f("all tests passed", True, False) is True
    assert f("测试已通过", True, True) is False
    assert f("测试已通过", False, False) is False
    assert f("已确认代码没问题", True, False) is False
    assert f("", True, False) is False
    assert f(None, True, False) is False
    assert f("目前没有全部通过，还有 2 个用例失败。", True, False) is False
    assert f("是否已通过验证还不确定。", True, False) is False
    assert f("尚未通过", True, False) is False
    assert f("测试通过率 80%。", True, False) is False
    assert f("All Tests Passed", True, False) is True


def test_tool_call_activity_kinds_classification():
    from core.llm import _tool_call_activity_kinds as kinds

    assert kinds([{"function": {"name": "write_file"}},
                  {"function": {"name": "execute_command"}}]) == (True, True)
    assert kinds([{"function": {"name": "edit_file"}}]) == (True, False)
    assert kinds([{"function": {"name": "execute_command"}}]) == (False, True)
    assert kinds([{"function": {"name": "read_file"}}]) == (False, False)
    assert kinds([]) == (False, False)


def test_subagent_command_evidence_matrix():
    from core.llm_execution import _subagent_counts_as_command_evidence as f

    completed = '{"status": "completed", "result": "ok"}'
    # 命令型角色（test 跑测试 / operate 跑技能脚本与内网取证）+ completed 才算
    assert f("test", completed) is True
    assert f("operate", completed) is True
    assert f("OPERATE", completed) is True
    assert f(" operate ", completed) is True
    # 非命令型角色不能冒充「本回合执行过命令」的证据
    assert f("explore", completed) is False
    assert f("review", completed) is False
    assert f("general", completed) is False
    assert f("", completed) is False
    assert f(None, completed) is False
    # 非 completed / 非对象 / 解析失败都不算，绝不把模糊内容当成通过证据
    assert f("operate", '{"status": "failed"}') is False
    assert f("operate", '{"status": "running"}') is False
    assert f("operate", "not json") is False
    assert f("operate", "[]") is False
    assert f("operate", None) is False
    assert f("operate", "") is False


def test_verify_claim_nudge_injected_once_then_converges(mocker):
    mocker.patch("core.llm._claims_verification_without_evidence", return_value=True)

    responses = [
        iter([_chunk("代码已写入，测试已通过。")]),
        iter([_chunk("未验证，如实说明。")]),
    ]
    completion = mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "写个文件并确保测试通过"}]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-verify-claim",
        consume_background_notifications=False,
    ))

    nudges = [
        m for m in messages
        if m.get("role") == "user" and "没有执行任何命令" in str(m.get("content") or "")
    ]
    assert len(nudges) == 1, "应恰好注入一条验证声明提醒（上限 1 次）"
    assert completion.call_count == 2
    assert any(
        event.kind == "notice" and "验证声明" in str(event.payload.get("message") or "")
        for event in events
    )
