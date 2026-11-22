import json

import pytest

import config
import core.context_watermark as cw


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
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


def test_save_then_load_round_trip(cfg_env):
    assert cw.save_watermark_base(400000) == 400000

    assert cw.load_watermark_base() == 400000
    on_disk = _read_config(cfg_env)
    assert on_disk["memory_compression"]["watermark_base_tokens"] == 400000


@pytest.mark.parametrize("value", [cw.MIN_WATERMARK_BASE, cw.MAX_WATERMARK_BASE])
def test_boundary_values_are_accepted(cfg_env, value):
    assert cw.validate_watermark_base(value) == value
    assert cw.save_watermark_base(value) == value
    assert cw.load_watermark_base() == value


def test_constants_contract():
    assert cw.WATERMARK_BASE_CONFIG_PATH == "memory_compression.watermark_base_tokens"
    assert cw.MIN_WATERMARK_BASE == 50000
    assert cw.MAX_WATERMARK_BASE == 2000000
    assert issubclass(cw.WatermarkBaseError, ValueError)


def test_validate_numeric_string_is_normalized(cfg_env):
    assert cw.validate_watermark_base("400000") == 400000


def test_load_returns_none_when_key_missing(cfg_env):
    _write_config(cfg_env, _sample_config())

    assert cw.load_watermark_base() is None


def test_load_ignores_illegal_value_without_raising(cfg_env):
    _write_config(
        cfg_env,
        {"memory_compression": {"watermark_base_tokens": "abc"}},
    )

    assert cw.load_watermark_base() is None


@pytest.mark.parametrize("value", [0, None, ""])
def test_unset_values_validate_to_none(value):
    assert cw.validate_watermark_base(value) is None


@pytest.mark.parametrize("value", [0, None, ""])
def test_unset_values_clear_existing_base(cfg_env, value):
    cw.save_watermark_base(400000)
    assert cw.load_watermark_base() == 400000

    assert cw.save_watermark_base(value) is None

    assert cw.load_watermark_base() is None


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
    monkeypatch.setattr(config, "save_nested_config_value", lambda key, value: False)

    with pytest.raises(cw.WatermarkBaseError) as excinfo:
        cw.save_watermark_base(400000)

    assert "保存失败" in str(excinfo.value)


def test_save_nested_config_value_keeps_other_keys(cfg_env):
    _write_config(cfg_env, _sample_config())

    assert config.save_nested_config_value("memory_compression.retain_count", 3) is True

    on_disk = _read_config(cfg_env)
    assert on_disk["memory_compression"]["retain_count"] == 3
    assert on_disk["theme"] == "cyan"
    assert on_disk["active_model_id"] == "m1"
    assert on_disk["providers"] == _sample_config()["providers"]


def test_save_watermark_base_keeps_other_keys(cfg_env):
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


def test_watermark_nested_keys_are_whitelisted():
    data = {"memory_compression": {"watermark_base_tokens": 400000}}

    assert config.find_unknown_config_keys(data) == []


def test_model_watermark_bases_key_is_whitelisted():
    data = {"memory_compression": {"model_watermark_bases": {"gpt-5": 250000}}}

    assert config.find_unknown_config_keys(data) == []


def test_unknown_nested_key_still_detected():
    data = {"memory_compression": {"watermark_base_token": 400000}}

    assert config.find_unknown_config_keys(data) == [
        "memory_compression.watermark_base_token"
    ]
