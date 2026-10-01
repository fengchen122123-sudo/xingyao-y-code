# -*- coding: utf-8 -*-
"""退化输出护栏（修复①：填充词判据收紧）纯单元测试。

被测对象：``core/llm.py`` 的 ``_is_meaningful_assistant_content``。

- 纯填充词（"好。""嗯。""OK。"等）必须被判为“无有效正文”，
  以便下游慢步熔断 / 总结催促 / 收尾报错三道护栏恢复正常生效；
- 含实义短句（"好的，已完成"等）与正常长回复不得被误判；
- 函数名与签名不得改动，空串 / None 语义保持原样（防回归）。
"""
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
    """用例1：纯填充词（剥完噪声后只剩词表元素）判为空正文。"""
    assert _is_meaningful_assistant_content(text) is False


@pytest.mark.parametrize(
    "text", ["好的，已完成", "OK，我来处理", "可以，但需要确认路径"]
)
def test_short_reply_with_meaning_is_meaningful(text):
    """用例2：含实义短句的短回复不得被误判为填充词。"""
    assert _is_meaningful_assistant_content(text) is True


def test_normal_long_reply_is_meaningful():
    """用例3：正常长回复（≥100 字中文结论）不误判。"""
    text = (
        "已完成对目标模块的排查：首先定位到判定函数剥离噪声字符后仍把填充词当作有效正文，"
        "导致慢步熔断、最终总结催促与收尾兜底报错三道护栏同时短路；其次确认进展护栏只比较"
        "确定性状态，对自然语言填充词并不敏感；最后给出收紧判据的最小修复方案，并通过回归测试。"
    )
    assert len(text) >= 100
    assert _is_meaningful_assistant_content(text) is True


def test_signature_unchanged():
    """用例9：签名回归——参数名与数量未变（仅 text 一个参数）。"""
    params = inspect.signature(_is_meaningful_assistant_content).parameters
    assert list(params) == ["text"]


def test_empty_and_none_semantics_unchanged():
    """用例10：空串 / None 仍判为空正文，语义保持不变。"""
    assert _is_meaningful_assistant_content("") is False
    assert _is_meaningful_assistant_content(None) is False


# ---------------------------------------------------------------------------
# 修复②：流式装配层退化（复读）检测
#
# 场景同源：模型连续多步只重复同一段短正文/思考，既不调用工具也不给结论，
# 前端气泡持续刷屏、状态栏长期停在"生成中"，用户视角等同卡死。
# 本组用例全部用假 provider 流驱动，绝不发起真实网络调用，也绝不落盘。
# ---------------------------------------------------------------------------


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
    """退化（复读）护栏发出的事件：消息里带"复读"字样，避免与其它 notice/error 混淆。"""
    return [
        event for event in events
        if event.kind == kind and "复读" in str(event.payload.get("message") or "")
    ]


def _nudges(messages):
    """内存 messages 里由退化护栏追加的收敛催促（复用慢步催促文案）。"""
    return [
        message for message in messages
        if message.get("role") == "user"
        and message.get("content") == SLOW_STEP_NUDGE_TEXT
    ]


# 正常调查回合的 5 步正文：互不相同、长度均超过 20 字符（不触发"短且相同"判据）。
NORMAL_STEP_TEXTS = (
    "第一步：列出工作目录下的源文件，确认模块整体布局。",
    "已定位配置加载入口，继续核对默认值与环境变量覆盖逻辑。",
    "检查测试目录结构，找出与本次修复相关的用例文件。",
    "对照方案文档核实改动锚点，确认函数名与签名没有被改。",
    "汇总上述五个环节的发现，准备输出最终结论。",
)
NORMAL_FINAL_TEXT = "调查结束：已确认五个环节均无异常，本回合可以正常收尾。"


def test_near_repeat_fingerprints_are_degenerate():
    """用例8：指纹两两相似度 ≥0.9 的近似复读判为退化（纯函数层）。

    典型场景是模型把同一句填充词换着标点/多一个字反复输出（"好的。"/"好。"/"好的"），
    指纹只有一字之差，必须靠相似度判据而不是"逐位相等"捕获。
    """
    fingerprints = [_degenerate_fingerprint(text) for text in ("好的。", "好。", "好的")]
    assert fingerprints == ["好的", "好", "好的"], "指纹应为去掉标点与空白后的字符串"
    assert _is_degenerate_repeat(fingerprints) is True

    # 不足 3 步不判退化；空指纹（无正文/无思考的步骤）不参与判定
    assert _is_degenerate_repeat(["好的", "好"]) is False
    assert _is_degenerate_repeat(["", "", ""]) is False
    # 指纹互不相同的正常多步不判退化
    assert _is_degenerate_repeat(["已读取配置", "正在检查日志", "准备输出结论"]) is False


def test_normal_multi_step_turn_does_not_trigger_degenerate_guard(tmp_path, mocker):
    """用例5：正常多步回合不得误触退化检测（纯函数层 + 端到端层）。"""
    # 纯函数层：5 个互不相同的指纹，任一 3 步滑窗都不得判为退化
    fingerprints = [_degenerate_fingerprint(text) for text in NORMAL_STEP_TEXTS]
    assert len(set(fingerprints)) == len(NORMAL_STEP_TEXTS)
    for start in range(len(fingerprints) - 2):
        assert _is_degenerate_repeat(fingerprints[start:start + 3]) is False

    # 端到端层：5 步各自带一次只读工具调用（回合因此继续到下一步），正文互不相同
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
    """用例4：连续多步只输出 "好。" → 首次命中发 notice + 催促，二次命中发 error 收尾。

    构造说明：修复①已让 "好。" 判为无有效正文，单步"只有填充词且无工具无思考"会直接
    走收尾兜底结束回合，因此这里用"填充词正文 + 恒定思考内容"构造连续多步（思考促升后
    正文会被改写，正文字符串指纹必须取促升前原文——正是实现里的口径坑）。
    防挂死保护：completion 被调用超过 8 次即断言失败，测试自身绝不死循环。
    """
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

    # 首次命中：notice 可见 + 恰好一条收敛催促进入上下文
    assert _degenerate_events(events, "notice"), "首次命中必须发 notice 让用户可见"
    assert len(_nudges(messages)) == 1, "首次命中应恰好追加一条收敛催促"
    # 二次命中：error 可见，并正常收尾（不是静默结束）
    assert _degenerate_events(events, "error"), "二次命中必须发 error 让用户可见"
    assert completion.call_count <= 6, "退化护栏必须在有限步内止损"
    assert events[-1].kind == "turn_finished"


# ---------------------------------------------------------------------------
# 修复②收紧：判定只针对"空转步"（无工具调用），带工具调用的相似短步骤不得误判
#
# 收紧理由：有工具调用说明模型正在推进任务，不属于"既不调工具也不给结论"的
# 故障模式；若对有工具调用的步也判定，首次命中会 continue 直接丢弃该步真实
# tool_calls（工具不执行、assistant 消息不入历史），把正常多步任务误杀。
# ---------------------------------------------------------------------------

SIMILAR_TOOL_STEP_TEXT = "好的，先读取配置。"
SIMILAR_TOOL_FINAL_TEXT = (
    "配置读取完成：已核对默认值与环境变量覆盖逻辑，未发现异常，本回合可以正常收尾。"
)


def test_similar_short_steps_with_tool_calls_do_not_trigger_degenerate_guard(tmp_path, mocker):
    """用例11：带工具调用的相似短步骤不触发退化检测，也不丢失工具调用。

    构造连续 3 步：正文都是同一个 ≤20 字短句，且每步都带一个只读工具调用
    （glob_files），之后补一步正常长结论收尾。
    防挂死保护：completion 被调用超过 8 次即断言失败，测试自身绝不死循环。
    """
    # 前置校验：该正文确实构成"连续 3 步相同且 ≤20 字"的退化形状——
    # 若无工具调用必然触发，确保用例确实卡在"有工具调用时不得误判"这个点上。
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

    # 有工具调用的步不是空转：不得触发退化 notice/error，也不得追加收敛催促
    assert _degenerate_events(events, "notice") == [], "有工具调用的步不得触发退化 notice"
    assert _degenerate_events(events, "error") == [], "有工具调用的步不得触发退化 error"
    assert _nudges(messages) == [], "带工具调用的相似短步骤不得被追加退化催促"
    # 工具步骤被正常处理：3 次工具步 + 1 次收尾 = 4 次请求，回合推进到收尾
    assert len(calls) == 4, "工具步骤必须被正常处理并推进到收尾步"
    assert any(
        message.get("role") == "tool" for message in messages
    ), "工具必须真正执行并把结果回填进历史"
    assert events[-1].kind == "turn_finished"


# ---------------------------------------------------------------------------
# 修复③：额度/鉴权类确定性错误 fail-fast（不再退避重试）
#
# 402（额度耗尽）/401（鉴权失败）/403（权限受限）/404（模型不存在）都是确定性
# 失败，重试只会反复空烧额度、拖长用户等待；429 等瞬时错误仍保持既有退避重试。
# ---------------------------------------------------------------------------


class _StubApiError(Exception):
    """模拟 provider 抛出的带状态码的 API 异常（可被 map_llm_error 分类）。"""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


QUOTA_ERROR_TEXT = (
    "Error code: 429 - {'error': {'message': 'You exceeded your current quota', "
    "'type': 'insufficient_quota'}}"
)
RATE_LIMIT_ERROR_TEXT = "Error code: 429 - Rate limit reached for requests per minute (RPM)"


def _make_fast_retry(mocker):
    """把退避等待压到最小，避免测试真实 sleep（性能红线）。

    - is_rate_limit_error 固定返回正的小 retry_after：绕开 5*2^n 的指数退避；
    - random.random 归零：去掉 jitter，等待时长确定且极小；
    - time.sleep no-op：配合小 retry_after，忙等循环仅几十毫秒。
    """
    mocker.patch("core.llm.is_rate_limit_error", lambda exc: (True, 0.05))
    mocker.patch("core.llm.random.random", lambda: 0.0)
    mocker.patch("core.llm.time.sleep", lambda *args, **kwargs: None)


def test_quota_error_fails_fast_without_retry(mocker):
    """用例6：402/insufficient_quota 判为确定性失败，端到端只调用 1 次（不重试）。"""
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
    # 既有失败路径收尾：发错误事件，且按异常回合收尾（不发 turn_finished）
    assert any(event.kind == "error" for event in events)
    assert events[-1].kind == "turn_error"


def test_rate_limit_error_still_retries(mocker):
    """用例7：429/rate_limit 判为非确定性失败，端到端确实发生重试（≥2 次调用）。

    性能红线：`_make_fast_retry` 把等待压到几十毫秒，整个用例在一两秒内完成。
    """
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
