"""水位线计算基准上限（memory_compression.watermark_base_tokens /
model_watermark_bases）配置读取层的单元测试。

只验证配置读取与容错：非法配置必须安全回落（None / {}），
绝不能把用户写错的新配置变成配置读取崩溃。
"""

import pytest

import core.memory_compressor as mc


def _patch_load_config(monkeypatch, config):
    """把 core.model_manager.load_config 打桩为返回指定 dict。"""
    monkeypatch.setattr("core.model_manager.load_config", lambda: config)


def test_missing_memory_compression_section(monkeypatch):
    """a. 无 memory_compression 配置 → 基准为 None / 空表。"""
    _patch_load_config(monkeypatch, {})
    cfg = mc._load_compression_config()
    assert cfg["watermark_base_tokens"] is None
    assert cfg["model_watermark_bases"] == {}
    assert mc.get_watermark_base_tokens("gpt-5") is None


def test_global_watermark_base_tokens(monkeypatch):
    """b. 全局 watermark_base_tokens 生效。"""
    _patch_load_config(
        monkeypatch,
        {"memory_compression": {"watermark_base_tokens": 400000}},
    )
    assert mc._load_compression_config()["watermark_base_tokens"] == 400000
    assert mc.get_watermark_base_tokens("gpt-5") == 400000


def test_model_specific_overrides_global(monkeypatch):
    """c. model_watermark_bases 命中时覆盖全局值，未命中回落全局值。"""
    _patch_load_config(
        monkeypatch,
        {
            "memory_compression": {
                "watermark_base_tokens": 400000,
                "model_watermark_bases": {"deepseek-flash": 300000},
            }
        },
    )
    assert mc.get_watermark_base_tokens("deepseek-flash") == 300000
    assert mc.get_watermark_base_tokens("gpt-5") == 400000


def test_model_name_leaf_and_case_normalization(monkeypatch):
    """d. 模型名取最后一个 "/" 之后的叶子名、小写；键也 strip + 小写归一。"""
    _patch_load_config(
        monkeypatch,
        {
            "memory_compression": {
                "model_watermark_bases": {
                    " DEEPSEEK-Flash ": 300000,
                    "gpt-5": 250000,
                }
            }
        },
    )
    assert mc.get_watermark_base_tokens("openai/deepseek-flash") == 300000
    assert mc.get_watermark_base_tokens("DeepSeek-Flash") == 300000
    assert mc.get_watermark_base_tokens("vendor/proxy/gpt-5") == 250000


@pytest.mark.parametrize("bad_value", [0, -1, "abc", 1.5, True, None])
def test_invalid_global_value_is_none(monkeypatch, bad_value):
    """e1. 全局基准非法值安全忽略为 None。"""
    _patch_load_config(
        monkeypatch,
        {"memory_compression": {"watermark_base_tokens": bad_value}},
    )
    assert mc._load_compression_config()["watermark_base_tokens"] is None
    assert mc.get_watermark_base_tokens("gpt-5") is None


@pytest.mark.parametrize("bad_dict", ["oops", [1, 2], 123, None])
def test_non_dict_model_bases_is_empty(monkeypatch, bad_dict):
    """e2. model_watermark_bases 非 dict → {}，并按全局值回落。"""
    _patch_load_config(
        monkeypatch,
        {
            "memory_compression": {
                "watermark_base_tokens": 400000,
                "model_watermark_bases": bad_dict,
            }
        },
    )
    assert mc._load_compression_config()["model_watermark_bases"] == {}
    assert mc.get_watermark_base_tokens("deepseek-flash") == 400000


def test_invalid_model_entries_are_filtered(monkeypatch):
    """e3. 非法条目过滤，合法条目保留。"""
    _patch_load_config(
        monkeypatch,
        {
            "memory_compression": {
                "model_watermark_bases": {
                    "ok-model": 123,
                    "zero": 0,
                    "negative": -5,
                    "text": "abc",
                    "float": 1.5,
                    "": 999,
                }
            }
        },
    )
    assert mc._load_compression_config()["model_watermark_bases"] == {"ok-model": 123}
    assert mc.get_watermark_base_tokens("ok-model") == 123
    assert mc.get_watermark_base_tokens("zero") is None
    assert mc.get_watermark_base_tokens("text") is None


def test_load_config_exception_falls_back_safely(monkeypatch):
    """f. load_config 抛异常 → 配置读取仍可用且基准为 None（不抛）。"""

    def boom():
        raise RuntimeError("config broken")

    monkeypatch.setattr("core.model_manager.load_config", boom)
    cfg = mc._load_compression_config()
    assert cfg["watermark_base_tokens"] is None
    assert cfg["model_watermark_bases"] == {}
    assert cfg["retain_count"] == mc.DEFAULT_RETAIN_COUNT
    assert mc.get_watermark_base_tokens("deepseek-flash") is None