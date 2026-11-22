# -*- coding: utf-8 -*-
import inspect
import json
from types import SimpleNamespace

import pytest

from core.llm import (
    SLOW_STEP_NUDGE_TEXT,
    _degenerate_fingerprint,
    _is_degenerate_repeat,
    _is_meaningful_assistant_content,
    chat_with_model_events,
)


@pytest.mark.parametrize("text", ["好。", "嗯。", "OK。", "好的", "收到……"])
def test_filler_only_text_is_not_meaningful(text):
    assert _is_meaningful_assistant_content(text) is False


@pytest.mark.parametrize(
    "text", ["好的，已完成", "OK，我来处理", "可以，但需要确认路径"]
)
def test_short_reply_with_meaning_is_meaningful(text):
    assert _is_meaningful_assistant_content(text) is True


def test_normal_long_reply_is_meaningful():
    text = (
        "已完成对目标模块的排查：首先定位到判定函数剥离噪声字符后仍把填充词当作有效正文，"
        "导致慢步熔断、最终总结催促与收尾兜底报错三道护栏同时短路；其次确认进展护栏只比较"
        "确定性状态，对自然语言填充词并不敏感；最后给出收紧判据的最小修复方案，并通过回归测试。"
    )
    assert len(text) >= 100
    assert _is_meaningful_assistant_content(text) is True


def test_signature_unchanged():
    params = inspect.signature(_is_meaningful_assistant_content).parameters
    assert list(params) == ["text"]


def test_empty_and_none_semantics_unchanged():
    assert _is_meaningful_assistant_content("") is False
    assert _is_meaningful_assistant_content(None) is False


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
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _degenerate_events(events, kind):
    return [
        event for event in events
        if event.kind == kind and "复读" in str(event.payload.get("message") or "")
    ]


def _nudges(messages):
    return [
        message for message in messages
        if message.get("role") == "user"
        and message.get("content") == SLOW_STEP_NUDGE_TEXT
    ]


NORMAL_STEP_TEXTS = (
    "第一步：列出工作目录下的源文件，确认模块整体布局。",
    "已定位配置加载入口，继续核对默认值与环境变量覆盖逻辑。",
    "检查测试目录结构，找出与本次修复相关的用例文件。",
    "对照方案文档核实改动锚点，确认函数名与签名没有被改。",
    "汇总上述五个环节的发现，准备输出最终结论。",
)
NORMAL_FINAL_TEXT = "调查结束：已确认五个环节均无异常，本回合可以正常收尾。"


def test_near_repeat_fingerprints_are_degenerate():
    fingerprints = [_degenerate_fingerprint(text) for text in ("好的。", "好。", "好的")]
    assert fingerprints == ["好的", "好", "好的"], "指纹应为去掉标点与空白后的字符串"
    assert _is_degenerate_repeat(fingerprints) is True

    assert _is_degenerate_repeat(["好的", "好"]) is False
    assert _is_degenerate_repeat(["", "", ""]) is False
    assert _is_degenerate_repeat(["已读取配置", "正在检查日志", "准备输出结论"]) is False


def test_normal_multi_step_turn_does_not_trigger_degenerate_guard(tmp_path, mocker):
    fingerprints = [_degenerate_fingerprint(text) for text in NORMAL_STEP_TEXTS]
    assert len(set(fingerprints)) == len(NORMAL_STEP_TEXTS)
    for start in range(len(fingerprints) - 2):
        assert _is_degenerate_repeat(fingerprints[start:start + 3]) is False

    responses = [
        iter([_chunk(
            text,
            tool_calls=[_tool_call(0, f"glob-{index}", "glob_files", {
                "pattern": f"*.p{index}",
                "dir_path": str(tmp_path),
            })],
        )])
        for index, text in enumerate(NORMAL_STEP_TEXTS)
    ]
    responses.append(iter([_chunk(NORMAL_FINAL_TEXT)]))
    completion = mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "帮我调查一下这个仓库"}]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-degenerate-normal",
        consume_background_notifications=False,
    ))

    assert completion.call_count == len(NORMAL_STEP_TEXTS) + 1
    assert _nudges(messages) == [], "正常回合不得追加退化收敛催促"
    assert _degenerate_events(events, "notice") == []
    assert _degenerate_events(events, "error") == []
    assert events[-1].kind == "turn_finished"


def test_degenerate_repeat_nudges_then_aborts_turn(mocker):
    calls = []

    def _fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) > 8:
            raise AssertionError("退化护栏未止损：Provider 请求次数超过上限（疑似死循环）")
        return iter([_chunk(
            "好。",
            reasoning="我在想下一步应该做什么，先把当前状态确认一下。",
        )])

    completion = mocker.patch("core.llm.completion", side_effect=_fake_completion)
    messages = [{"role": "user", "content": "你帮我去找一下 openc"}]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-degenerate-abort",
        consume_background_notifications=False,
    ))

    assert _degenerate_events(events, "notice"), "首次命中必须发 notice 让用户可见"
    assert len(_nudges(messages)) == 1, "首次命中应恰好追加一条收敛催促"
    assert _degenerate_events(events, "error"), "二次命中必须发 error 让用户可见"
    assert completion.call_count <= 6, "退化护栏必须在有限步内止损"
    assert events[-1].kind == "turn_finished"


SIMILAR_TOOL_STEP_TEXT = "好的，先读取配置。"
SIMILAR_TOOL_FINAL_TEXT = (
    "配置读取完成：已核对默认值与环境变量覆盖逻辑，未发现异常，本回合可以正常收尾。"
)


def test_similar_short_steps_with_tool_calls_do_not_trigger_degenerate_guard(tmp_path, mocker):
    assert len(SIMILAR_TOOL_STEP_TEXT) <= 20
    assert _is_degenerate_repeat(
        [_degenerate_fingerprint(SIMILAR_TOOL_STEP_TEXT)] * 3
    ) is True

    responses = [
        iter([_chunk(
            SIMILAR_TOOL_STEP_TEXT,
            tool_calls=[_tool_call(0, f"glob-deg-{index}", "glob_files", {
                "pattern": f"*.cfg{index}",
                "dir_path": ".",
            })],
        )])
        for index in range(3)
    ]
    responses.append(iter([_chunk(SIMILAR_TOOL_FINAL_TEXT)]))
    calls = []

    def _fake_completion(**kwargs):
        calls.append(1)
        if len(calls) > 8:
            raise AssertionError(
                "带工具调用的相似短步骤疑似被误判：Provider 请求次数超过上限（丢工具后空转）"
            )
        return responses[len(calls) - 1]

    mocker.patch("core.llm.completion", side_effect=_fake_completion)
    messages = [{"role": "user", "content": "帮我检查一下项目配置"}]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-degenerate-toolcall",
        consume_background_notifications=False,
    ))

    assert _degenerate_events(events, "notice") == [], "有工具调用的步不得触发退化 notice"
    assert _degenerate_events(events, "error") == [], "有工具调用的步不得触发退化 error"
    assert _nudges(messages) == [], "带工具调用的相似短步骤不得被追加退化催促"
    assert len(calls) == 4, "工具步骤必须被正常处理并推进到收尾步"
    assert any(
        message.get("role") == "tool" for message in messages
    ), "工具必须真正执行并把结果回填进历史"
    assert events[-1].kind == "turn_finished"


class _StubApiError(Exception):

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


QUOTA_ERROR_TEXT = (
    "Error code: 429 - {'error': {'message': 'You exceeded your current quota', "
    "'type': 'insufficient_quota'}}"
)
RATE_LIMIT_ERROR_TEXT = "Error code: 429 - Rate limit reached for requests per minute (RPM)"


def _make_fast_retry(mocker):
    mocker.patch("core.llm.is_rate_limit_error", lambda exc: (True, 0.05))
    mocker.patch("core.llm.random.random", lambda: 0.0)
    mocker.patch("core.llm.time.sleep", lambda *args, **kwargs: None)


def test_quota_error_fails_fast_without_retry(mocker):
    from core.llm import (
        _DETERMINISTIC_API_FAILURE_CATEGORIES,
        _is_deterministic_api_failure,
    )

    assert "insufficient_quota" in _DETERMINISTIC_API_FAILURE_CATEGORIES
    quota_exc = _StubApiError(QUOTA_ERROR_TEXT, status_code=402)
    assert _is_deterministic_api_failure(quota_exc) is True

    _make_fast_retry(mocker)
    calls = []

    def _fake_completion(**kwargs):
        calls.append(1)
        if len(calls) > 8:
            raise AssertionError("额度类错误未 fail-fast：Provider 请求次数超过上限")
        raise quota_exc

    mocker.patch("core.llm.completion", side_effect=_fake_completion)
    messages = [{"role": "user", "content": "帮我生成一份报告"}]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-quota-fail-fast",
        consume_background_notifications=False,
    ))

    assert len(calls) == 1, "额度类确定性失败必须只请求一次，不得进入退避重试"
    assert any(event.kind == "error" for event in events)
    assert events[-1].kind == "turn_error"


def test_rate_limit_error_still_retries(mocker):
    from core.llm import _is_deterministic_api_failure

    rate_exc = _StubApiError(RATE_LIMIT_ERROR_TEXT, status_code=429)
    assert _is_deterministic_api_failure(rate_exc) is False

    _make_fast_retry(mocker)
    calls = []

    def _fake_completion(**kwargs):
        calls.append(1)
        if len(calls) > 8:
            raise AssertionError("限流错误重试次数超过上限（疑似死循环）")
        raise rate_exc

    mocker.patch("core.llm.completion", side_effect=_fake_completion)
    messages = [{"role": "user", "content": "帮我生成一份报告"}]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-rate-limit-retry",
        consume_background_notifications=False,
    ))

    assert len(calls) >= 2, "限流（429）必须保持既有退避重试行为，不得 fail-fast"
    assert any(event.kind == "turn_error" for event in events)
