import pytest


def _wait_until(predicate, timeout=5.0, interval=0.02):
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(interval)
    return predicate()


def _fire_immediately(service, session_key=None):
    with service._condition:
        if session_key is None:
            targets = list(service._entries.items())
        else:
            targets = [(session_key, service._entries[session_key])]
        assert targets, "没有已注册的保活条目"
        for _key, entry in targets:
            entry["last_activity"] -= entry["interval"] + 1
        service._condition.notify_all()


def _keepalive_request(tag):
    return {
        "model": "openai/qwen3.8-flash",
        "messages": [{"role": "user", "content": tag}],
    }


@pytest.fixture(autouse=True)
def _clear_route_config_caches():
    import core.llm_dialects as dialects

    def _clear():
        with dialects._qwen_cache_cfg_lock:
            dialects._keepalive_routes_cfg_cache["entry"] = None
            dialects._qwen_cache_cfg_cache.clear()

    _clear()
    yield
    _clear()


def _drop_sessions(service, *session_keys):
    for session_key in session_keys:
        service.register_request({}, interval=0, session_key=session_key)


def test_keepalive_interval_defaults_120_and_rejects_too_short(monkeypatch):
    from core.cache_keepalive import load_keepalive_interval

    monkeypatch.setattr("core.model_manager.load_config", lambda: {})
    assert load_keepalive_interval() == 120

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
    # 配置路径绝不抛出（每轮主请求前置区裸调，raise 会打穿全部对话）：
    # 越界回落默认 120
    assert load_keepalive_interval() == 120
    from core.cache_keepalive import CacheKeepaliveService

    with pytest.raises(ValueError, match="至少为 60"):
        CacheKeepaliveService().register_request(_keepalive_request("t"), interval=30)


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


def test_deepseek_keepalive_covers_third_party_routes():
    from core.llm import _supports_cache_keepalive

    assert _supports_cache_keepalive(
        "openai/deepseek-v4-pro", "https://api.deepseek.com"
    ) is True
    assert _supports_cache_keepalive(
        "openai/deepseek-v4-pro", "https://aggregator.example/v1"
    ) is True


def test_kimi_keepalive_covers_third_party_routes(monkeypatch):
    from core.llm import _supports_cache_keepalive

    monkeypatch.setattr("core.model_manager.load_config", lambda: {})

    assert _supports_cache_keepalive(
        "openai/k3-256k", "https://api.kimi.com/coding/v1"
    ) is True
    assert _supports_cache_keepalive(
        "openai/kimi-k2.6", "https://tokenrhythm.studio/v1"
    ) is True


def test_keepalive_routes_config_enables_explicit_gateway_routes(monkeypatch):
    from core.llm import _supports_cache_keepalive

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"cache_keepalive_routes": ["tokenrhythm.studio"]},
    )

    assert _supports_cache_keepalive(
        "openai/some-model", "https://tokenrhythm.studio/v1"
    ) is True
    assert _supports_cache_keepalive(
        "openai/some-model", "https://TokenRhythm.Studio"
    ) is True
    assert _supports_cache_keepalive(
        "openai/some-model", "https://tokenrhythm.studio:8443/v1"
    ) is True
    assert _supports_cache_keepalive(
        "openai/some-model", "https://aggregator.example/v1"
    ) is False

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"cache_keepalive_routes": ["https://tokenrhythm.studio/v1"]},
    )
    assert _supports_cache_keepalive(
        "openai/some-model", "https://tokenrhythm.studio/v1"
    ) is True


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


def test_zhipu_keepalive_covers_third_party_routes():
    from core.llm import _supports_cache_keepalive

    assert _supports_cache_keepalive(
        "openai/glm-5.3", "https://open.bigmodel.cn/api/paas/v4"
    ) is True
    assert _supports_cache_keepalive(
        "zhipu/glm-5.2", "https://open.bigmodel.cn/api/paas/v4"
    ) is True
    assert _supports_cache_keepalive(
        "openai/glm-5.3", "https://open.bigmodel.cn/api/coding/paas/v4"
    ) is True
    assert _supports_cache_keepalive(
        "anthropic/glm-5.3-flash", "https://open.bigmodel.cn/api/anthropic"
    ) is True
    assert _supports_cache_keepalive(
        "openai/glm-5.3", "https://open.bigmodel.cn/api/unknown"
    ) is True
    assert _supports_cache_keepalive(
        "openai/glm-5.3", "https://tokenrhythm.studio/v1"
    ) is True
    assert _supports_cache_keepalive(
        "openai/zai-org/GLM-5.2", "https://api.siliconflow.cn/v1"
    ) is True
    assert _supports_cache_keepalive(
        "anthropic/glm-5.2", "https://ark.cn-beijing.volces.com/api/coding"
    ) is True


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

    assert keepalive["extra_body"]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in keepalive
    assert keepalive["max_tokens"] == KEEPALIVE_THINKING_MODEL_MAX_TOKENS
    assert keepalive["max_tokens"] > 1
    assert keepalive["tool_choice"] == "none"


def test_glm_family_thinking_keepalive_keeps_enabled():
    from core.cache_keepalive import (
        KEEPALIVE_THINKING_MODEL_MAX_TOKENS,
        build_keepalive_request,
    )

    original = {
        "model": "openai/glm-5.2",
        "api_key": "secret",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "messages": [{"role": "system", "content": "stable"}],
        "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        "stream": True,
    }

    keepalive = build_keepalive_request(original)

    assert keepalive["extra_body"]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in keepalive["extra_body"]
    assert keepalive["max_tokens"] == KEEPALIVE_THINKING_MODEL_MAX_TOKENS
    assert keepalive["max_tokens"] > 1


def test_keepalive_timeout_scales_with_prompt_size():
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

    mid = build_keepalive_request({
        **base,
        "messages": [{"role": "user", "content": "字" * 200_000}],
    })
    assert mid["timeout"] == API_REQUEST_TIMEOUT_SECONDS + 200_000 // 1500

    huge = build_keepalive_request({
        **base,
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "字" * 1_000_000},
        ],
    })
    assert huge["timeout"] == API_REQUEST_MAX_TIMEOUT_SECONDS


def test_keepalive_retries_transient_failure_instead_of_stopping(monkeypatch):
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
        assert _wait_until(lambda: service.status()["enabled"] is False), "重试耗尽应停用该会话"
        assert len(calls) == 3
        assert service.status()["error"] == "_Timeout"
        assert service.status()["session_count"] == 0
    finally:
        service.register_request({}, interval=0)


def test_keepalive_retry_backoff_yields_to_new_registration(monkeypatch):
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


def test_session_key_falls_back_to_telemetry_then_default(monkeypatch):
    import core.cache_keepalive as ka

    monkeypatch.setattr(ka, "load_keepalive_interval", lambda: 120)

    service = ka.CacheKeepaliveService()
    try:
        service.register_request(
            _keepalive_request("from-telemetry"),
            interval=60,
            telemetry={"session_id": "session-x"},
        )
        assert set(service._entries) == {"session-x"}
        service.register_request(
            _keepalive_request("explicit"),
            interval=60,
            telemetry={"session_id": "session-x"},
            session_key="session-y",
        )
        assert set(service._entries) == {"session-x", "session-y"}
        service.register_request(_keepalive_request("legacy"))
        assert set(service._entries) == {
            "session-x",
            "session-y",
            ka.DEFAULT_KEEPALIVE_SESSION_KEY,
        }
        assert service.status()["interval"] == 120
        service.register_request({}, interval=0)
        assert set(service._entries) == {"session-x", "session-y"}
    finally:
        _drop_sessions(
            service, "session-x", "session-y", ka.DEFAULT_KEEPALIVE_SESSION_KEY
        )


def test_multi_session_keepalive_tracks_each_session(monkeypatch):
    import core.cache_keepalive as ka

    dispatched = []

    def fake_completion(**kwargs):
        dispatched.append(kwargs["messages"][0]["content"])
        return "ok-response"

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)
    monkeypatch.setattr(
        ka, "account_keepalive_response", lambda response, telemetry: {"valid": True}
    )

    service = ka.CacheKeepaliveService()
    try:
        service.register_request(
            _keepalive_request("sess-a"), interval=60, session_key="sess-a"
        )
        service.register_request(
            _keepalive_request("sess-b"), interval=60, session_key="sess-b"
        )
        assert service.status()["session_count"] == 2
        _fire_immediately(service)
        assert _wait_until(
            lambda: set(dispatched) == {"sess-a", "sess-b"}
        ), "两个会话都应收到保活请求"
        assert service.status()["enabled"] is True
    finally:
        _drop_sessions(service, "sess-a", "sess-b")


def test_stopping_one_session_keeps_other_sessions_alive(monkeypatch):
    import time as _time
    import core.cache_keepalive as ka

    dispatched = []

    def fake_completion(**kwargs):
        dispatched.append(kwargs["messages"][0]["content"])
        return "ok-response"

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)
    monkeypatch.setattr(
        ka, "account_keepalive_response", lambda response, telemetry: {"valid": True}
    )

    service = ka.CacheKeepaliveService()
    try:
        service.register_request(
            _keepalive_request("sess-a"), interval=60, session_key="sess-a"
        )
        service.register_request(
            _keepalive_request("sess-b"), interval=60, session_key="sess-b"
        )
        service.register_request({}, interval=0, session_key="sess-a")
        assert service.status()["session_count"] == 1
        _fire_immediately(service)
        assert _wait_until(lambda: dispatched == ["sess-b"]), "sess-b 仍应被保活"
        _time.sleep(0.2)
        assert dispatched == ["sess-b"], "sess-a 已停用，不应再发保活请求"
        assert service.status()["enabled"] is True
    finally:
        _drop_sessions(service, "sess-a", "sess-b")


def test_session_limit_evicts_oldest(monkeypatch):
    import core.cache_keepalive as ka

    assert ka.MAX_KEEPALIVE_SESSIONS == 4
    keys = [f"sess-{index}" for index in range(ka.MAX_KEEPALIVE_SESSIONS + 1)]

    service = ka.CacheKeepaliveService()
    try:
        for key in keys:
            service.register_request(
                _keepalive_request(key), interval=60, session_key=key
            )
        assert set(service._entries) == set(keys[1:]), "最旧的会话应被淘汰"
        assert service.status()["session_count"] == ka.MAX_KEEPALIVE_SESSIONS
    finally:
        _drop_sessions(service, *keys)


def test_idle_session_is_pruned_from_loop(monkeypatch):
    import core.cache_keepalive as ka

    assert ka.KEEPALIVE_IDLE_EXPIRE_SECONDS == 30 * 60

    service = ka.CacheKeepaliveService()
    try:
        service.register_request(
            _keepalive_request("stale"), interval=60, session_key="stale"
        )
        service.register_request(
            _keepalive_request("fresh"), interval=60, session_key="fresh"
        )
        with service._condition:
            service._entries["stale"]["last_activity"] -= 3600
            service._condition.notify_all()
        assert _wait_until(
            lambda: "stale" not in service._entries
        ), "空闲超时的会话条目应被清理"
        assert "fresh" in service._entries
        assert service.status()["session_count"] == 1
    finally:
        _drop_sessions(service, "stale", "fresh")


def test_failing_session_does_not_stop_other_sessions(monkeypatch):
    import core.cache_keepalive as ka

    class _Timeout(Exception):
        pass

    dispatched = []

    def fake_completion(**kwargs):
        content = kwargs["messages"][0]["content"]
        if content == "sess-bad":
            raise _Timeout("keepalive timed out")
        dispatched.append(content)
        return "ok-response"

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)
    monkeypatch.setattr(
        ka, "account_keepalive_response", lambda response, telemetry: {"valid": True}
    )
    monkeypatch.setattr(ka, "KEEPALIVE_RETRY_BACKOFF_SECONDS", 0.01)

    service = ka.CacheKeepaliveService()
    try:
        service.register_request(
            _keepalive_request("sess-bad"), interval=60, session_key="sess-bad"
        )
        service.register_request(
            _keepalive_request("sess-good"), interval=60, session_key="sess-good"
        )
        _fire_immediately(service)
        assert _wait_until(
            lambda: dispatched == ["sess-good"]
        ), "健康会话的保活不能被失败会话拖垮"
        assert _wait_until(
            lambda: set(service._entries) == {"sess-good"}
        ), "失败会话只停自己（连续 3 次尝试后移除该条目）"
        assert service.status()["enabled"] is True
        assert service.status()["session_count"] == 1
        assert service.status()["error"] == ""
    finally:
        _drop_sessions(service, "sess-bad", "sess-good")


_TASK5_MODEL = "openai/deepseek-v4.1-flash"


def _expire_keepalive_evidence_cache():
    import core.llm_diagnostics as diagnostics

    with diagnostics._keepalive_lane_evidence_lock:
        diagnostics._keepalive_lane_evidence_cache["expire_at"] = 0.0
        diagnostics._keepalive_lane_evidence_cache["lanes"] = {}


def _reset_keepalive_evidence_state():
    import core.llm_dialects as dialects

    with dialects._keepalive_evidence_disabled_lock:
        dialects._keepalive_evidence_disabled_routes.clear()
    with dialects._qwen_cache_cfg_lock:
        dialects._keepalive_routes_cfg_cache["entry"] = None
    _expire_keepalive_evidence_cache()


@pytest.fixture(autouse=True)
def _isolate_keepalive_evidence():
    import core.llm_diagnostics as diagnostics

    with diagnostics.token_usage_lock:
        before = set(diagnostics._cache_usage_states)
    _reset_keepalive_evidence_state()
    yield
    _reset_keepalive_evidence_state()
    with diagnostics.token_usage_lock:
        leaked = [
            key for key in diagnostics._cache_usage_states if key not in before
        ]
        for key in leaked:
            del diagnostics._cache_usage_states[key]


def _stub_config(monkeypatch, data):
    import core.llm_dialects as dialects

    monkeypatch.setattr("core.model_manager.load_config", lambda: data)
    with dialects._qwen_cache_cfg_lock:
        dialects._keepalive_routes_cfg_cache["entry"] = None


def _seed_keepalive_lane(session, model, api_base, *, turns, prompt_total, cached_total):
    import core.llm_diagnostics as diagnostics

    key = diagnostics._cache_usage_key(session, model, api_base, "keepalive")
    with diagnostics.token_usage_lock:
        diagnostics._cache_usage_states[key] = {
            "cumulative_prompt": prompt_total,
            "cumulative_cached": cached_total,
            "hot_prompt": prompt_total,
            "hot_cached": cached_total,
            "hot_turns": turns,
            "latest_prompt": prompt_total,
            "latest_cached": cached_total,
            "usage_valid": True,
            "usage_error": "",
            "invalidation_reason": "",
            "turns": turns,
        }
    _expire_keepalive_evidence_cache()


def test_keepalive_lane_evidence_aggregates_across_sessions():
    from core.llm_diagnostics import lane_keepalive_evidence

    base = "https://task5-aggregate.example/v1"
    other_base = "https://task5-aggregate-other.example/v1"
    _seed_keepalive_lane(
        "task5-aggregate-a", _TASK5_MODEL, base,
        turns=60, prompt_total=4_979_460, cached_total=1_196_936,
    )
    _seed_keepalive_lane(
        "task5-aggregate-b", _TASK5_MODEL, base,
        turns=58, prompt_total=4_813_474, cached_total=1_156_984,
    )
    _seed_keepalive_lane(
        "task5-aggregate-other", _TASK5_MODEL, other_base,
        turns=5, prompt_total=5_000, cached_total=5_000,
    )

    turns, prompt_tokens, cached_tokens, hit_rate = lane_keepalive_evidence(
        _TASK5_MODEL, base
    )

    assert turns == 118
    assert prompt_tokens == 9_792_934
    assert cached_tokens == 2_353_920
    assert hit_rate == pytest.approx(2_353_920 / 9_792_934)
    assert 0.2403 < hit_rate < 0.2405
    assert lane_keepalive_evidence(_TASK5_MODEL, other_base)[:3] == (5, 5_000, 5_000)


def test_aliyun_keepalive_lane_118_turns_2404_stops(monkeypatch):
    from core.llm import _supports_cache_keepalive

    base = "https://task5-aliyun.example/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-aliyun", _TASK5_MODEL, base,
        turns=118, prompt_total=9_792_934, cached_total=2_353_920,
    )

    assert _supports_cache_keepalive(_TASK5_MODEL, base) is False


def test_tokenrhythm_keepalive_lane_61_turns_9015_stays(monkeypatch):
    from core.llm import _supports_cache_keepalive

    base = "https://task5-tokenrhythm.example/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-tokenrhythm", _TASK5_MODEL, base,
        turns=61, prompt_total=3_368_268, cached_total=3_036_416,
    )

    assert _supports_cache_keepalive(_TASK5_MODEL, base) is True


def test_kimi_keepalive_lane_746_turns_9207_stays(monkeypatch):
    from core.llm import _supports_cache_keepalive

    base = "https://api.kimi.com/coding/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-kimi", "openai/k3", base,
        turns=746, prompt_total=135_118_794, cached_total=124_404_334,
    )

    assert _supports_cache_keepalive("openai/k3", base) is True


def test_keepalive_sample_fuse_9_turns_zero_hit_never_stops(monkeypatch):
    from core.llm import _supports_cache_keepalive

    base = "https://task5-fuse.example/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-fuse", _TASK5_MODEL, base,
        turns=9, prompt_total=9_000, cached_total=0,
    )

    assert _supports_cache_keepalive(_TASK5_MODEL, base) is True


def test_keepalive_boundary_exactly_10_turns_below_half_stops(monkeypatch):
    from core.llm import _supports_cache_keepalive
    from core.llm_diagnostics import lane_keepalive_evidence

    base = "https://task5-boundary.example/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-boundary", _TASK5_MODEL, base,
        turns=10, prompt_total=10_000, cached_total=4_990,
    )

    assert lane_keepalive_evidence(_TASK5_MODEL, base)[3] == pytest.approx(0.499)
    assert _supports_cache_keepalive(_TASK5_MODEL, base) is False


def test_keepalive_mature_lane_at_half_hit_rate_stays(monkeypatch):
    from core.llm import _supports_cache_keepalive

    base = "https://task5-half.example/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-half", _TASK5_MODEL, base,
        turns=10, prompt_total=10_000, cached_total=5_000,
    )

    assert _supports_cache_keepalive(_TASK5_MODEL, base) is True


def _legacy_supports_cache_keepalive(model_name, api_base):
    from core.llm_dialects import (
        _is_deepseek_family,
        _is_glm_family,
        _is_kimi_family,
        _is_qwen_family,
        _keepalive_route_explicitly_enabled,
    )

    return (
        _is_kimi_family(model_name, api_base)
        or _is_deepseek_family(model_name, api_base)
        or _is_glm_family(model_name, api_base)
        or _is_qwen_family(model_name, api_base)
        or _keepalive_route_explicitly_enabled(api_base)
    )


@pytest.mark.parametrize(
    "model,api_base",
    [
        ("openai/deepseek-v4.1-flash", "https://api.deepseek.com"),
        ("openai/deepseek-v4.1-flash", "https://tokenrhythm.studio/v1"),
        (
            "openai/deepseek-v4.1-flash",
            "https://task5-matrix.maas.aliyuncs.com/compatible-mode/v1",
        ),
        ("openai/kimi-k2.6", "https://api.moonshot.cn/v1"),
        ("openai/k3", "https://api.kimi.com/coding/v1"),
        ("openai/glm-5.3", "https://open.bigmodel.cn/api/paas/v4"),
        (
            "openai/qwen3.8-flash",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        ("openai/qwen3.8-flash", "https://task5-relay.example/v1"),
        ("openai/some-model", "https://task5-relay.example/v1"),
    ],
)
def test_evidence_gate_healthy_lanes_match_legacy_verdict(model, api_base, monkeypatch):
    from core.llm import _supports_cache_keepalive

    _stub_config(monkeypatch, {})

    assert _supports_cache_keepalive(model, api_base) is _legacy_supports_cache_keepalive(
        model, api_base
    )


def test_evidence_gate_disabled_ignores_loss_lane(monkeypatch):
    from core.llm import _supports_cache_keepalive

    base = "https://task5-gate-off.example/v1"
    _stub_config(monkeypatch, {"cache_keepalive_evidence_gate": False})
    _seed_keepalive_lane(
        "task5-gate-off", _TASK5_MODEL, base,
        turns=118, prompt_total=9_792_934, cached_total=2_353_920,
    )

    assert _supports_cache_keepalive(_TASK5_MODEL, base) is True


def test_evidence_gate_defaults_enabled_when_config_key_absent(monkeypatch):
    import core.llm_dialects as dialects

    _stub_config(monkeypatch, {})

    assert dialects._keepalive_evidence_gate_enabled() is True


def test_keepalive_stop_ttl_is_hour_scale_and_reescalates(monkeypatch):
    import time as _time
    import core.llm_dialects as dialects
    from core.llm import _supports_cache_keepalive

    assert dialects._KEEPALIVE_EVIDENCE_DISABLED_TTL_SECONDS >= 3600

    base = "https://task5-ttl.example/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-ttl", _TASK5_MODEL, base,
        turns=12, prompt_total=12_000, cached_total=1_200,
    )

    assert _supports_cache_keepalive(_TASK5_MODEL, base) is False
    key = dialects._keepalive_evidence_route_key(_TASK5_MODEL, base)
    with dialects._keepalive_evidence_disabled_lock:
        remaining = dialects._keepalive_evidence_disabled_routes[key] - _time.monotonic()
    assert 3599 <= remaining <= 3601

    with dialects._keepalive_evidence_disabled_lock:
        dialects._keepalive_evidence_disabled_routes[key] = _time.monotonic() - 1
    assert _supports_cache_keepalive(_TASK5_MODEL, base) is False
    with dialects._keepalive_evidence_disabled_lock:
        refreshed = dialects._keepalive_evidence_disabled_routes[key] - _time.monotonic()
    assert 3599 <= refreshed <= 3601


def test_keepalive_stop_log_records_lane_digits_only(monkeypatch, caplog):
    import logging
    from core.llm import _supports_cache_keepalive

    base = "https://task5-log.example/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-log", _TASK5_MODEL, base,
        turns=12, prompt_total=12_000, cached_total=1_200,
    )

    with caplog.at_level(logging.WARNING, logger="core.llm_dialects"):
        assert _supports_cache_keepalive(_TASK5_MODEL, base) is False

    stops = [
        record.getMessage()
        for record in caplog.records
        if record.name == "core.llm_dialects"
        and "保活" in record.getMessage()
        and base in record.getMessage()
    ]
    assert len(stops) == 1, "止损只记一条"
    log_line = stops[0]
    assert _TASK5_MODEL in log_line
    assert "turns=12" in log_line
    assert "hit_rate=0.1000" in log_line
    assert "at=" in log_line
    assert "ttl_seconds=3600" in log_line
    # 只记数字与键名：绝不记录任何消息正文/提示词内容
    assert "messages" not in log_line
    assert "content" not in log_line
    assert "prompt" not in log_line


def test_keepalive_evidence_isolates_main_scope_low_hit(monkeypatch):
    import core.llm as llm
    import core.llm_diagnostics as diagnostics

    base = "https://task5-scope.example/v1"
    _stub_config(monkeypatch, {})
    for _ in range(30):
        llm._record_cache_usage("task5-scope", "main", 1_000, 100, "", _TASK5_MODEL, base)

    lane_buckets = [
        key
        for key in diagnostics._cache_usage_states
        if key[1] == _TASK5_MODEL and key[2] == base and key[3] == "keepalive"
    ]
    assert lane_buckets == []
    assert llm._supports_cache_keepalive(_TASK5_MODEL, base) is True


def test_record_keepalive_lane_usage_accumulates_turns_and_tokens():
    from core.llm_diagnostics import (
        lane_keepalive_evidence,
        record_keepalive_lane_usage,
    )

    base = "https://task5-record.example/v1"
    for index in range(12):
        record_keepalive_lane_usage(
            "task5-record", _TASK5_MODEL, base, 1_000, 100 if index < 3 else 900
        )

    turns, prompt_tokens, cached_tokens, hit_rate = lane_keepalive_evidence(
        _TASK5_MODEL, base
    )
    assert turns == 12
    assert prompt_tokens == 12_000
    assert cached_tokens == 3 * 100 + 9 * 900
    assert hit_rate == pytest.approx(8_400 / 12_000)


def test_account_keepalive_response_feeds_lane_evidence(monkeypatch):
    from types import SimpleNamespace
    from core.cache_keepalive import account_keepalive_response
    from core.llm import _supports_cache_keepalive
    from core.llm_diagnostics import lane_keepalive_evidence

    base = "https://task5-link.example/v1"
    _stub_config(monkeypatch, {})
    monkeypatch.setattr(
        "core.llm._account_server_usage",
        lambda usage, **kwargs: {
            "valid": True,
            "cache_reported": True,
            "prompt_tokens": 1_000,
            "cached_tokens": 100,
        },
    )

    for _ in range(10):
        account_keepalive_response(
            SimpleNamespace(usage=SimpleNamespace()),
            {"model_name": _TASK5_MODEL, "api_base": base, "session_id": "task5-link"},
        )

    assert lane_keepalive_evidence(_TASK5_MODEL, base) == (
        10, 10_000, 1_000, pytest.approx(0.1),
    )
    assert _supports_cache_keepalive(_TASK5_MODEL, base) is False


def test_account_keepalive_response_without_cache_report_skips_evidence(monkeypatch):
    from types import SimpleNamespace
    from core.cache_keepalive import account_keepalive_response
    from core.llm_diagnostics import lane_keepalive_evidence

    base = "https://task5-unreported.example/v1"
    _stub_config(monkeypatch, {})
    monkeypatch.setattr(
        "core.llm._account_server_usage",
        lambda usage, **kwargs: {"valid": True},
    )

    account_keepalive_response(
        SimpleNamespace(usage=SimpleNamespace()),
        {"model_name": _TASK5_MODEL, "api_base": base, "session_id": "task5-unreported"},
    )

    assert lane_keepalive_evidence(_TASK5_MODEL, base) == (0, 0, 0, None)


def test_keepalive_evidence_cache_ttl_is_short_window(monkeypatch):
    from core.llm_diagnostics import (
        _KEEPALIVE_LANE_EVIDENCE_TTL_SECONDS,
        lane_keepalive_evidence,
        record_keepalive_lane_usage,
    )

    assert 30 <= _KEEPALIVE_LANE_EVIDENCE_TTL_SECONDS <= 60

    base = "https://task5-cache.example/v1"
    _stub_config(monkeypatch, {})
    record_keepalive_lane_usage("task5-cache", _TASK5_MODEL, base, 1_000, 1_000)
    first = lane_keepalive_evidence(_TASK5_MODEL, base)
    assert first == (1, 1_000, 1_000, pytest.approx(1.0))

    record_keepalive_lane_usage("task5-cache", _TASK5_MODEL, base, 1_000, 0)
    assert lane_keepalive_evidence(_TASK5_MODEL, base) == first

    _expire_keepalive_evidence_cache()
    assert lane_keepalive_evidence(_TASK5_MODEL, base) == (
        2, 2_000, 1_000, pytest.approx(0.5),
    )


_TASK5_SEED_MODEL_LEAF = "deepseek-v4.1-flash"


def _clear_keepalive_lane(model, api_base):
    import core.llm_diagnostics as diagnostics

    target = (str(model or "").lower(), str(api_base or "").rstrip("/").lower())
    with diagnostics.token_usage_lock:
        for key in [
            k for k in diagnostics._cache_usage_states
            if str(k[3]) == "keepalive" and (k[1], k[2]) == target
        ]:
            del diagnostics._cache_usage_states[key]
    _expire_keepalive_evidence_cache()


def _persist_keepalive_turns(manager, api_base, *, turns, prompt, cached, ts_list=None,
                            session_prefix="persisted"):
    import time as _time

    now = int(_time.time())
    for index in range(turns):
        manager.add_token_usage(
            "openai", _TASK5_SEED_MODEL_LEAF, prompt, 5,
            None if cached is None else cached, prompt + 5,
            session_id=f"{session_prefix}-{index}", api_base=api_base,
            cache_scope="keepalive",
            ts=(ts_list[index] if ts_list else now - 3600 + index),
        )


def test_seed_restores_loss_evidence_after_process_restart(tmp_path, monkeypatch):
    import time as _time

    import core.app_context as app_context
    import core.llm_diagnostics as diagnostics
    from core.memory import MemoryManager

    base = "https://task5-restart.example/v1"
    manager = MemoryManager(tmp_path / "seed.db")
    now = int(_time.time())
    _persist_keepalive_turns(manager, base, turns=12, prompt=100_000, cached=20_000)

    monkeypatch.setattr(app_context, "get_manager", lambda: manager)
    _clear_keepalive_lane(_TASK5_MODEL, base)
    diagnostics._keepalive_lane_evidence_seeded = False

    try:
        assert diagnostics.lane_keepalive_evidence(_TASK5_MODEL, base)[0] == 0, "播种前内存应为空"
        assert diagnostics.seed_keepalive_lane_evidence_from_store(before_ts=now + 10) == 1

        turns, prompt, cached, hit_rate = diagnostics.lane_keepalive_evidence(
            _TASK5_MODEL, base
        )
        assert turns == 12
        assert prompt == 1_200_000 and cached == 240_000
        assert hit_rate == pytest.approx(0.2)

        import core.llm_dialects as dialects

        _stub_config(monkeypatch, {})
        dialects._keepalive_evidence_disabled_routes.clear()
        from core.llm import _supports_cache_keepalive

        assert _supports_cache_keepalive(_TASK5_MODEL, base) is False, "重启后应立即止损"
    finally:
        _clear_keepalive_lane(_TASK5_MODEL, base)
        diagnostics._keepalive_lane_evidence_seeded = False


def test_seed_skips_current_process_rows_and_unreported_turns(tmp_path, monkeypatch):
    import time as _time

    import core.app_context as app_context
    import core.llm_diagnostics as diagnostics
    from core.memory import MemoryManager

    base = "https://task5-seed-window.example/v1"
    manager = MemoryManager(tmp_path / "seed2.db")
    now = int(_time.time())
    process_start = now - 60
    _persist_keepalive_turns(
        manager, base, turns=12, prompt=100_000, cached=20_000,
        ts_list=[process_start - 120 + i for i in range(12)],
        session_prefix="prev-process",
    )
    _persist_keepalive_turns(
        manager, base, turns=8, prompt=100_000, cached=90_000,
        ts_list=[process_start + 10 + i for i in range(8)],
        session_prefix="this-process",
    )
    # 未报告缓存明细的行：绝不入分母
    _persist_keepalive_turns(
        manager, base, turns=3, prompt=100_000, cached=None,
        ts_list=[process_start - 200 - i for i in range(3)],
        session_prefix="unreported",
    )

    monkeypatch.setattr(app_context, "get_manager", lambda: manager)
    _clear_keepalive_lane(_TASK5_MODEL, base)
    diagnostics._keepalive_lane_evidence_seeded = False

    try:
        diagnostics.seed_keepalive_lane_evidence_from_store(before_ts=process_start)
        turns, prompt, cached, hit_rate = diagnostics.lane_keepalive_evidence(
            _TASK5_MODEL, base
        )
        assert turns == 12, "只播上一个进程的 12 轮，跳过本进程 8 轮与未报告 3 轮"
        assert prompt == 1_200_000 and cached == 240_000
        assert hit_rate == pytest.approx(0.2)
    finally:
        _clear_keepalive_lane(_TASK5_MODEL, base)
        diagnostics._keepalive_lane_evidence_seeded = False


def test_seed_is_idempotent_and_keeps_per_session_view_clean(tmp_path, monkeypatch):
    import time as _time

    import core.app_context as app_context
    import core.llm_diagnostics as diagnostics
    from core.memory import MemoryManager

    base = "https://task5-seed-idempotent.example/v1"
    manager = MemoryManager(tmp_path / "seed3.db")
    now = int(_time.time())
    _persist_keepalive_turns(manager, base, turns=11, prompt=50_000, cached=10_000)

    monkeypatch.setattr(app_context, "get_manager", lambda: manager)
    _clear_keepalive_lane(_TASK5_MODEL, base)
    diagnostics._keepalive_lane_evidence_seeded = False

    try:
        assert diagnostics.seed_keepalive_lane_evidence_from_store(before_ts=now + 10) == 1
        again = diagnostics.seed_keepalive_lane_evidence_from_store(before_ts=now + 10)
        assert again == 0, "默认不重复播种"
        assert diagnostics.lane_keepalive_evidence(_TASK5_MODEL, base)[0] == 11

        snapshot = diagnostics.get_token_usage_snapshot(
            "task5-real-session", _TASK5_MODEL, base, "main"
        )
        assert "keepalive" not in snapshot["cache_scopes"], "播种不得冒充真实会话"
    finally:
        _clear_keepalive_lane(_TASK5_MODEL, base)
        diagnostics._keepalive_lane_evidence_seeded = False
