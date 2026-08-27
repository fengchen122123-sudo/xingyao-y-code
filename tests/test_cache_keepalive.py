import pytest


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
