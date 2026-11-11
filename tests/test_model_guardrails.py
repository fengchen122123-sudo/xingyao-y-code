"""慢步熔断护栏回归测试（防止推理型小模型思考失控、静默空转、零产出）。

覆盖三个场景：
(a) 正常耗时步骤不触发任何催促；
(b) 超时且无产出的步骤触发一次强催促；
(c) 超过催促上限后循环结束并返回已有内容。

全部通过 monkeypatch/mocker 构造假 provider 流来驱动，绝不发起真实网络调用。
触发阈值用 monkeypatch 调低（例如 -1.0），避免测试真的等待 30 秒；真实代码里的
判定仍读取模块级常量 SLOW_STEP_ABORT_SECONDS / SLOW_STEP_MAX_NUDGES。
"""

from types import SimpleNamespace

from core.llm import chat_with_model_events

# 慢步强催促的稳定文案片段（与实现中的催促 user 消息一致），用于在内存 messages
# 里定位熔断催促，避免与既有"只输出了思考过程"的终局催促混淆。
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
    """(a) 耗时正常（毫秒级，远低于默认 30s 阈值）的步骤，即便没有任何产出，
    也绝不能触发慢步熔断催促——证明熔断是按耗时门控的，而非按有无产出门控。"""
    import core.llm as llm

    # 不改阈值：默认 SLOW_STEP_ABORT_SECONDS = 60.0
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
    """(b) 单步耗时超阈值且既无正文也无工具调用：注入一次强催促，随后正常收尾。"""
    import core.llm as llm

    # 调低阈值使真实（毫秒级）耗时被判定为"慢步"，避免测试真的等待 30 秒。
    monkeypatch.setattr(llm, "SLOW_STEP_ABORT_SECONDS", -1.0)

    responses = [
        iter([_chunk("")]),                       # 慢步空转 → 触发一次强催促
        iter([_chunk("最终结论：任务已经完成。")]),  # 催促后正常给出结论
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

    # 恰好一次强催促，且催促后模型继续完成了一步请求
    assert len(_slow_nudges(messages)) == 1
    assert completion.call_count == 2
    assert any(
        event.kind == "notice" and "长时间无产出" in str(event.payload.get("message") or "")
        for event in events
    )
    assert "最终结论" in _text(events)


def test_slow_step_aborts_after_nudge_limit_and_returns_content(mocker, monkeypatch):
    """(c) 达到催促上限后再次慢步空转：结束循环、返回已产出内容，不再发起新请求。"""
    import core.llm as llm

    monkeypatch.setattr(llm, "SLOW_STEP_ABORT_SECONDS", -1.0)
    assert llm.SLOW_STEP_MAX_NUDGES == 1

    responses = [
        iter([_chunk("")]),                                   # 第 1 步：慢步空转 → 催促一次
        iter([_chunk("", reasoning="仍在反复自检思考过程……")]),  # 第 2 步：仍空转 → 达到上限，熔断
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
    # 熔断时把已产出的内容（促升的思考预览）返回，而不是静默空转
    assert "反复自检" in _text(events)


# ---------------------------------------------------------------------------
# 模型级系统提示扩展（system_prompt_extra，B 方案）
#
# 目标：配置了该字段的模型在请求时自动追加一段指令，其余模型完全不受影响。
# 这里通过 monkeypatch 模型配置查询返回假条目，绝不落盘、不改真实 config.json，
# 也绝不发起真实网络调用。
# ---------------------------------------------------------------------------

EXTRA_INSTRUCTION = "始终用中文回答，并在末尾给出一句下一步建议。"


def _stub_model_entry(monkeypatch, entry):
    """让 llm 侧的模型配置查询固定返回指定条目（None 表示查不到）。"""
    import core.model_manager as model_manager

    monkeypatch.setattr(
        model_manager,
        "get_model_config_by_litellm_model",
        lambda *args, **kwargs: entry,
    )


def _silence_kairos_memory(monkeypatch):
    """关掉 KAIROS 长期记忆注入：本组用例只关心 system_prompt_extra 这一条 system。

    记忆注入是另一条独立链路（测试环境里的记忆目录内容不稳定），关掉它之后请求
    消息形状在用例内才是逐字节确定的。
    """
    import core.memory_kairos as memory_kairos

    monkeypatch.setattr(
        memory_kairos, "build_memory_system_section", lambda *args, **kwargs: ""
    )


def _run_capture(mocker, model_name, messages, turn_id):
    """跑一轮真实 chat_with_model_events，抓取每次 Provider 请求收到的 messages。"""
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
    """纯函数层面：未配置/非字符串/空白/查询异常一律返回空串，配置值原样返回。"""
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
    """(a) 未配置该字段：请求 messages 与现状逐字节一致（零影响），也不污染调用方列表。"""
    _silence_kairos_memory(monkeypatch)
    _stub_model_entry(monkeypatch, {"id": "mid-plain", "litellm_model": "fake-model"})
    messages = [{"role": "user", "content": "开始"}]

    captured, events = _run_capture(mocker, "fake-model", messages, "turn-extra-off")

    assert captured, "必须发起至少一次请求"
    # 未配置扩展字段：请求就是调用方给的原样消息，不许多出任何 system 头部
    assert captured[0] == [{"role": "user", "content": "开始"}]
    assert all(message.get("role") != "system" for message in captured[0])
    # 调用方共享列表里也不得凭空多出一条 system 消息
    assert all(message.get("role") != "system" for message in messages)
    assert "结论：任务已完成。" in _text(events)


def test_configured_system_prompt_extra_is_injected_as_leading_system(mocker, monkeypatch):
    """(b) 配置后：请求头部多一条 role=system 且内容等于该字段，其余消息顺序不变。"""
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
    # 注入只作用于请求副本：调用方共享列表不得被写入这条 system 消息；但循环里
    # 追加的 assistant 回复仍按既有语义回写（非流式调用方依赖 messages[-1]）。
    assert all(message.get("role") != "system" for message in messages)
    assert messages[-1]["role"] == "assistant"
    assert "结论：任务已完成。" in _text(events)


def test_system_prompt_extra_is_byte_stable_across_consecutive_requests(mocker, monkeypatch):
    """(c) 连续两次请求注入内容逐字节一致（跨轮稳定），且等于配置值。"""
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


# ---------------------------------------------------------------------------
# 预置护栏（A 方案落地）：neohorse-1-9b 的实测护栏写进预置目录
#
# 护栏文本来自对该模型的真实实测（tmp/neohorse-1-9b-agent-report.md）：它是推理型
# 小模型，推理与正文共用 max_tokens，遇多约束自检任务会推理失控（2000 token 烧光、
# 正文为空、单步 46.9s）；加「禁止逐项自检」元指令后同题由 0/5 变 5/5、输出 token
# 2000→223。因此把它作为该模型的默认 system_prompt_extra 落到 core/model_catalog.py，
# 其余预置模型必须完全不受影响（不带该字段）。
# ---------------------------------------------------------------------------

NEOHORSE_GUARDRAIL = (
    "你是执行器：直接给出结果，禁止逐项自检、反复确认或复述思考过程。"
    "能用工具就用工具：每一步尽量落到一次工具调用，不要用长段文字描述你打算怎么做。"
    "任何验证结论（测试通过、功能正常等）必须附上你实际执行命令的原始输出；"
    "没有实际运行就不要声称已通过。"
    "最终答复保持简短，先说结论再给必要细节。"
)


def test_preset_catalog_carries_no_models():
    """预置目录只含厂商信息；neohorse 推理护栏等模型级元数据不再随目录下发，
    已配置用户不受影响（config.json 快照保留既有 system_prompt_extra）。"""
    from core.model_catalog import get_preset_providers

    for provider in get_preset_providers():
        assert "models" not in provider


def test_preset_write_paths_carry_guardrail_only_when_declared():
    """新增/刷新两条预置写入路径透传 system_prompt_extra；未声明的模型不落键。"""
    import core.model_manager as model_manager

    catalog_model = {
        "name": "guard-model",
        "context_window": 256000,
        "system_prompt_extra": NEOHORSE_GUARDRAIL,
    }

    # 新增路径（sync 追加缺失模型 / add_preset_provider_with_models 共用）
    created = {"id": "mid-new", "name": "guard-model", "context_window": 256000}
    model_manager._apply_preset_capability_fields(created, catalog_model)
    assert created["system_prompt_extra"] == NEOHORSE_GUARDRAIL

    # 目录未声明该字段的模型：两条路径都不得凭空写键（其他模型零影响）
    plain_catalog = {"name": "plain-model", "context_window": 128000}
    plain_created = {"id": "mid-plain-1", "name": "plain-model", "context_window": 128000}
    model_manager._apply_preset_capability_fields(plain_created, plain_catalog)
    assert "system_prompt_extra" not in plain_created



# ---------------------------------------------------------------------------
# D 方案：验证声明护栏（"口头声称已验证、却没有命令执行证据"）
# ---------------------------------------------------------------------------


def test_verify_claim_pure_function_matrix():
    """检测函数：仅在"用过写入类工具 + 命中强验证声明 + 没有命令证据"时命中。"""
    from core.llm import _claims_verification_without_evidence as f

    assert f("代码已写入，测试已通过。", True, False) is True
    assert f("all tests passed", True, False) is True
    # 有命令执行证据 → 不算谎报
    assert f("测试已通过", True, True) is False
    # 本回合没写过文件 → 不介入
    assert f("测试已通过", False, False) is False
    # 弱表述（可能来自 read_file 确认）刻意不匹配，避免误伤
    assert f("已确认代码没问题", True, False) is False
    assert f("", True, False) is False
    assert f(None, True, False) is False
    # 否定/疑问语境不得命中（评审建议1）
    assert f("目前没有全部通过，还有 2 个用例失败。", True, False) is False
    assert f("是否已通过验证还不确定。", True, False) is False
    assert f("尚未通过", True, False) is False
    # 非声明用法（通过率）不得命中
    assert f("测试通过率 80%。", True, False) is False
    # 英文标记大小写不敏感（评审建议3）
    assert f("All Tests Passed", True, False) is True


def test_tool_call_activity_kinds_classification():
    """工具名粗分类：写入类看 write/edit，命令类看 COMMAND_TOOL_NAME_MARKERS。"""
    from core.llm import _tool_call_activity_kinds as kinds

    assert kinds([{"function": {"name": "write_file"}},
                  {"function": {"name": "execute_command"}}]) == (True, True)
    assert kinds([{"function": {"name": "edit_file"}}]) == (True, False)
    assert kinds([{"function": {"name": "execute_command"}}]) == (False, True)
    assert kinds([{"function": {"name": "read_file"}}]) == (False, False)
    assert kinds([]) == (False, False)


def test_verify_claim_nudge_injected_once_then_converges(mocker):
    """命中时恰好注入一条提醒（上限 1 次），随后模型正常收尾。"""
    mocker.patch("core.llm._claims_verification_without_evidence", return_value=True)

    responses = [
        iter([_chunk("代码已写入，测试已通过。")]),      # 命中 → 注入提醒
        iter([_chunk("未验证，如实说明。")]),            # 收敛
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
