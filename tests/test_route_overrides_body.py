"""渠道请求形态覆盖（headers + body）经 _provider_request_options 唯一出口生效。

来自 待开发/网关请求形态覆盖/设计规格与实施计划.md §5 Task 2 Step 1。
"""

import pytest

from core.llm_dialects import _provider_request_options


@pytest.fixture(autouse=True)
def _reset_override_cache():
    """每个用例前后强制重读配置（同 tests/test_route_overrides.py 的先例）。"""
    import core.route_overrides as mod
    mod._cache["entry"] = None
    yield
    mod._cache["entry"] = None


def test_provider_request_options_applies_route_override(monkeypatch):
    import core.route_overrides as overrides
    monkeypatch.setattr(overrides, "_load_config_for_overrides", lambda: {
        "route_request_overrides": [{
            "match": "tokenrhythm.studio",
            "headers": {"X-Test": "1"},
            "body": {"thinking": {"type": "enabled"}},
        }],
    })
    overrides._cache["entry"] = None  # 强制重读配置（同 _keepalive_routes_cfg_cache 先例）

    options = _provider_request_options(
        "openai/deepseek-flash", "https://tokenrhythm.studio/v1", cache_scope="main")

    assert options["extra_headers"]["X-Test"] == "1"
    assert options["extra_body"]["thinking"] == {"type": "enabled"}


def test_provider_request_options_untouched_without_config(monkeypatch):
    import core.route_overrides as overrides
    monkeypatch.setattr(overrides, "_load_config_for_overrides", lambda: {})
    overrides._cache["entry"] = None

    options = _provider_request_options(
        "openai/deepseek-flash", "https://api.deepseek.com", cache_scope="main")

    assert not options.get("extra_headers")