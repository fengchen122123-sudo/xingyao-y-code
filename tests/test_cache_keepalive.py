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
