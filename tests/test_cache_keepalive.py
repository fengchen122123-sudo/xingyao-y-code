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


def _fire_immediately(service, session_key=None):
    """把已注册会话条目的 deadline 回拨到过去并唤醒线程，立即触发一次保活。

    session_key 为 None 时对所有会话条目生效（线程每轮挑最早到期的一个执行）。
    """
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
    """最小可用保活请求；tag 落在 messages 里便于断言是哪个会话被保活。"""
    return {
        "model": "openai/qwen3.8-flash",
        "messages": [{"role": "user", "content": tag}],
    }


@pytest.fixture(autouse=True)
def _clear_route_config_caches():
    """清空路由配置的指纹缓存，避免测试替身 load_config 的结论跨测试残留。

    _configured_keepalive_routes / _qwen_cache 系列按 (load_config 身份, 配置
    文件 mtime) 失效缓存；monkeypatch 换替身后指纹虽已变化，但缓存条目会持有
    替身函数的引用，这里统一清空以保证测试间完全隔离。
    """
    import core.llm_dialects as dialects

    def _clear():
        with dialects._qwen_cache_cfg_lock:
            dialects._keepalive_routes_cfg_cache["entry"] = None
            dialects._qwen_cache_cfg_cache.clear()

    _clear()
    yield
    _clear()


def _drop_sessions(service, *session_keys):
    """测试收尾：停掉指定会话的保活，避免线程残留在后续测试里继续发请求。"""
    for session_key in session_keys:
        service.register_request({}, interval=0, session_key=session_key)


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
    # 显式传 interval 的调用点（register_request）同样拒绝过短间隔
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
    """DeepSeek 家族任意渠道放行保活：官方与第三方中转一致。"""
    from core.llm import _supports_cache_keepalive

    assert _supports_cache_keepalive(
        "openai/deepseek-v4-pro", "https://api.deepseek.com"
    ) is True
    assert _supports_cache_keepalive(
        "openai/deepseek-v4-pro", "https://aggregator.example/v1"
    ) is True


def test_kimi_keepalive_covers_third_party_routes(monkeypatch):
    """Kimi 家族任意渠道放行保活：官方与第三方中转一致。"""
    from core.llm import _supports_cache_keepalive

    monkeypatch.setattr("core.model_manager.load_config", lambda: {})

    assert _supports_cache_keepalive(
        "openai/k3-256k", "https://api.kimi.com/coding/v1"
    ) is True
    assert _supports_cache_keepalive(
        "openai/kimi-k2.6", "https://tokenrhythm.studio/v1"
    ) is True


def test_keepalive_routes_config_enables_explicit_gateway_routes(monkeypatch):
    """顶层 cache_keepalive_routes 对非四族模型的显式通道：声明后该域名放行。"""
    from core.llm import _supports_cache_keepalive

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"cache_keepalive_routes": ["tokenrhythm.studio"]},
    )

    # 裸域名条目命中带路径的 api_base（/v1 后缀不影响域名归一化）
    assert _supports_cache_keepalive(
        "openai/some-model", "https://tokenrhythm.studio/v1"
    ) is True
    # 无路径、大小写与端口变体同样命中
    assert _supports_cache_keepalive(
        "openai/some-model", "https://TokenRhythm.Studio"
    ) is True
    assert _supports_cache_keepalive(
        "openai/some-model", "https://tokenrhythm.studio:8443/v1"
    ) is True
    # 未在配置中声明的其他网关路由不受影响
    assert _supports_cache_keepalive(
        "openai/some-model", "https://aggregator.example/v1"
    ) is False

    # 条目写成完整 URL（带路径）走同一归一化，同样命中
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
    """GLM 家族任意渠道放行保活：官方端点、未知路径与第三方中转一视同仁。"""
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
    # 家族判定不看 URL 形状：官方域名未知路径、第三方中转的 GLM 模型一律放行
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
    """重试耗尽（1 次首发 + 2 次退避重试）仍失败才停用该会话的保活。"""
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


def test_session_key_falls_back_to_telemetry_then_default(monkeypatch):
    """会话键解析：显式 session_key > telemetry.session_id > 固定默认键。

    默认键保证旧调用方（只传 request/interval/telemetry）行为不变：
    不带 telemetry 的 register_request 只影响默认键条目，不误伤会话条目。"""
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
        # 显式 session_key 优先于 telemetry.session_id
        service.register_request(
            _keepalive_request("explicit"),
            interval=60,
            telemetry={"session_id": "session-x"},
            session_key="session-y",
        )
        assert set(service._entries) == {"session-x", "session-y"}
        # 旧调用方（无 session_key / 无 telemetry）落到固定默认键，间隔走配置默认值
        service.register_request(_keepalive_request("legacy"))
        assert set(service._entries) == {
            "session-x",
            "session-y",
            ka.DEFAULT_KEEPALIVE_SESSION_KEY,
        }
        assert service.status()["interval"] == 120
        # 默认键的 interval=0 只停默认键条目，会话条目照旧
        service.register_request({}, interval=0)
        assert set(service._entries) == {"session-x", "session-y"}
    finally:
        _drop_sessions(
            service, "session-x", "session-y", ka.DEFAULT_KEEPALIVE_SESSION_KEY
        )


def test_multi_session_keepalive_tracks_each_session(monkeypatch):
    """多会话各自注册快照后，每个会话都应被保活（不再只覆盖最后一个会话）。

    单快照实现的 bug：register_request 覆盖全局快照，只有最后注册的会话被续
    TTL；实测 19 个会话、920 次切换/天下，间隔 > 5 分钟切回时 28% 的请求
    命中率 < 50%，而带保活的官方路由切回命中率 96.2%。"""
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
    """主请求开始时只停本会话快照：其他会话的保活继续（旧实现清空全局快照）。"""
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
        # 模拟主模型请求开始：只停 sess-a
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
    """超过 MAX_KEEPALIVE_SESSIONS 时淘汰 last_activity 最旧的条目。"""
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
    """last_activity 超过 KEEPALIVE_IDLE_EXPIRE_SECONDS 的条目在循环中被清理。"""
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
        # 把 stale 会话回拨到 1 小时前（超过 30 分钟空闲），唤醒线程触发清理
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
    """某会话连续失败只停用该会话；其他会话的保活不受影响（旧实现全局自停）。"""
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


# ---------------------------------------------------------------------------
# 跨渠道缓存命中率修复 · Task 5：保活按车道证据止损
#
# 规则（写死）：
# - 双条件同时满足才停：该车道 cache_scope='keepalive' 的独立 token 加权命中率
#   < 0.5 且轮次 >= 10；
# - 样本不足绝不动作（R3 保险丝）：轮次 < 10 时一律不停，哪怕命中率 0%；
# - 健康车道行为零变化；开关 cache_keepalive_evidence_gate=false 时行为与
#   改动前逐字等价；
# - 止损 TTL 小时级（>= 3600s，不用 900s 的 400 拒绝档）；
# - 止损日志只记车道标识与数字（turns / hit_rate / 判定时间），绝不记正文。
# ---------------------------------------------------------------------------

_TASK5_MODEL = "openai/deepseek-v4.1-flash"


def _expire_keepalive_evidence_cache():
    """强制下次聚合重读（短 TTL 缓存是生产优化，测试需可控失效）。"""
    import core.llm_diagnostics as diagnostics

    with diagnostics._keepalive_lane_evidence_lock:
        diagnostics._keepalive_lane_evidence_cache["expire_at"] = 0.0
        diagnostics._keepalive_lane_evidence_cache["lanes"] = {}


def _reset_keepalive_evidence_state():
    """清空 Task 5 新增的全部进程内状态：止损表、开关缓存、聚合缓存。"""
    import core.llm_dialects as dialects

    with dialects._keepalive_evidence_disabled_lock:
        dialects._keepalive_evidence_disabled_routes.clear()
    with dialects._qwen_cache_cfg_lock:
        # 开关与保活白名单共用同一份配置解析条目，清它即同时失效两者。
        dialects._keepalive_routes_cfg_cache["entry"] = None
    _expire_keepalive_evidence_cache()


@pytest.fixture(autouse=True)
def _isolate_keepalive_evidence():
    """Task 5 状态隔离：测试前后清空止损表/开关缓存/聚合缓存，并回收测试桶。"""
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
    """替换 load_config 并清掉开关指纹缓存，保证读到替身配置。"""
    import core.llm_dialects as dialects

    monkeypatch.setattr("core.model_manager.load_config", lambda: data)
    with dialects._qwen_cache_cfg_lock:
        # 开关与保活白名单共用同一份配置解析条目，清它即同时失效两者。
        dialects._keepalive_routes_cfg_cache["entry"] = None


def _seed_keepalive_lane(session, model, api_base, *, turns, prompt_total, cached_total):
    """按桶形状注入 keepalive 车道证据（轮次 + 累计 prompt/cached），并失效聚合缓存。"""
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
    """跨会话聚合：同 (model, api_base) 的 keepalive 桶合计轮次与 token（他车道隔离）。

    阿里云形状的两个会话切片：60 轮 + 58 轮 = 118 轮，
    prompt 9,792,934 / cached 2,353,920（24.04%）。
    """
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
    # 另一车道（同模型、不同 api_base）不得混入
    assert lane_keepalive_evidence(_TASK5_MODEL, other_base)[:3] == (5, 5_000, 5_000)


def test_aliyun_keepalive_lane_118_turns_2404_stops(monkeypatch):
    """阿里云形状应停：118 轮、24.04% → 该车道保活停（False）。"""
    from core.llm import _supports_cache_keepalive

    base = "https://task5-aliyun.example/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-aliyun", _TASK5_MODEL, base,
        turns=118, prompt_total=9_792_934, cached_total=2_353_920,
    )

    assert _supports_cache_keepalive(_TASK5_MODEL, base) is False


def test_tokenrhythm_keepalive_lane_61_turns_9015_stays(monkeypatch):
    """基元律动形状应留：61 轮、90.15% → 保活保留（True）。"""
    from core.llm import _supports_cache_keepalive

    base = "https://task5-tokenrhythm.example/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-tokenrhythm", _TASK5_MODEL, base,
        turns=61, prompt_total=3_368_268, cached_total=3_036_416,
    )

    assert _supports_cache_keepalive(_TASK5_MODEL, base) is True


def test_kimi_keepalive_lane_746_turns_9207_stays(monkeypatch):
    """api.kimi.com 形状应留：746 轮、92.07% → 保活保留（True）。"""
    from core.llm import _supports_cache_keepalive

    base = "https://api.kimi.com/coding/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-kimi", "openai/k3", base,
        turns=746, prompt_total=135_118_794, cached_total=124_404_334,
    )

    assert _supports_cache_keepalive("openai/k3", base) is True


def test_keepalive_sample_fuse_9_turns_zero_hit_never_stops(monkeypatch):
    """保险丝（R3）：9 轮、0% 命中 → 样本不足，绝不停（True）。"""
    from core.llm import _supports_cache_keepalive

    base = "https://task5-fuse.example/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-fuse", _TASK5_MODEL, base,
        turns=9, prompt_total=9_000, cached_total=0,
    )

    assert _supports_cache_keepalive(_TASK5_MODEL, base) is True


def test_keepalive_boundary_exactly_10_turns_below_half_stops(monkeypatch):
    """双条件边界：恰好 10 轮、49.9% → 停（False）。"""
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
    """双条件：>=10 轮但命中率 >= 0.5 → 不停（True）。"""
    from core.llm import _supports_cache_keepalive

    base = "https://task5-half.example/v1"
    _stub_config(monkeypatch, {})
    _seed_keepalive_lane(
        "task5-half", _TASK5_MODEL, base,
        turns=10, prompt_total=10_000, cached_total=5_000,
    )

    assert _supports_cache_keepalive(_TASK5_MODEL, base) is True


def _legacy_supports_cache_keepalive(model_name, api_base):
    """改动前的判据（四族 ∨ 显式白名单）——用于逐字等价对照。"""
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
    """健康车道零变化：门开着且无亏损证据时，返回值与改动前判据逐字等价。"""
    from core.llm import _supports_cache_keepalive

    _stub_config(monkeypatch, {})

    assert _supports_cache_keepalive(model, api_base) is _legacy_supports_cache_keepalive(
        model, api_base
    )


def test_evidence_gate_disabled_ignores_loss_lane(monkeypatch):
    """开关 false：同样的亏损车道 → 门被关掉（True，行为与改动前等价）。"""
    from core.llm import _supports_cache_keepalive

    base = "https://task5-gate-off.example/v1"
    _stub_config(monkeypatch, {"cache_keepalive_evidence_gate": False})
    _seed_keepalive_lane(
        "task5-gate-off", _TASK5_MODEL, base,
        turns=118, prompt_total=9_792_934, cached_total=2_353_920,
    )

    assert _supports_cache_keepalive(_TASK5_MODEL, base) is True


def test_evidence_gate_defaults_enabled_when_config_key_absent(monkeypatch):
    """开关缺省 → 默认开（只可能停"已判定亏损"的车道）。"""
    import core.llm_dialects as dialects

    _stub_config(monkeypatch, {})

    assert dialects._keepalive_evidence_gate_enabled() is True


def test_keepalive_stop_ttl_is_hour_scale_and_reescalates(monkeypatch):
    """止损 TTL 小时级（>=3600，明确不用 900），到期后重判并重新计满。"""
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

    # 把条目拨到过期：重判（证据仍亏损）→ 再次止损，TTL 重新计满
    with dialects._keepalive_evidence_disabled_lock:
        dialects._keepalive_evidence_disabled_routes[key] = _time.monotonic() - 1
    assert _supports_cache_keepalive(_TASK5_MODEL, base) is False
    with dialects._keepalive_evidence_disabled_lock:
        refreshed = dialects._keepalive_evidence_disabled_routes[key] - _time.monotonic()
    assert 3599 <= refreshed <= 3601


def test_keepalive_stop_log_records_lane_digits_only(monkeypatch, caplog):
    """止损依据可观测：日志含车道标识、样本轮次、命中率与判定时间，且不含正文。"""
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
    """cache_scope 隔离：同车道 main scope 命中率再低也不得影响 keepalive 判定。"""
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
    """保活记账通道：每轮 +1 轮次并累计 token；12 轮 70% → 判定可复算。"""
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
    """保活响应记账成功后 → 车道证据累计；10 轮 10% 触发止损（False）。"""
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
    """未报告缓存明细的保活轮次不进车道证据（未报告 ≠ 0，R3 保险丝不受影响）。"""
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
    """聚合短 TTL 缓存（30~60s）：窗口内返回旧快照，过期后重扫。"""
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

    # TTL 窗口内再写一轮：缓存未过期，聚合仍返回旧快照
    record_keepalive_lane_usage("task5-cache", _TASK5_MODEL, base, 1_000, 0)
    assert lane_keepalive_evidence(_TASK5_MODEL, base) == first

    # 过期后重扫：两轮合计 2000/1000 = 50%
    _expire_keepalive_evidence_cache()
    assert lane_keepalive_evidence(_TASK5_MODEL, base) == (
        2, 2_000, 1_000, pytest.approx(0.5),
    )
