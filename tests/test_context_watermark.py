"""上下文水位基准（memory_compression.watermark_base_tokens）读写与校验测试。

覆盖 core/context_watermark.py 的校验/读取/保存，以及 config.save_nested_config_value
的两层点分路径写入、memory_compression 嵌套键白名单登记。

隔离策略（与 tests/test_config_key_validation.py 一致）：
- monkeypatch config._CONFIG_FILE / _TOML_CONFIG_FILE 到 pytest tmp_path；
- 用例前后 dict.clear() 清空 config._CONFIG_CACHE 与 _TOML_CACHE；
- 不写 api_key，避开 DPAPI/Keyring 加解密路径。
绝不污染用户真实的 ~/.ycode/config.json。
"""

import json

import pytest

import config
import core.context_watermark as cw


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    """隔离配置环境：返回 config.json 路径。"""
    cfg_path = tmp_path / "config.json"
    toml_path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(toml_path))
    config._CONFIG_CACHE.clear()
    config._TOML_CACHE.clear()
    yield cfg_path
    config._CONFIG_CACHE.clear()
    config._TOML_CACHE.clear()


def _write_config(cfg_path, data):
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read_config(cfg_path):
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def _sample_config():
    """含其他顶层键的样例配置（providers 不带 api_key，避开加密路径）。"""
    return {
        "theme": "cyan",
        "active_model_id": "m1",
        "providers": [
            {
                "id": "p1",
                "name": "测试厂商",
                "base_url": "https://api.example.com",
                "protocol": "openai",
                "models": [{"id": "m1", "name": "gpt-4o", "context_window": 128000}],
            }
        ],
        "memory_compression": {"retain_count": 8},
    }


# ---------------------------------------------------------------------------
# ① 合法值往返
# ---------------------------------------------------------------------------

def test_save_then_load_round_trip(cfg_env):
    """save_watermark_base(400000) 后 load 回来仍是 400000，且落在嵌套键上。"""
    assert cw.save_watermark_base(400000) == 400000

    assert cw.load_watermark_base() == 400000
    on_disk = _read_config(cfg_env)
    assert on_disk["memory_compression"]["watermark_base_tokens"] == 400000


@pytest.mark.parametrize("value", [cw.MIN_WATERMARK_BASE, cw.MAX_WATERMARK_BASE])
def test_boundary_values_are_accepted(cfg_env, value):
    """区间端点 50000 / 2000000 合法。"""
    assert cw.validate_watermark_base(value) == value
    assert cw.save_watermark_base(value) == value
    assert cw.load_watermark_base() == value


def test_constants_contract():
    """常量契约：配置路径与区间边界。"""
    assert cw.WATERMARK_BASE_CONFIG_PATH == "memory_compression.watermark_base_tokens"
    assert cw.MIN_WATERMARK_BASE == 50000
    assert cw.MAX_WATERMARK_BASE == 2000000
    assert issubclass(cw.WatermarkBaseError, ValueError)


def test_validate_numeric_string_is_normalized(cfg_env):
    """数字字符串按整数处理（设置面板回传文本时不报错）。"""
    assert cw.validate_watermark_base("400000") == 400000


def test_load_returns_none_when_key_missing(cfg_env):
    """未配置 → None（不设基准、跟随真实模型窗口）。"""
    _write_config(cfg_env, _sample_config())

    assert cw.load_watermark_base() is None


def test_load_ignores_illegal_value_without_raising(cfg_env):
    """磁盘上是非法值时读取安全回落 None（读取绝不抛错）。"""
    _write_config(
        cfg_env,
        {"memory_compression": {"watermark_base_tokens": "abc"}},
    )

    assert cw.load_watermark_base() is None


# ---------------------------------------------------------------------------
# ② 0 / None / "" → 不设基准，且能把已有基准清掉
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [0, None, ""])
def test_unset_values_validate_to_none(value):
    assert cw.validate_watermark_base(value) is None


@pytest.mark.parametrize("value", [0, None, ""])
def test_unset_values_clear_existing_base(cfg_env, value):
    """保存 0 / None / "" 能把已有基准值清掉，load 返回 None。"""
    cw.save_watermark_base(400000)
    assert cw.load_watermark_base() == 400000

    assert cw.save_watermark_base(value) is None

    assert cw.load_watermark_base() is None


# ---------------------------------------------------------------------------
# ③ 非法值：抛 WatermarkBaseError 且不写盘
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_value",
    [49999, 2000001, "abc", 1.5, True, False, -1, [], {}],
)
def test_invalid_values_raise_and_do_not_touch_disk(cfg_env, bad_value):
    cw.save_watermark_base(400000)
    before = cfg_env.read_bytes()

    with pytest.raises(cw.WatermarkBaseError):
        cw.validate_watermark_base(bad_value)
    with pytest.raises(cw.WatermarkBaseError):
        cw.save_watermark_base(bad_value)

    assert cfg_env.read_bytes() == before
    assert cw.load_watermark_base() == 400000


def test_save_failure_raises_watermark_base_error(cfg_env, monkeypatch):
    """底层写入失败 → WatermarkBaseError("...保存失败")。"""
    monkeypatch.setattr(config, "save_nested_config_value", lambda key, value: False)

    with pytest.raises(cw.WatermarkBaseError) as excinfo:
        cw.save_watermark_base(400000)

    assert "保存失败" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ④ save_nested_config_value：写嵌套键不丢其他顶层键
# ---------------------------------------------------------------------------

def test_save_nested_config_value_keeps_other_keys(cfg_env):
    _write_config(cfg_env, _sample_config())

    assert config.save_nested_config_value("memory_compression.retain_count", 3) is True

    on_disk = _read_config(cfg_env)
    assert on_disk["memory_compression"]["retain_count"] == 3
    assert on_disk["theme"] == "cyan"
    assert on_disk["active_model_id"] == "m1"
    assert on_disk["providers"] == _sample_config()["providers"]


def test_save_watermark_base_keeps_other_keys(cfg_env):
    """走完整保存链路（含水位基准写入）同样不丢其他顶层键。"""
    _write_config(cfg_env, _sample_config())

    cw.save_watermark_base(400000)

    on_disk = _read_config(cfg_env)
    assert on_disk["memory_compression"] == {
        "retain_count": 8,
        "watermark_base_tokens": 400000,
    }
    assert on_disk["theme"] == "cyan"
    assert on_disk["providers"] == _sample_config()["providers"]


def test_save_nested_config_value_creates_missing_parent(cfg_env):
    _write_config(cfg_env, {"theme": "cyan"})

    assert config.save_nested_config_value("memory_compression.retain_count", 5) is True

    on_disk = _read_config(cfg_env)
    assert on_disk["memory_compression"]["retain_count"] == 5
    assert on_disk["theme"] == "cyan"


def test_save_nested_config_value_rejects_invalid_path(cfg_env, monkeypatch):
    """只支持两层点分路径；非法路径记告警并返回 False（不抛给调用方）。"""
    warnings = []
    monkeypatch.setattr(
        config,
        "_log_warning",
        lambda message, *args: warnings.append(message % args if args else message),
    )
    _write_config(cfg_env, _sample_config())
    before = cfg_env.read_bytes()

    assert config.save_nested_config_value("theme", "dark") is False
    assert config.save_nested_config_value("a.b.c", 1) is False

    assert cfg_env.read_bytes() == before
    assert warnings


# ---------------------------------------------------------------------------
# ⑤ 白名单：watermark_base_tokens / model_watermark_bases 不再误报
# ---------------------------------------------------------------------------

def test_watermark_nested_keys_are_whitelisted():
    data = {"memory_compression": {"watermark_base_tokens": 400000}}

    assert config.find_unknown_config_keys(data) == []


def test_model_watermark_bases_key_is_whitelisted():
    data = {"memory_compression": {"model_watermark_bases": {"gpt-5": 250000}}}

    assert config.find_unknown_config_keys(data) == []


def test_unknown_nested_key_still_detected():
    """白名单只补登记这两个键，拼写错误仍必须命中。"""
    data = {"memory_compression": {"watermark_base_token": 400000}}

    assert config.find_unknown_config_keys(data) == [
        "memory_compression.watermark_base_token"
    ]
