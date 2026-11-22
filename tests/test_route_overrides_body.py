import pytest

from core.llm_dialects import _provider_request_options


@pytest.fixture(autouse=True)
def _reset_override_cache():
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
    overrides._cache["entry"] = None

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