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
