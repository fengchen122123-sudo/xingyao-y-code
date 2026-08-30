"""tests/test_config_toml_overlay.py — config.toml 只读兼容层测试。

隔离策略：
- monkeypatch config._CONFIG_FILE / config._TOML_CONFIG_FILE 到 pytest tmp_path；
- 每个用例前后 dict.clear() 清空 config._CONFIG_CACHE 与 config._TOML_CACHE，
  避免 mtime 缓存串扰；
- 不写 providers 数组，避开 DPAPI 加解密路径。
"""

import json
import os
import threading
from pathlib import Path

import pytest

import config


@pytest.fixture
def toml_env(tmp_path, monkeypatch):
    """隔离配置环境：返回 (config.json 路径, config.toml 路径)。"""
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
    """无 TOML 回归：只有 config.json 时 _load 返回 JSON 原值，行为与改动前一致。"""
    cfg_path, toml_path = toml_env
    assert not toml_path.exists()
    cfg_path.write_text(json.dumps({"theme": "json-theme", "count": 1}), encoding="utf-8")

    assert config._load() == {"theme": "json-theme", "count": 1}


def test_toml_overrides_top_level_scalar(toml_env):
    """覆盖顶层标量：同名键以 TOML 值为准，JSON 独有键保留。"""
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"theme": "json-theme", "count": 1}), encoding="utf-8")
    toml_path.write_text('theme = "toml-theme"\n', encoding="utf-8")

    data = config._load()

    assert data["theme"] == "toml-theme"
    assert data["count"] == 1


def test_nested_dict_deep_merge(toml_env):
    """嵌套深合并：TOML 只覆盖一个子键，其余子键保留 JSON 原值（递归合并而非整体替换）。"""
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
    """list 整体替换：TOML 的列表覆盖 JSON 同名列表，不做元素级合并。"""
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"tags": ["a", "b", "c"]}), encoding="utf-8")
    toml_path.write_text('tags = ["x"]\n', encoding="utf-8")

    assert config._load()["tags"] == ["x"]


def test_invalid_toml_falls_back_to_json(toml_env):
    """TOML 语法错误：不抛异常，回退纯 JSON 原值。"""
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"theme": "json-theme"}), encoding="utf-8")
    toml_path.write_text("broken = [unclosed", encoding="utf-8")

    assert config._load() == {"theme": "json-theme"}


def test_update_config_writes_merged_values_back_to_json(toml_env):
    """update_config 写回：落盘的 config.json 含合并值，且文件格式为合法 JSON。"""
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    toml_path.write_text("b = 2\n", encoding="utf-8")

    result = config.update_config(lambda data: data.__setitem__("c", 3))

    assert result == {"a": 1, "b": 2, "c": 3}
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk == {"a": 1, "b": 2, "c": 3}


def test_ycode_dir_points_to_isolated_data_dir():
    """YCODE_DATA_DIR 隔离：测试环境下 YCODE_DIR 指向隔离目录而非真实 ~/.ycode。"""
    config._CONFIG_CACHE.clear()
    config._TOML_CACHE.clear()

    data_dir = os.environ.get("YCODE_DATA_DIR")
    assert data_dir, "conftest.py 必须在收集阶段设置 YCODE_DATA_DIR 隔离环境变量"
    assert Path(config.YCODE_DIR) == Path(data_dir)
    assert Path(config.YCODE_DIR).name.startswith("ycode-pytest-")
    # TOML 与 JSON 路径默认由 YCODE_DIR 拼装，落在隔离目录内
    assert config._TOML_CONFIG_FILE == os.path.join(config.YCODE_DIR, "config.toml")
    assert config._CONFIG_FILE == os.path.join(config.YCODE_DIR, "config.json")
    assert str(Path(config.YCODE_DIR)) != str(Path.home() / ".ycode")


# ---------------------------------------------------------------------------
# 保存时 TOML 覆盖检测：TOML 是只读覆盖层且读取时后合并，被覆盖键的 JSON
# 写入"成功却不生效"，必须在保存路径上告警留痕
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_warnings(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        config, "_log_warning", lambda msg, *args: warnings.append(msg % args if args else msg)
    )
    return warnings


def test_save_config_value_warns_when_key_overridden_by_toml(toml_env, captured_warnings):
    """save_config_value 写入被 TOML 覆盖的键：JSON 落盘成功且必须发出覆盖告警。"""
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"theme": "json-theme"}), encoding="utf-8")
    toml_path.write_text('theme = "toml-theme"\n', encoding="utf-8")

    assert config.save_config_value("theme", "json-new") is True

    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["theme"] == "json-new"
    assert any("theme" in warning for warning in captured_warnings)


def test_save_config_value_no_warning_without_toml_conflict(toml_env, captured_warnings):
    """保存未被 TOML 覆盖的键：不产生告警，行为与改动前一致。"""
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"theme": "json-theme"}), encoding="utf-8")
    toml_path.write_text('other = "x"\n', encoding="utf-8")

    assert config.save_config_value("theme", "json-new") is True
    assert captured_warnings == []


def test_save_config_value_no_warning_without_toml_file(toml_env, captured_warnings):
    """无 config.toml 时保存：绝不告警。"""
    cfg_path, _ = toml_env
    cfg_path.write_text(json.dumps({"theme": "json-theme"}), encoding="utf-8")

    assert config.save_config_value("theme", "json-new") is True
    assert captured_warnings == []


def test_update_config_warns_only_for_changed_toml_keys(toml_env, captured_warnings):
    """update_config：只有 mutator 真正改动被 TOML 覆盖的键时才告警。"""
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"theme": "json", "count": 1}), encoding="utf-8")
    toml_path.write_text('theme = "toml"\n', encoding="utf-8")

    # 只改未被 TOML 覆盖的键：不应告警
    config.update_config(lambda data: data.__setitem__("count", 2))
    assert captured_warnings == []

    # 改动被 TOML 覆盖的键：必须告警
    config.update_config(lambda data: data.__setitem__("theme", "json-new"))
    assert any("theme" in warning for warning in captured_warnings)


def test_update_config_warns_when_toml_key_deleted(toml_env, captured_warnings):
    """mutator 删除被 TOML 覆盖的键：下次读取会被 TOML 复活，同样必须告警。"""
    cfg_path, toml_path = toml_env
    cfg_path.write_text(json.dumps({"theme": "json"}), encoding="utf-8")
    toml_path.write_text('theme = "toml"\n', encoding="utf-8")

    def _delete(data):
        data.pop("theme")

    config.update_config(_delete)
    assert any("theme" in warning for warning in captured_warnings)


def test_concurrent_load_with_toml_stays_consistent(toml_env):
    """缓存读写纳入 _CONFIG_LOCK：多线程并发 _load 结果一致且无异常。"""
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
