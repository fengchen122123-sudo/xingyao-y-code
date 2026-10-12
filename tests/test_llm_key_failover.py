"""集成测试：厂商多 Key 粘性故障转移接入主请求链路（core/llm.py）。

设计依据：docs/superpowers/specs/2026-10-12-provider-multi-key-failover-design.md
（第 3 节「请求链路接入」）。

mock 层级：
- ``core.llm.completion``：控制上游响应（抛错 / 返回 chunk 流）；
- ``core.model_manager.load_config``：注入双 Key provider 配置；
- Key 池本身走真实 ``core.provider_keys.build_key_failover`` / ``ProviderKeyPool``，
  仅清理进程内 ``_POOL_REGISTRY`` 保证测试隔离（池为进程内存态）。

覆盖行为规格：
a) 402 额度耗尽 → 第二次请求自动换到第二个 Key 并成功；成功 Key 成为新 sticky；
b) 401 鉴权失败 → 同样自动切换；
c) 两个 Key 全失败 → 抛原错误，completion 调用次数 = Key 数（本链每 Key 只试一次）；
d) 成功路径 → api_key 全程不变（跨 Turn 粘性），不触发切换；
e) 429 限速 → 不切换 Key（走既有退避重试，sleep 已 mock 为 no-op）。
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

import core.llm as llm
from core import provider_keys
from core.llm import chat_with_model_events

BASE_URL = "https://fake-key-failover.invalid/v1"
MODEL_NAME = "fake-key-failover-model"
KEY_1 = "sk-fake-failover-key-one"
KEY_2 = "sk-fake-failover-key-two"

QUOTA_ERROR_TEXT = (
    "Error code: 402 - {'error': {'message': 'You exceeded your current quota', "
    "'type': 'insufficient_quota'}}"
)
AUTH_ERROR_TEXT = (
    "Error code: 401 - {'error': {'code': 'invalid_api_key', "
    "'message': 'Incorrect API key provided'}}"
)
FORBIDDEN_ERROR_TEXT = "Error code: 403 - Forbidden: permission denied for this key"
NOT_FOUND_ERROR_TEXT = (
    "Error code: 404 - {'error': {'message': 'model_not_found', "
    "'type': 'invalid_request_error'}}"
)
RATE_LIMIT_ERROR_TEXT = "Error code: 429 - Rate limit reached for requests per minute (RPM)"

PROVIDER_CONFIG = {
    "providers": [
        {
            "id": "prov-key-failover",
            "name": "Fake Key Failover Provider",
            "base_url": BASE_URL,
            "protocol": "openai",
            "api_key": f"{KEY_1}\n{KEY_2}",
            "models": [
                {
                    "id": "model-key-failover",
                    "name": "Fake Key Failover Model",
                    "litellm_model": MODEL_NAME,
                }
            ],
        }
    ]
}


class _StubApiError(Exception):
    """带状态码的上游异常（可被 map_llm_error 分类）。"""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _chunk(content=""):
    delta = SimpleNamespace(
        content=content,
        reasoning_content="",
        tool_calls=None,
        provider_specific_fields=None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _make_fast_retry(mocker):
    """把退避等待压到最小，避免测试真实 sleep（参照 test_degenerate_output_guard）。"""
    mocker.patch("core.llm.is_rate_limit_error", lambda exc: (True, 0.01))
    mocker.patch("core.llm.random.random", lambda: 0.0)
    mocker.patch("core.llm.time.sleep", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _double_key_provider(mocker):
    """注入双 Key provider 配置，并在测试前后重置进程内 Key 池注册表。"""
    mocker.patch(
        "core.model_manager.load_config", return_value=deepcopy(PROVIDER_CONFIG)
    )
    provider_keys._POOL_REGISTRY.clear()
    yield
    provider_keys._POOL_REGISTRY.clear()


def _run_turn(turn_id, messages=None):
    """跑一个最小成本的完整回合（persist=False，不消费后台通知）。"""
    if messages is None:
        messages = [{"role": "user", "content": "你好"}]
    return list(
        chat_with_model_events(
            MODEL_NAME,
            KEY_1,
            messages,
            api_base=BASE_URL,
            persist=False,
            turn_id=turn_id,
            consume_background_notifications=False,
        )
    )


def test_key_related_failure_classification():
    """_is_key_related_failure 仅覆盖三类 Key 类失败；429/404 等不命中。"""
    assert llm._KEY_RELATED_FAILURE_CATEGORIES == frozenset({
        "insufficient_quota", "auth_invalid", "permission_denied",
    })
    assert llm._is_key_related_failure(
        _StubApiError(QUOTA_ERROR_TEXT, status_code=402)
    ) is True
    assert llm._is_key_related_failure(
        _StubApiError(AUTH_ERROR_TEXT, status_code=401)
    ) is True
    assert llm._is_key_related_failure(
        _StubApiError(FORBIDDEN_ERROR_TEXT, status_code=403)
    ) is True
    assert llm._is_key_related_failure(
        _StubApiError(RATE_LIMIT_ERROR_TEXT, status_code=429)
    ) is False
    assert llm._is_key_related_failure(
        _StubApiError(NOT_FOUND_ERROR_TEXT, status_code=404)
    ) is False
    assert llm._is_key_related_failure(None) is False


def test_quota_error_switches_to_next_key_and_keeps_new_key_sticky(mocker):
    """a) 402 额度耗尽：第二次请求自动用 k2 成功；k2 成为后续请求链的 sticky Key。"""
    keys_used = []

    def completion(**kwargs):
        keys_used.append(kwargs.get("api_key"))
        if len(keys_used) == 1:
            raise _StubApiError(QUOTA_ERROR_TEXT, status_code=402)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=completion)
    info_spy = mocker.patch.object(llm.logger, "info")

    events = _run_turn("turn-quota-switch")

    # 同一请求链内：第一次用 KEY_1（失败）→ 静默换到 KEY_2（成功）
    assert keys_used == [KEY_1, KEY_2]
    assert events[-1].kind == "turn_finished"
    assert not any(event.kind in ("error", "turn_error") for event in events)

    # 静默：仅 logger 留痕，且日志/事件绝不包含 Key 明文
    info_args = [str(call.args[0]) for call in info_spy.call_args_list if call.args]
    assert any("多 Key 静默切换" in message for message in info_args)
    assert not any(KEY_1 in message or KEY_2 in message for message in info_args)

    # 粘性迁移：后续请求链直接使用成功过的 KEY_2，不回切 KEY_1
    events_second = _run_turn("turn-quota-switch-second")
    assert keys_used == [KEY_1, KEY_2, KEY_2]
    assert events_second[-1].kind == "turn_finished"


def test_auth_error_switches_to_next_key(mocker):
    """b) 401 鉴权失败：自动切换到第二个 Key 重发。"""
    keys_used = []

    def completion(**kwargs):
        keys_used.append(kwargs.get("api_key"))
        if len(keys_used) == 1:
            raise _StubApiError(AUTH_ERROR_TEXT, status_code=401)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=completion)

    events = _run_turn("turn-auth-switch")

    assert keys_used == [KEY_1, KEY_2]
    assert events[-1].kind == "turn_finished"
    assert not any(event.kind in ("error", "turn_error") for event in events)


def test_all_keys_fail_raises_original_error_without_infinite_retry(mocker):
    """c) 两个 Key 全失败：抛原错误，completion 调用次数 = Key 数（本链每 Key 只试一次）。"""
    keys_used = []
    quota_exc = _StubApiError(QUOTA_ERROR_TEXT, status_code=402)

    def completion(**kwargs):
        keys_used.append(kwargs.get("api_key"))
        if len(keys_used) > 4:
            raise AssertionError("全部 Key 失败后仍在重试：疑似死循环")
        raise quota_exc

    mocker.patch("core.llm.completion", side_effect=completion)

    events = _run_turn("turn-all-keys-fail")

    assert keys_used == [KEY_1, KEY_2], "每个 Key 在同一条请求链内只应尝试一次"
    assert any(event.kind == "error" for event in events)
    assert events[-1].kind == "turn_error"


def test_success_path_never_switches_key(mocker):
    """d) 成功路径：api_key 全程不变（两个连续 Turn 都用 sticky Key），不触发切换。"""
    keys_used = []

    def completion(**kwargs):
        keys_used.append(kwargs.get("api_key"))
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=completion)
    info_spy = mocker.patch.object(llm.logger, "info")

    first = _run_turn("turn-sticky-one")
    second = _run_turn("turn-sticky-two")

    assert keys_used == [KEY_1, KEY_1]
    assert first[-1].kind == "turn_finished"
    assert second[-1].kind == "turn_finished"
    info_args = [str(call.args[0]) for call in info_spy.call_args_list if call.args]
    assert not any("多 Key 静默切换" in message for message in info_args)


def test_rate_limit_does_not_switch_key_and_keeps_retrying(mocker):
    """e) 429 限速：不切换 Key，走既有退避重试（第三次成功）。"""
    _make_fast_retry(mocker)
    keys_used = []
    rate_exc = _StubApiError(RATE_LIMIT_ERROR_TEXT, status_code=429)

    def completion(**kwargs):
        keys_used.append(kwargs.get("api_key"))
        if len(keys_used) <= 2:
            raise rate_exc
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=completion)

    events = _run_turn("turn-rate-limit-no-switch")

    assert keys_used == [KEY_1, KEY_1, KEY_1], "429 与 Key 无关，必须保持同一 Key 重试"
    assert events[-1].kind == "turn_finished"
    assert not any(event.kind in ("error", "turn_error") for event in events)
