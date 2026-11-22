import pytest

from core.route_overrides import resolve_route_override, HEADER_DENY_KEYS, BODY_DENY_KEYS


@pytest.fixture(autouse=True)
def _reset_override_cache():
    import core.route_overrides as mod
    mod._cache["entry"] = None
    yield
    mod._cache["entry"] = None


def _fake_config(monkeypatch, data):
    import core.route_overrides as mod
    monkeypatch.setattr(mod, "_load_config_for_overrides", lambda: data)
    mod._cache["entry"] = None


def test_matched_host_returns_headers_and_body(monkeypatch):
    _fake_config(monkeypatch, {"route_request_overrides": [
        {"match": "tokenrhythm.studio",
         "headers": {"X-Test": "1"},
         "body": {"thinking": {"type": "enabled"}}},
    ]})
    result = resolve_route_override("openai/deepseek-flash", "https://tokenrhythm.studio/v1")
    assert result["headers"] == {"X-Test": "1"}
    assert result["body"] == {"thinking": {"type": "enabled"}}
    assert result["fingerprint"]


def test_unmatched_host_is_zero_impact(monkeypatch):
    _fake_config(monkeypatch, {"route_request_overrides": [
        {"match": "tokenrhythm.studio", "headers": {"X-Test": "1"}},
    ]})
    result = resolve_route_override("openai/deepseek-flash", "https://api.deepseek.com")
    assert result == {"headers": {}, "body": {}, "fingerprint": ""}


def test_deny_keys_are_stripped(monkeypatch):
    _fake_config(monkeypatch, {"route_request_overrides": [
        {"match": "tokenrhythm.studio",
         "headers": {"Authorization": "Bearer leaked", "X-Test": "1"},
         "body": {"model": "hacked", "temperature": 0.7}},
    ]})
    result = resolve_route_override("openai/deepseek-flash", "https://tokenrhythm.studio/v1")
    assert result["headers"] == {"X-Test": "1"}
    assert result["body"] == {"temperature": 0.7}


def test_malformed_entries_are_ignored(monkeypatch):
    _fake_config(monkeypatch, {"route_request_overrides": [
        "not-a-dict",
        {"headers": {"X-NoMatch": "1"}},
        {"match": "tokenrhythm.studio", "headers": "not-a-dict", "body": {"a": 1}},
    ]})
    result = resolve_route_override("openai/deepseek-flash", "https://tokenrhythm.studio/v1")
    assert result["headers"] == {}
    assert result["body"] == {"a": 1}


def test_env_var_reference_is_expanded(monkeypatch):
    monkeypatch.setenv("TR_TEST_TOKEN", "s3cret")
    _fake_config(monkeypatch, {"route_request_overrides": [
        {"match": "tokenrhythm.studio", "headers": {"X-Key": "${env:TR_TEST_TOKEN}"}},
    ]})
    result = resolve_route_override("openai/deepseek-flash", "https://tokenrhythm.studio/v1")
    assert result["headers"] == {"X-Key": "s3cret"}


def test_stamp_change_invalidates_cache(monkeypatch):
    import core.route_overrides as mod

    stamp = {"value": ("loader", 1)}
    config = {"route_request_overrides": [
        {"match": "tokenrhythm.studio", "headers": {"X-Test": "old"}},
    ]}
    monkeypatch.setattr(mod, "_config_stamp", lambda: stamp["value"])
    monkeypatch.setattr(mod, "_load_config_for_overrides", lambda: config)
    mod._cache["entry"] = None

    assert resolve_route_override("m", "https://tokenrhythm.studio/v1")["headers"] == {"X-Test": "old"}

    config["route_request_overrides"][0]["headers"] = {"X-Test": "new"}
    stamp["value"] = ("loader", 2)
    assert resolve_route_override("m", "https://tokenrhythm.studio/v1")["headers"] == {"X-Test": "new"}