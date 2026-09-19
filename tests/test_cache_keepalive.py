import pytest


def _wait_until(predicate, timeout=5.0, interval=0.02):
    """轮询等待条件成立；超时后做最后一次判定并由调用方断言。"""
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(interval)
    return predicate()


def _fire_immediately(service):
    """把已注册快照的 deadline 回拨到过去并唤醒线程，立即触发一次保活。"""
    with service._condition:
        service._last_activity -= service._interval + 1
        service._condition.notify_all()


def test_keepalive_interval_defaults_120_and_rejects_too_short(monkeypatch):
    from core.cache_keepalive import load_keepalive_interval

    # 未配置时默认 120 秒（防停顿后缓存 TTL 过期整轮 miss）
    monkeypatch.setattr("core.model_manager.load_config", lambda: {})
    assert load_keepalive_interval() == 120

    # 显式配置 0 仍可关闭
    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"prompt_cache_keepalive_seconds": 0},
    )
    assert load_keepalive_interval() == 0

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"prompt_cache_keepalive_seconds": 120},
    )
    assert load_keepalive_interval() == 120

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"prompt_cache_keepalive_seconds": 30},
    )
    with pytest.raises(ValueError, match="至少为 60"):
        load_keepalive_interval()


def test_keepalive_request_reuses_prefix_but_limits_output():
    from core.cache_keepalive import build_keepalive_request

    original = {
        "model": "openai/kimi-for-coding",
        "api_key": "secret",
        "api_base": "https://api.kimi.com/coding/v1",
        "messages": [{"role": "system", "content": "stable"}],
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
        "extra_body": {"prompt_cache_key": "stable-key", "reasoning_effort": "max"},
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 32000,
    }

    keepalive = build_keepalive_request(original)

    assert keepalive["messages"] == original["messages"]
    assert keepalive["messages"] is not original["messages"]
    assert keepalive["tools"] == original["tools"]
    assert keepalive["extra_body"]["prompt_cache_key"] == "stable-key"
    assert "reasoning_effort" not in keepalive["extra_body"]
    assert keepalive["stream"] is False
    assert keepalive["max_tokens"] == 1
    assert keepalive["tool_choice"] == "none"
    assert "stream_options" not in keepalive


def test_deepseek_keepalive_is_limited_to_official_routes():
    from core.llm import _supports_cache_keepalive

    assert _supports_cache_keepalive(
        "openai/deepseek-v4-pro", "https://api.deepseek.com"
    ) is True
    assert _supports_cache_keepalive(
        "openai/deepseek-v4-pro", "https://aggregator.example/v1"
    ) is False


def test_kimi_keepalive_is_limited_to_official_routes():
    from core.llm import _supports_cache_keepalive

    assert _supports_cache_keepalive(
        "openai/k3-256k", "https://api.kimi.com/coding/v1"
    ) is True
    assert _supports_cache_keepalive(
        "openai/kimi-k2.6", "https://tokenrhythm.studio/v1"
    ) is False


def test_deepseek_thinking_keepalive_omits_tool_choice_and_disables_thinking():
    from core.cache_keepalive import build_keepalive_request

    original = {
        "model": "openai/deepseek-v4-pro",
        "api_key": "secret",
        "api_base": "https://api.deepseek.com",
        "messages": [{"role": "system", "content": "stable"}],
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
        "reasoning_effort": "max",
        "extra_body": {"thinking": {"type": "enabled"}},
        "stream": True,
    }

    keepalive = build_keepalive_request(original)

    assert "tool_choice" not in keepalive
    assert "reasoning_effort" not in keepalive
    assert keepalive["extra_body"]["thinking"] == {"type": "disabled"}


def test_keepalive_response_is_accounted_in_dedicated_scope(monkeypatch):
    from types import SimpleNamespace
    import core.cache_keepalive as keepalive

    calls = []
    monkeypatch.setattr(
        "core.llm._account_server_usage",
        lambda usage, **kwargs: calls.append((usage, kwargs)) or {"valid": True},
    )
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=1,
        total_tokens=101,
        prompt_cache_hit_tokens=90,
        prompt_cache_miss_tokens=10,
    )

    result = keepalive.account_keepalive_response(
        SimpleNamespace(usage=usage),
        {
            "model_name": "openai/deepseek-v4-pro",
            "api_base": "https://api.deepseek.com",
            "session_id": "keepalive-session",
        },
    )

    assert result == {"valid": True}
    assert calls == [(usage, {
        "model_name": "openai/deepseek-v4-pro",
        "api_base": "https://api.deepseek.com",
        "session_id": "keepalive-session",
        "cache_scope": "keepalive",
        "invalidation_reason": "保活请求",
    })]


def test_keepalive_response_without_usage_fails_loudly():
    from types import SimpleNamespace
    from core.cache_keepalive import account_keepalive_response

    with pytest.raises(RuntimeError, match="usage"):
        account_keepalive_response(SimpleNamespace(), {})


def test_zhipu_keepalive_is_limited_to_official_routes():
    from core.llm import _supports_cache_keepalive

    assert _supports_cache_keepalive(
        "openai/glm-5.3", "https://open.bigmodel.cn/api/paas/v4"
    ) is True
    assert _supports_cache_keepalive(
        "zhipu/glm-5.2", "https://open.bigmodel.cn/api/paas/v4"
    ) is True
    # 同域官方 CodingPlan 套餐端点（OpenAI/Anthropic 协议）同样放行
    assert _supports_cache_keepalive(
        "openai/glm-5.3", "https://open.bigmodel.cn/api/coding/paas/v4"
    ) is True
    assert _supports_cache_keepalive(
        "anthropic/glm-5.3-flash", "https://open.bigmodel.cn/api/anthropic"
    ) is True
    # 官方域名下的未知路径不放行
    assert _supports_cache_keepalive(
        "openai/glm-5.3", "https://open.bigmodel.cn/api/unknown"
    ) is False
    # 第三方中转的 GLM 模型不放行
    assert _supports_cache_keepalive(
        "openai/glm-5.3", "https://tokenrhythm.studio/v1"
    ) is False
    assert _supports_cache_keepalive(
        "openai/zai-org/GLM-5.2", "https://api.siliconflow.cn/v1"
    ) is False
    assert _supports_cache_keepalive(
        "anthropic/glm-5.2", "https://ark.cn-beijing.volces.com/api/coding"
    ) is False


def test_glm_forced_thinking_keepalive_preserves_thinking_and_raises_output():
    from core.cache_keepalive import (
        KEEPALIVE_THINKING_MODEL_MAX_TOKENS,
        build_keepalive_request,
    )

    original = {
        "model": "openai/glm-5.3",
        "api_key": "secret",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "messages": [{"role": "system", "content": "stable"}],
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
        "reasoning_effort": "max",
        "extra_body": {"thinking": {"type": "enabled"}},
        "tool_choice": "auto",
        "stream": True,
    }

    keepalive = build_keepalive_request(original)

    # 强制思考模型收到 disabled 会直接报错；保活必须保留 enabled 并抬高上限
    assert keepalive["extra_body"]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in keepalive
    assert keepalive["max_tokens"] == KEEPALIVE_THINKING_MODEL_MAX_TOKENS
    assert keepalive["max_tokens"] > 1
    assert keepalive["tool_choice"] == "none"


def test_glm_closable_thinking_keepalive_still_flips_disabled():
    from core.cache_keepalive import build_keepalive_request

    original = {
        "model": "openai/glm-5.2",
        "api_key": "secret",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "messages": [{"role": "system", "content": "stable"}],
        "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        "stream": True,
    }

    keepalive = build_keepalive_request(original)

    # 有关闭档的 GLM 模型沿用既有行为：翻转 disabled + 1 token 上限
    assert keepalive["extra_body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in keepalive["extra_body"]
    assert keepalive["max_tokens"] == 1


def test_keepalive_timeout_scales_with_prompt_size():
    """保活请求超时必须随 prompt 规模放大（与主请求同口径：基础值 + tokens/1500）。

    写死 30 秒时，40 万 token 会话在缓存恰好过期、需要全量预填充的场景下
    必然超时；一次 Timeout 即令保活服务自停，随后 TTL 无人续、真实请求
    全量 miss（gui-runtime.log 中保活自停绝大多数为 Timeout）。缓存命中时
    预填充几乎免费，放大后的超时只是冷预填充的保险丝。"""
    from core.cache_keepalive import build_keepalive_request
    from core.llm import API_REQUEST_MAX_TIMEOUT_SECONDS, API_REQUEST_TIMEOUT_SECONDS

    base = {
        "model": "openai/qwen3.8-flash",
        "api_key": "secret",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    }

    small = build_keepalive_request({
        **base,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert small["timeout"] == API_REQUEST_TIMEOUT_SECONDS

    # 20 万 CJK 字 ≈ 20 万 token：90 + 133 = 223 秒
    mid = build_keepalive_request({
        **base,
        "messages": [{"role": "user", "content": "字" * 200_000}],
    })
    assert mid["timeout"] == API_REQUEST_TIMEOUT_SECONDS + 200_000 // 1500

    # 100 万 token：90 + 666 = 756 → 封顶 600
    huge = build_keepalive_request({
        **base,
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "字" * 1_000_000},
        ],
    })
    assert huge["timeout"] == API_REQUEST_MAX_TIMEOUT_SECONDS


def test_keepalive_retries_transient_failure_instead_of_stopping(monkeypatch):
    """瞬时失败（超时/限流/网络抖动）必须有界退避重试，不得一次失败即自停。

    自停是级联放大器：服务停 → TTL 5 分钟无人续 → 真实请求全量 miss
    （实测一次 40 万 token 会话因保活自停后全量 miss）。"""
    import core.cache_keepalive as ka

    calls = []

    class _Timeout(Exception):
        pass

    def fake_completion(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise _Timeout("keepalive timed out")
        return "ok-response"

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)
    accounted = []
    monkeypatch.setattr(
        ka, "account_keepalive_response",
        lambda response, telemetry: accounted.append(response) or {"valid": True},
    )
    monkeypatch.setattr(ka, "KEEPALIVE_RETRY_BACKOFF_SECONDS", 0.01)

    service = ka.CacheKeepaliveService()
    try:
        service.register_request(
            {
                "model": "openai/qwen3.8-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
            interval=60,
        )
        _fire_immediately(service)
        assert _wait_until(lambda: len(accounted) == 1), "首次失败后应退避重试并成功"
        assert len(calls) == 2
        assert service.status()["enabled"] is True
        assert service.status()["error"] == ""
    finally:
        service.register_request({}, interval=0)


def test_keepalive_stops_after_retries_exhausted(monkeypatch):
    """重试耗尽（1 次首发 + 2 次退避重试）仍失败才允许自停。"""
    import core.cache_keepalive as ka

    calls = []

    class _Timeout(Exception):
        pass

    def fake_completion(**kwargs):
        calls.append(1)
        raise _Timeout("keepalive timed out")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)
    monkeypatch.setattr(
        ka, "account_keepalive_response", lambda response, telemetry: {"valid": True}
    )
    monkeypatch.setattr(ka, "KEEPALIVE_RETRY_BACKOFF_SECONDS", 0.01)

    service = ka.CacheKeepaliveService()
    try:
        service.register_request(
            {
                "model": "openai/qwen3.8-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
            interval=60,
        )
        _fire_immediately(service)
        assert _wait_until(lambda: service.status()["enabled"] is False), "重试耗尽应自停"
        assert len(calls) == 3
        assert service.status()["error"] == "_Timeout"
    finally:
        service.register_request({}, interval=0)


def test_keepalive_retry_backoff_yields_to_new_registration(monkeypatch):
    """退避等待期间：服务不得自停（还在重试节奏里），新请求注册后旧重试立即让位。"""
    import time as _time
    import core.cache_keepalive as ka

    calls = []

    class _Timeout(Exception):
        pass

    def fake_completion(**kwargs):
        calls.append(1)
        raise _Timeout("keepalive timed out")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)
    monkeypatch.setattr(
        ka, "account_keepalive_response", lambda response, telemetry: {"valid": True}
    )
    # 拉长退避窗口，保证测试能在退避期内完成新注册
    monkeypatch.setattr(ka, "KEEPALIVE_RETRY_BACKOFF_SECONDS", 30.0)

    service = ka.CacheKeepaliveService()
    try:
        service.register_request(
            {
                "model": "openai/qwen3.8-flash",
                "messages": [{"role": "user", "content": "q1"}],
            },
            interval=60,
        )
        _fire_immediately(service)
        assert _wait_until(lambda: len(calls) == 1)
        # 退避期内服务仍在重试节奏中，未自停（旧 bug：一次失败立即停）
        _time.sleep(0.3)
        assert service.status()["enabled"] is True
        # 新注册接管：旧快照不再补发重试请求
        service.register_request(
            {
                "model": "openai/qwen3.8-flash",
                "messages": [{"role": "user", "content": "q2"}],
            },
            interval=60,
        )
        _time.sleep(0.5)
        assert len(calls) == 1
        assert service.status()["enabled"] is True
    finally:
        service.register_request({}, interval=0)
