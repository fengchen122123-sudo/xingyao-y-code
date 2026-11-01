"""渠道请求形态覆盖在压缩链路（compression scope）生效。

来自 待开发/网关请求形态覆盖/设计规格与实施计划.md §4.3（全 scope 一致）、
§6 V5（全 scope 一致）、§7 R9（compression 未生效缺口）、§9.4。

压缩请求不经过 `_provider_request_options`（那条出口会带 max_tokens /
thinking / allowed_openai_params，改变压缩请求字节），而是在
`_provider_cache_affinity_options(...)` 之后单独合并
`_apply_route_request_overrides`，语义与 provider options 出口一致：
headers → extra_headers、body → extra_body。
"""

import pytest

import core.memory_compressor as mc
from core.memory import MemoryManager
from core.memory_compressor import compress_session_memory

_URL_MATCHED = "https://tokenrhythm.studio/v1"
_URL_UNMATCHED = "https://api.deepseek.com/v1"


class _Resp:
    def __init__(self, text):
        class _Msg:
            content = text

        class _Choice:
            message = _Msg()

        self.choices = [_Choice()]


@pytest.fixture(autouse=True)
def _reset_override_cache():
    """每个用例前后强制重读配置（同 tests/test_route_overrides_body.py 的先例）。"""
    import core.route_overrides as mod

    mod._cache["entry"] = None
    yield
    mod._cache["entry"] = None


def _fake_overrides(monkeypatch, data):
    """把 route_request_overrides 的配置源换成测试数据并强制重读。"""
    import core.route_overrides as overrides

    monkeypatch.setattr(overrides, "_load_config_for_overrides", lambda: data)
    overrides._cache["entry"] = None


@pytest.fixture
def manager():
    m = MemoryManager(":memory:")
    m.create_session_explicit("compress-session", workdir="GLOBAL", source="GUI")
    return m


def _seed_history(manager, count, filler=3):
    for i in range(count):
        manager.add_message(
            "compress-session",
            "user" if i % 2 == 0 else "assistant",
            f"消息 {i} " + "内容填充 " * filler,
        )


@pytest.fixture(autouse=True)
def _no_secondary(monkeypatch):
    """禁用副模型、固定窗口、强制字符估算，并堵掉插入式压缩分支。

    复制自 tests/test_memory_compressor_chunks.py，额外把
    `_get_keepalive_snapshot` 置 None：避免走 insert_mode 支线（那条支线用
    保活快照，本身已带 extra_headers/extra_body），确保断言的是分段路径。
    """
    monkeypatch.setattr(
        "core.model_manager.get_secondary_roles", lambda: {"compression": False}
    )
    fake_active = {
        "litellm_model": "fake/compress-model",
        "api_key": "k",
        "base_url": "",
        "context_window": 4096,
    }
    monkeypatch.setattr(
        "core.model_manager.get_active_model", lambda *a, **kw: fake_active
    )
    monkeypatch.setattr(
        "core.llm._provider_cache_affinity_options",
        lambda *a, **kw: {},
    )
    monkeypatch.setattr(mc, "_get_encoding", lambda *a, **kw: None)
    monkeypatch.setattr(mc, "COMPRESSION_INTER_CHUNK_DELAY_SECONDS", 0)
    monkeypatch.setattr(mc, "_get_keepalive_snapshot", lambda *_a, **_kw: None)


def _capture_request(monkeypatch):
    """桩掉压缩模型调用，返回被调用时的 kwargs 列表。"""
    captured = []

    def fake_completion(**kwargs):
        captured.append(kwargs)
        return _Resp("压缩摘要")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)
    return captured


def test_compression_request_carries_route_override(manager, monkeypatch):
    """配置命中域名时，压缩请求带上覆盖的头与体。"""
    _fake_overrides(monkeypatch, {"route_request_overrides": [{
        "match": "tokenrhythm.studio",
        "headers": {"X-Test": "1"},
        "body": {"thinking": {"type": "enabled"}},
    }]})
    captured = _capture_request(monkeypatch)
    _seed_history(manager, 11)

    assert compress_session_memory(
        manager, "compress-session", "fake/compress-model", "k", _URL_MATCHED,
        output_callback=lambda _m: None,
    ) is True

    assert captured, "压缩请求未发出"
    kwargs = captured[0]
    assert kwargs["extra_headers"]["X-Test"] == "1"
    assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}


def test_compression_request_untouched_without_override(manager, monkeypatch):
    """未命中任何覆盖配置时，压缩请求字节零变化（不带 extra_headers/覆盖键）。"""
    _fake_overrides(monkeypatch, {})
    captured = _capture_request(monkeypatch)
    _seed_history(manager, 11)

    assert compress_session_memory(
        manager, "compress-session", "fake/compress-model", "k", _URL_UNMATCHED,
        output_callback=lambda _m: None,
    ) is True

    assert captured, "压缩请求未发出"
    kwargs = captured[0]
    assert "extra_headers" not in kwargs
    assert "thinking" not in (kwargs.get("extra_body") or {})


def test_compression_override_merges_with_affinity_options(manager, monkeypatch):
    """覆盖项与亲和参数是合并而非替换：prompt_cache_key 与覆盖键同时在场。"""
    _fake_overrides(monkeypatch, {"route_request_overrides": [{
        "match": "tokenrhythm.studio",
        "headers": {"X-Test": "1"},
        "body": {"thinking": {"type": "enabled"}},
    }]})
    monkeypatch.setattr(
        "core.llm._provider_cache_affinity_options",
        lambda *a, **kw: {"extra_body": {"prompt_cache_key": "ycode-x"}},
    )
    captured = _capture_request(monkeypatch)
    _seed_history(manager, 11)

    assert compress_session_memory(
        manager, "compress-session", "fake/compress-model", "k", _URL_MATCHED,
        output_callback=lambda _m: None,
    ) is True

    assert captured, "压缩请求未发出"
    kwargs = captured[0]
    assert kwargs["extra_body"]["prompt_cache_key"] == "ycode-x"
    assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}
    assert kwargs["extra_headers"]["X-Test"] == "1"
