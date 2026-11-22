import json
import os
import threading
from pathlib import Path

import pytest

import config


@pytest.fixture
def toml_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    toml_path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(toml_path))
    config._CONFIG_CACHE.clear()
    config._TOML_CACHE.clear()
    yield cfg_path, toml_path
    config._CONFIG_CACHE.clear()
    config._TOML_CACHE.clear()


def test_no_toml_falls_back_to_pure_json(toml_env):
    cfg_path, toml_path = toml_env
    assert not toml_path.exists()
    cfg_path.write_text(json.dumps({"theme": "json-theme", "count": 1}), encoding="utf-8")

    assert config._load() == {"theme": "json-theme", "count": 1}


def test_toml_overrides_top_level_scalar(toml_env):
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"theme": "json-theme", "count": 1}), encoding="utf-8")
    toml_path.write_text('theme = "toml-theme"\n', encoding="utf-8")

    data = config._load()

    assert data["theme"] == "toml-theme"
    assert data["count"] == 1


def test_nested_dict_deep_merge(toml_env):
    cfg_path, toml_path = toml_env
    cfg_path.write_text(
        json.dumps({"ui": {"theme": "json", "lang": "zh", "font": "mono"}, "keep": True}),
        encoding="utf-8",
    )
    toml_path.write_text('[ui]\ntheme = "toml"\n', encoding="utf-8")

    data = config._load()

    assert data["ui"] == {"theme": "toml", "lang": "zh", "font": "mono"}
    assert data["keep"] is True


def test_list_is_replaced_wholesale(toml_env):
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"tags": ["a", "b", "c"]}), encoding="utf-8")
    toml_path.write_text('tags = ["x"]\n', encoding="utf-8")

    assert config._load()["tags"] == ["x"]


def test_invalid_toml_falls_back_to_json(toml_env):
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"theme": "json-theme"}), encoding="utf-8")
    toml_path.write_text("broken = [unclosed", encoding="utf-8")

    assert config._load() == {"theme": "json-theme"}


def test_update_config_writes_merged_values_back_to_json(toml_env):
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    toml_path.write_text("b = 2\n", encoding="utf-8")

    result = config.update_config(lambda data: data.__setitem__("c", 3))

    assert result == {"a": 1, "b": 2, "c": 3}
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk == {"a": 1, "b": 2, "c": 3}


def test_ycode_dir_points_to_isolated_data_dir():
    config._CONFIG_CACHE.clear()
    config._TOML_CACHE.clear()

    data_dir = os.environ.get("YCODE_DATA_DIR")
    assert data_dir, "conftest.py 必须在收集阶段设置 YCODE_DATA_DIR 隔离环境变量"
    assert Path(config.YCODE_DIR) == Path(data_dir)
    assert Path(config.YCODE_DIR).name.startswith("ycode-pytest-")
    assert config._TOML_CONFIG_FILE == os.path.join(config.YCODE_DIR, "config.toml")
    assert config._CONFIG_FILE == os.path.join(config.YCODE_DIR, "config.json")
    assert str(Path(config.YCODE_DIR)) != str(Path.home() / ".ycode")


@pytest.fixture
def captured_warnings(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        config, "_log_warning", lambda msg, *args: warnings.append(msg % args if args else msg)
    )
    return warnings


def test_save_config_value_warns_when_key_overridden_by_toml(toml_env, captured_warnings):
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"theme": "json-theme"}), encoding="utf-8")
    toml_path.write_text('theme = "toml-theme"\n', encoding="utf-8")

    assert config.save_config_value("theme", "json-new") is True

    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["theme"] == "json-new"
    assert any("theme" in warning for warning in captured_warnings)


def test_save_config_value_no_warning_without_toml_conflict(toml_env, captured_warnings):
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"theme": "json-theme"}), encoding="utf-8")
    toml_path.write_text('other = "x"\n', encoding="utf-8")

    assert config.save_config_value("theme", "json-new") is True
    assert captured_warnings == []


def test_save_config_value_no_warning_without_toml_file(toml_env, captured_warnings):
    cfg_path, _ = toml_env
    cfg_path.write_text(json.dumps({"theme": "json-theme"}), encoding="utf-8")

    assert config.save_config_value("theme", "json-new") is True
    assert captured_warnings == []


def test_update_config_warns_only_for_changed_toml_keys(toml_env, captured_warnings):
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"theme": "json", "count": 1}), encoding="utf-8")
    toml_path.write_text('theme = "toml"\n', encoding="utf-8")

    config.update_config(lambda data: data.__setitem__("count", 2))
    assert captured_warnings == []

    config.update_config(lambda data: data.__setitem__("theme", "json-new"))
    assert any("theme" in warning for warning in captured_warnings)


def test_update_config_warns_when_toml_key_deleted(toml_env, captured_warnings):
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"theme": "json"}), encoding="utf-8")
    toml_path.write_text('theme = "toml"\n', encoding="utf-8")

    def _delete(data):
        data.pop("theme")

    config.update_config(_delete)
    assert any("theme" in warning for warning in captured_warnings)


def test_concurrent_load_with_toml_stays_consistent(toml_env):
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"count": 1}), encoding="utf-8")
    toml_path.write_text('name = "toml"\n', encoding="utf-8")

    errors = []

    def _worker():
        try:
            for _ in range(50):
                assert config._load() == {"count": 1, "name": "toml"}
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
