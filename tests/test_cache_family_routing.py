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
# qwen（Task 1a）：名称优先的家族归属——聚合域不再把外族模型拽进 Qwen 家族
# ---------------------------------------------------------------------------

_MAAS_AGGREGATE_BASE = (
    "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)


def test_qwen_family_not_pulled_in_by_aggregate_domain():
    """deepseek 名 × maas 聚合域：不得被判成 Qwen 家族，请求字节保持原样。"""
    from core.llm import (
        _apply_cache_control,
        _provider_request_options,
        _qwen_cache_enabled,
        _supports_cache_control,
    )

    model = "openai/deepseek-v4.1-flash"
    api_base = _MAAS_AGGREGATE_BASE
    assert _qwen_cache_enabled(model, api_base) is False
    assert _supports_cache_control(model, api_base) is False
    opts = _provider_request_options(model, api_base, cache_scope="main")
    assert "x-dashscope-session-cache" not in opts.get("extra_headers", {})
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "inspect"},
    ]
    assert _apply_cache_control(deepcopy(messages), model, api_base) == messages


def test_qwen_family_still_enabled_for_qwen_names_on_aggregate_domain():
    """对偶断言：本族名 × maas 域行为零变化；名称无归属时域名兜底照旧。"""
    from core.llm import (
        _apply_cache_control,
        _is_qwen_family,
        _provider_request_options,
        _qwen_cache_enabled,
        _supports_cache_control,
    )

    model = "openai/qwen3.8-flash"
    api_base = _MAAS_AGGREGATE_BASE
    assert _qwen_cache_enabled(model, api_base) is True
    assert _supports_cache_control(model, api_base) is True
    opts = _provider_request_options(model, api_base, cache_scope="main")
    assert opts.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "inspect"},
    ]
    marked = _apply_cache_control(deepcopy(messages), model, api_base)
    assert marked == [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": "stable",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "inspect",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
    ]
    # 源列表未被污染（deepcopy 传参）
    assert messages == [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "inspect"},
    ]

    # 名称无任何归属时，官方域名兜底照旧（_is_official_dashscope_route 未动）
    assert (
        _is_qwen_family(
            "custom-model", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        is True
    )


# ---------------------------------------------------------------------------
# Task 1b：kimi / deepseek / glm 对称收紧——名称优先于域名兜底
#
# 与 Task 1a 的 qwen 同一道门：名称命中本族 → True；名称已属其他已知族 →
# 域名兜底不生效；名称无任何归属 → 官方域兜底照旧。
# ---------------------------------------------------------------------------

_FAMILY_ORDER = ("kimi", "deepseek", "glm", "qwen")

# 四族 × {各自官方域, maas 聚合域, tokenrhythm, relay} 的完整笛卡尔积基底：
# _FAMILIES 的四个官方域 + _OTHER_CHANNELS（含 maas 聚合域、tokenrhythm、relay）。
_MATRIX_BASES = [official_base for _model, official_base in _FAMILIES] + list(
    _OTHER_CHANNELS
)

# 名称已属已知族的代表模型及其（唯一的）归属族。
_OWNED_MODELS = [
    ("openai/deepseek-v4.1-flash", "deepseek"),
    ("openai/kimi-k2.6", "kimi"),
    ("openai/glm-5.3", "glm"),
    ("openai/qwen3.8-flash", "qwen"),
]


def _family_bools(model, api_base):
    """四族判定的完整布尔四元组，顺序同 _FAMILY_ORDER。"""
    from core.llm import (
        _is_deepseek_family,
        _is_glm_family,
        _is_kimi_family,
        _is_qwen_family,
    )

    return (
        _is_kimi_family(model, api_base),
        _is_deepseek_family(model, api_base),
        _is_glm_family(model, api_base),
        _is_qwen_family(model, api_base),
    )


def test_foreign_family_names_not_pulled_in_by_official_domains():
    """外族模型 × 他族官方域：只剩本族单 True，不再被域名兜底拽进他族。"""
    from core.llm import (
        _is_deepseek_family,
        _is_glm_family,
        _is_kimi_family,
        is_deepseek_ledger_route,
    )

    # kimi 名挂 deepseek 官方域：deepseek 家族不得抢认，kimi 名称命中照旧
    assert _is_deepseek_family("openai/kimi-k2.6", "https://api.deepseek.com") is False
    assert _is_kimi_family("openai/kimi-k2.6", "https://api.deepseek.com") is True
    # glm 名挂 deepseek 官方域
    assert _is_deepseek_family("openai/glm-5.3", "https://api.deepseek.com") is False
    # deepseek 名挂智谱官方域
    assert (
        _is_glm_family(
            "openai/deepseek-v4.1-flash", "https://open.bigmodel.cn/api/paas/v4"
        )
        is False
    )
    # deepseek 名挂 moonshot 官方域（kimi 兜底是裸子串，比另外三族松）
    assert (
        _is_kimi_family("openai/deepseek-v4.1-flash", "https://api.moonshot.cn/v1")
        is False
    )
    # kimi 名挂智谱官方域
    assert (
        _is_glm_family("openai/kimi-k2.6", "https://open.bigmodel.cn/api/paas/v4")
        is False
    )
    # qwen 名挂 deepseek 官方域：不得启用 DeepSeek 账本（Task 1b 真实收益点）
    assert (
        is_deepseek_ledger_route("openai/qwen3.8-flash", "https://api.deepseek.com")
        is False
    )


def test_unowned_names_still_fall_back_to_official_domains():
    """名称无任何归属时，四族官方域兜底照旧（对偶语义，门不得误伤）。"""
    from core.llm import (
        _is_deepseek_family,
        _is_glm_family,
        _is_kimi_family,
        _is_qwen_family,
    )

    assert _is_deepseek_family("custom-model", "https://api.deepseek.com") is True
    assert _is_kimi_family("custom-model", "https://api.moonshot.cn/v1") is True
    assert _is_glm_family("custom-model", "https://open.bigmodel.cn/api/paas/v4") is True
    assert (
        _is_qwen_family(
            "custom-model", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        is True
    )


@pytest.mark.parametrize("model,owner", _OWNED_MODELS)
@pytest.mark.parametrize("api_base", _MATRIX_BASES)
def test_family_matrix_known_names_single_owner(model, owner, api_base):
    """四族名 × 七个渠道基底：完整四元组只有名称归属族为 True（含 maas 聚合域）。"""
    expected = tuple(family == owner for family in _FAMILY_ORDER)
    assert _family_bools(model, api_base) == expected, (model, api_base)


@pytest.mark.parametrize(
    "api_base,owner",
    [
        ("https://api.deepseek.com", "deepseek"),
        ("https://api.moonshot.cn/v1", "kimi"),
        ("https://open.bigmodel.cn/api/paas/v4", "glm"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen"),
        (_MAAS_AGGREGATE_BASE, "qwen"),
        ("https://tokenrhythm.studio/v1", None),
        ("https://relay.example/v1", None),
    ],
)
def test_family_matrix_unowned_name_domain_fallback(api_base, owner):
    """名称无归属 × 七个渠道基底：域名兜底仅本族官方/聚合域生效，其余全 False。"""
    expected = tuple(family == owner for family in _FAMILY_ORDER)
    assert _family_bools("custom-model", api_base) == expected, api_base


def test_mixed_family_names_keep_multi_membership():
    """混合名维持现状：deepseek-qwen 双名仍双 True，不引入单值家族概念。"""
    from core.llm import _is_deepseek_family, _is_qwen_family

    model = "openai/deepseek-r1-qwen-distill"
    assert _is_deepseek_family(model, "https://relay.example/v1") is True
    assert _is_qwen_family(model, "https://relay.example/v1") is True


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


# ---------------------------------------------------------------------------
# Task 6：prompt_cache_key 白名单化——默认关闭，仅白名单车道发送
#
# 铁律：绝不发 prompt_cache_breakpoint（§1.8：OpenAI 认它是有效字段，
# Kimi 收到直接 400）。白名单留空时，非 kimi 模型的请求字节零变化。
# ---------------------------------------------------------------------------

_AFFINITY_LANE_BASE = "https://relay.example/v1"
_AFFINITY_LANE_OTHER_BASE = "https://relay-other.example/v1"


def _set_affinity_lanes(monkeypatch, entries):
    """配置替身 + 强制重读共用配置条目（同 cache_keepalive_routes 测试手法）。"""
    import core.llm_dialects as dialects

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"cache_affinity_lanes": entries},
    )
    with dialects._qwen_cache_cfg_lock:
        dialects._keepalive_routes_cfg_cache["entry"] = None


def test_affinity_key_default_off_for_non_kimi_models():
    """默认关闭：无 cache_affinity_lanes 配置时，非 kimi 模型一律不带亲和键。"""
    from core.llm import _provider_request_options

    for model, api_base in (
        ("openai/deepseek-v4.1-flash", "https://relay.example/v1"),
        ("openai/qwen3.8-flash", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ):
        opts = _provider_request_options(
            model, api_base, cache_session_id="lane-default-off"
        )
        assert "prompt_cache_key" not in opts.get("extra_body", {}), (model, api_base)


@pytest.mark.parametrize("api_base", ["https://api.moonshot.cn/v1", *_OTHER_CHANNELS])
def test_kimi_affinity_unchanged_with_empty_lanes(monkeypatch, api_base):
    """白名单为空/缺省：kimi 家族在任意渠道仍带亲和键，行为零变化。"""
    from core.llm import _provider_request_options

    _set_affinity_lanes(monkeypatch, [])
    opts = _provider_request_options(
        "openai/kimi-k2.6", api_base, cache_session_id="family-matrix"
    )
    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")


def test_affinity_lane_whitelist_enables_only_listed_domain(monkeypatch):
    """白名单开启后才生效：命中域名发亲和键，其他域名与家族仍不带。"""
    from core.llm import _provider_cache_affinity_options, _provider_request_options

    _set_affinity_lanes(monkeypatch, ["relay.example"])

    enabled = _provider_request_options(
        "openai/deepseek-v4.1-flash", _AFFINITY_LANE_BASE, cache_session_id="lane-on"
    )
    assert enabled["extra_body"]["prompt_cache_key"].startswith("ycode-")

    blocked = _provider_request_options(
        "openai/deepseek-v4.1-flash",
        _AFFINITY_LANE_OTHER_BASE,
        cache_session_id="lane-off",
    )
    assert "prompt_cache_key" not in blocked.get("extra_body", {})

    blocked_dashscope = _provider_request_options(
        "openai/qwen3.8-flash",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        cache_session_id="lane-off",
    )
    assert "prompt_cache_key" not in blocked_dashscope.get("extra_body", {})

    # 副模型/压缩通道（_provider_cache_affinity_options）同样只放行白名单车道
    affinity = _provider_cache_affinity_options(
        "openai/deepseek-v4.1-flash",
        _AFFINITY_LANE_BASE,
        cache_scope="secondary:compression",
        tool_contract="no-tools",
    )
    assert affinity["extra_body"]["prompt_cache_key"].startswith("ycode-")
    other_affinity = _provider_cache_affinity_options(
        "openai/deepseek-v4.1-flash",
        _AFFINITY_LANE_OTHER_BASE,
        cache_scope="secondary:compression",
        tool_contract="no-tools",
    )
    assert other_affinity == {}


def test_empty_affinity_lanes_byte_equivalent_to_missing_key(monkeypatch):
    """白名单为空列表：未白名单行为与无配置时逐字等价（序列化级比对）。"""
    import json

    from core.llm import _provider_request_options

    model, api_base = "openai/deepseek-v4.1-flash", _AFFINITY_LANE_BASE
    without = _provider_request_options(model, api_base, cache_session_id="lane-empty")
    _set_affinity_lanes(monkeypatch, [])
    with_empty = _provider_request_options(
        model, api_base, cache_session_id="lane-empty"
    )
    assert json.dumps(with_empty, sort_keys=True) == json.dumps(
        without, sort_keys=True
    )


def test_affinity_lane_entry_normalization_accepts_bare_domain_and_url(monkeypatch):
    """归一化：白名单写裸域名与写完整 URL 都匹配同一 api_base。"""
    from core.llm import _provider_request_options

    for entry in ("relay.example", "https://relay.example/v1"):
        _set_affinity_lanes(monkeypatch, [entry])
        opts = _provider_request_options(
            "openai/deepseek-v4.1-flash",
            _AFFINITY_LANE_BASE,
            cache_session_id="lane-normalize",
        )
        assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-"), entry


def test_affinity_lane_rejection_downgrades_retry_without_key(monkeypatch, mocker):
    """白名单车道被上游 400 拒绝：本轮重试剥离亲和键，该路由进拉黑表。"""
    from core.llm import (
        _affinity_route_disabled,
        _provider_request_options,
        chat_with_model_events,
    )

    _set_affinity_lanes(monkeypatch, ["relay.example"])
    model, base = "openai/deepseek-v4.1-flash", _AFFINITY_LANE_BASE

    captured = []

    def fake_completion(**kwargs):
        captured.append(deepcopy(kwargs))
        if len(captured) == 1:
            raise _FakeBadRequest("Error code: 400 - unknown field 'prompt_cache_key'")
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        model,
        "fake-key",
        [{"role": "user", "content": "提问"}],
        base,
        persist=False,
        turn_id="turn-affinity-lane-downgrade",
        consume_background_notifications=False,
        cache_session_id="affinity-lane-downgrade",
    ))

    assert len(captured) >= 2
    assert captured[0]["extra_body"]["prompt_cache_key"].startswith("ycode-")
    assert "prompt_cache_key" not in captured[1].get("extra_body", {})
    assert _affinity_route_disabled(model, base) is True
    follow_up = _provider_request_options(
        model, base, cache_session_id="affinity-lane-downgrade"
    )
    assert "prompt_cache_key" not in follow_up.get("extra_body", {})


def test_guard_no_prompt_cache_breakpoint_in_provider_kwargs(monkeypatch, mocker):
    """铁律守卫：最终 provider kwargs（含 extra_body/extra_headers/metadata）
    的序列化结果里不得出现 prompt_cache_breakpoint。"""
    import json

    from core.llm import _provider_request_options, chat_with_model_events

    _set_affinity_lanes(monkeypatch, ["relay.example"])

    for model, api_base in (
        ("openai/kimi-k2.6", "https://api.moonshot.cn/v1"),
        ("anthropic/kimi-k2.6", "https://api.moonshot.cn/v1"),
        ("openai/deepseek-v4.1-flash", _AFFINITY_LANE_BASE),
        ("openai/qwen3.8-flash", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ):
        opts = _provider_request_options(
            model, api_base, cache_session_id="breakpoint-guard"
        )
        assert "prompt_cache_breakpoint" not in json.dumps(
            opts, ensure_ascii=False, default=str
        ), (model, api_base)

    captured = []

    def fake_completion(**kwargs):
        captured.append(deepcopy(kwargs))
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)
    list(chat_with_model_events(
        "openai/kimi-k2.6",
        "fake-key",
        [{"role": "user", "content": "守卫"}],
        "https://api.moonshot.cn/v1",
        persist=False,
        turn_id="turn-breakpoint-guard",
        consume_background_notifications=False,
        cache_session_id="breakpoint-guard",
    ))
    assert captured
    assert "prompt_cache_breakpoint" not in json.dumps(
        captured[0], ensure_ascii=False, default=str
    )


def test_mixed_kimi_qwen_name_on_affinity_lane_keeps_kimi_branch_only(monkeypatch):
    """白名单不得把 qwen 分支拽进亲和选项：kimi+qwen 混合名仍只走 kimi 分支。

    混合名同时命中两族；原语义是 if/elif（kimi 命中即不再走 qwen），
    白名单只应新增 prompt_cache_key 通道，不得顺带塞入 qwen 的会话缓存头。
    注意：qwen 会话缓存头在主链路 `_provider_request_options` 里走的是另一条
    独立路径，不受本函数分支影响——所以这条守卫断言只针对本函数的返回值。
    """
    from core.llm import _provider_cache_affinity_options

    _set_affinity_lanes(monkeypatch, ["relay.example"])

    opts = _provider_cache_affinity_options(
        "openai/kimi-k2.6-qwen",
        _AFFINITY_LANE_BASE,
        cache_scope="main",
        cache_session_id="mixed-name-guard",
    )

    assert opts["extra_body"]["prompt_cache_key"].startswith("ycode-")
    assert "extra_headers" not in opts
