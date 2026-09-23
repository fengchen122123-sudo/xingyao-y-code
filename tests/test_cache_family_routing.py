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
    with dialects._cache_control_disabled_lock:
        dialects._cache_control_disabled_routes.clear()
    yield
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
