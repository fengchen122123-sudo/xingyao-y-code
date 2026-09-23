"""缓存优化家族化判定：四族 × 渠道矩阵与 kimi 亲和键拒绝兜底。

判定只看模型家族，不看渠道：deepseek → 账本+工具排序+保活；
kimi → 亲和键+保活；glm → 保活；qwen → 断点+会话缓存头+工具排序+保活。
"""

import time
from copy import deepcopy

import pytest

# 四族代表模型与各自官方渠道
_FAMILIES = [
    ("openai/deepseek-v4.1-flash", "https://api.deepseek.com"),
    ("openai/kimi-k2.6", "https://api.moonshot.cn/v1"),
    ("openai/glm-5.3", "https://open.bigmodel.cn/api/paas/v4"),
    ("openai/qwen3.8-flash", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
]

# 非官方渠道：tokenrhythm / 阿里云 token-plan（maas.aliyuncs.com）/ 任意中转
_OTHER_CHANNELS = [
    "https://tokenrhythm.studio/v1",
    "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "https://relay.example/v1",
]


@pytest.fixture(autouse=True)
def _isolate_route_state(monkeypatch):
    """隔离路由判定全局态：配置替身、配置解析缓存与拉黑表不跨测试残留。"""
    import core.llm_dialects as dialects

    monkeypatch.setattr("core.model_manager.load_config", lambda: {})
    with dialects._qwen_cache_cfg_lock:
        dialects._keepalive_routes_cfg_cache["entry"] = None
        dialects._deepseek_cache_routes_cfg_cache["entry"] = None
        dialects._qwen_cache_cfg_cache.clear()
    with dialects._affinity_disabled_lock:
        dialects._affinity_disabled_routes.clear()
    with dialects._cache_control_disabled_lock:
        dialects._cache_control_disabled_routes.clear()
    yield
    with dialects._affinity_disabled_lock:
        dialects._affinity_disabled_routes.clear()
    with dialects._cache_control_disabled_lock:
        dialects._cache_control_disabled_routes.clear()


# ---------------------------------------------------------------------------
# qwen：断点 + 会话缓存头 + 工具排序，任意渠道自动启用
# ---------------------------------------------------------------------------


def test_qwen_cache_extras_enabled_across_channels():
    """qwen 家族任意渠道：缓存开启判定、断点支持、会话缓存头、工具排序全部生效。"""
    from core.llm import (
        _provider_request_options,
        _qwen_cache_enabled,
        _select_request_tools_for_route,
        _supports_cache_control,
    )

    model = "openai/qwen3.8-flash"
    tools = [
        {"type": "function", "function": {"name": "zeta", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    ]
    for api_base in [*_OTHER_CHANNELS]:
        assert _qwen_cache_enabled(model, api_base) is True, api_base
        assert _supports_cache_control(model, api_base) is True, api_base
        opts = _provider_request_options(model, api_base, cache_scope="main")
        assert (
            opts.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"
        ), api_base
        sorted_tools = _select_request_tools_for_route(model, api_base, tools)
        assert [t["function"]["name"] for t in sorted_tools] == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# deepseek：账本判定，任意渠道自动放行；非本族不误放
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api_base", ["https://api.deepseek.com", *_OTHER_CHANNELS])
def test_deepseek_ledger_route_covers_all_channels(api_base):
    from core.llm import is_deepseek_ledger_route

    assert is_deepseek_ledger_route("openai/deepseek-v4.1-flash", api_base) is True


def test_deepseek_ledger_route_not_released_for_other_families():
    from core.llm import is_deepseek_ledger_route

    assert is_deepseek_ledger_route("openai/kimi-for-coding", "https://tokenrhythm.studio/v1") is False
    assert is_deepseek_ledger_route("openai/qwen3.8-flash", "https://relay.example/v1") is False
    assert is_deepseek_ledger_route("openai/glm-5.3", "https://relay.example/v1") is False


# ---------------------------------------------------------------------------
# 工具排序：deepseek / qwen 任意渠道字母序；kimi / glm / 其它原样
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model,api_base", [
    ("openai/deepseek-v4.1-flash", "https://relay.example/v1"),
    ("openai/qwen3.8-flash", "https://relay.example/v1"),
])
def test_tools_sorted_for_deepseek_and_qwen_across_channels(model, api_base):
    from core.llm import _select_request_tools_for_route

    tools = [
        {"type": "function", "function": {"name": "zeta", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    ]
    sorted_tools = _select_request_tools_for_route(model, api_base, tools)
    assert [t["function"]["name"] for t in sorted_tools] == ["alpha", "zeta"]
    assert sorted_tools is not tools


def test_tools_order_untouched_for_other_models():
    from core.llm import _select_request_tools_for_route

    tools = [
        {"type": "function", "function": {"name": "zeta", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    ]
    for model in ("openai/kimi-k2.6", "openai/glm-5.3", "openai/gpt-4o"):
        assert _select_request_tools_for_route(model, "https://relay.example/v1", tools) is tools


# ---------------------------------------------------------------------------
# kimi：缓存亲和键，任意渠道发送
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api_base", ["https://api.moonshot.cn/v1", *_OTHER_CHANNELS])
def test_kimi_affinity_sent_across_channels(api_base):
    from core.llm import _provider_request_options

    opts = _provider_request_options(
        "openai/kimi-k2.6", api_base, cache_session_id="family-matrix"
    )
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")


# ---------------------------------------------------------------------------
# 保活：四族任意渠道自动放行；非四族仍走显式白名单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model,official_base", _FAMILIES)
def test_keepalive_enabled_for_family_across_channels(model, official_base):
    from core.llm import _supports_cache_keepalive

    for api_base in [official_base, *_OTHER_CHANNELS]:
        assert _supports_cache_keepalive(model, api_base) is True, (model, api_base)


def test_keepalive_explicit_routes_still_gate_non_family_models(monkeypatch):
    from core.llm import _supports_cache_keepalive
    import core.llm_dialects as dialects

    assert _supports_cache_keepalive("openai/some-model", "https://relay.example/v1") is False

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"cache_keepalive_routes": ["tokenrhythm.studio"]},
    )
    with dialects._qwen_cache_cfg_lock:
        dialects._keepalive_routes_cfg_cache["entry"] = None
    assert _supports_cache_keepalive("openai/some-model", "https://tokenrhythm.studio/v1") is True


# ---------------------------------------------------------------------------
# kimi 亲和键 400 拒绝兜底：判定从严、临时拉黑、TTL 自愈、重试剥离
# ---------------------------------------------------------------------------


class _FakeBadRequest(Exception):
    status_code = 400


class _FakeServerError(Exception):
    status_code = 500


def test_affinity_rejection_requires_bad_request_status_and_named_field():
    """只有 400 且文案点名 prompt_cache_key / user_id 才拉黑；无关故障不误伤。"""
    from core.llm import _is_kimi_affinity_rejection

    assert _is_kimi_affinity_rejection(
        _FakeBadRequest("Error code: 400 - unknown field 'prompt_cache_key'")
    )
    assert _is_kimi_affinity_rejection(
        _FakeBadRequest("Error code: 400 - invalid metadata.user_id")
    )
    assert not _is_kimi_affinity_rejection(
        RuntimeError("connection reset while uploading prompt_cache_key (4000 bytes)")
    )
    assert not _is_kimi_affinity_rejection(
        _FakeServerError("500 internal server error mentioning user_id")
    )
    assert not _is_kimi_affinity_rejection(
        _FakeBadRequest("Error code: 400 - context length exceeded, invalid prompt")
    )


def test_affinity_blacklist_skips_key_and_expires_after_ttl():
    """拉黑期间请求不再携带亲和键；TTL 到期自动恢复，一次误判不永久关优化。"""
    from core.llm import (
        _disable_affinity_route,
        _provider_request_options,
    )

    base = "https://relay.example/v1"
    model = "openai/kimi-k2.6"

    opts = _provider_request_options(model, base, cache_session_id="affinity-ttl")
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")

    _disable_affinity_route(model, base, ttl_seconds=0.05)
    blocked = _provider_request_options(model, base, cache_session_id="affinity-ttl")
    assert "prompt_cache_key" not in blocked.get("extra_body", {})

    time.sleep(0.08)
    recovered = _provider_request_options(model, base, cache_session_id="affinity-ttl")
    assert recovered["extra_body"]["prompt_cache_key"].startswith("ycode-")


def _chunk(text="完成"):
    from types import SimpleNamespace

    delta = SimpleNamespace(content=text, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason="stop")], usage=None
    )


def test_affinity_rejection_downgrades_retry_without_key(mocker):
    """模拟 400：拉黑该路由 → 本轮重试剥离亲和键 → 后续请求也不再携带。"""
    from core.llm import (
        _affinity_route_disabled,
        _provider_request_options,
        chat_with_model_events,
    )

    captured = []

    def fake_completion(**kwargs):
        captured.append(deepcopy(kwargs))
        if len(captured) == 1:
            raise _FakeBadRequest("Error code: 400 - unknown field 'prompt_cache_key'")
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "openai/kimi-k2.6",
        "fake-key",
        [{"role": "user", "content": "提问"}],
        "https://relay.example/v1",
        persist=False,
        turn_id="turn-affinity-downgrade",
        consume_background_notifications=False,
        cache_session_id="affinity-downgrade",
    ))

    assert len(captured) >= 2
    assert captured[0]["extra_body"]["prompt_cache_key"].startswith("ycode-")
    assert "prompt_cache_key" not in captured[1].get("extra_body", {})
    assert _affinity_route_disabled("openai/kimi-k2.6", "https://relay.example/v1") is True
    follow_up = _provider_request_options(
        "openai/kimi-k2.6", "https://relay.example/v1", cache_session_id="affinity-downgrade"
    )
    assert "prompt_cache_key" not in follow_up.get("extra_body", {})
