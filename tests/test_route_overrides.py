import pytest

from core.route_overrides import resolve_route_override, HEADER_DENY_KEYS, BODY_DENY_KEYS


@pytest.fixture(autouse=True)
def _reset_override_cache():
    """每个用例前强制重读配置。

    配置指纹只反映「loader 身份 + config 文件 mtime」；monkeypatch 换掉
    _load_config_for_overrides 时二者都不变，只靠 TTL 兜底会让用例串味
    （2026-09-24 实测：去掉这个 fixture，本文件 5 个用例挂 3 个）。
    手法同 _keepalive_routes_cfg_cache 注释里写明的"测试可直接把 entry 置 None"。
    """
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
    """配置指纹变化必须立刻失效缓存，而不是等 10 秒 TTL。

    注意这里刻意不调用 _fake_config（它会把缓存置 None）——缓存必须靠
    stamp 比对失效，才证明实现真的带了指纹。
    """
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
    stamp["value"] = ("loader", 2)  # 指纹变了，TTL 还没到
    assert resolve_route_override("m", "https://tokenrhythm.studio/v1")["headers"] == {"X-Test": "new"}