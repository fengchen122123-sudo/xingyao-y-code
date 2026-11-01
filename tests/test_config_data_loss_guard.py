"""回归测试：配置文件读失败时绝不能把用户的厂商/模型配置覆盖成默认值。

历史事故：配置在一次重启后「全部丢失」。根因链是
主文件读取失败 → _read_json_with_backup 返回 None → load_config 当成首次运行
→ 把默认 DeepSeek 结构写回磁盘，17 个厂商配置被 1 个默认厂商永久替换。
"""

import json

import config
import core.model_manager as model_manager


def _point_config(tmp_path, monkeypatch, raw_text):
    cfg = tmp_path / "config.json"
    cfg.write_text(raw_text, encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(model_manager, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(config, "_CONFIG_CACHE", {})
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(tmp_path / "config.toml"))
    return cfg


def test_load_config_does_not_overwrite_corrupt_file(tmp_path, monkeypatch):
    """主文件损坏且无备份：内存给默认结构，磁盘原文件必须原样保留。"""
    broken = '{ "active_model_id": "abc", "providers": [ {"id": "p1"'
    cfg = _point_config(tmp_path, monkeypatch, broken)

    data = model_manager.load_config()

    assert len(data.get("providers") or []) == 1
    assert cfg.read_text(encoding="utf-8") == broken


def test_load_config_recovers_from_history_backup(tmp_path, monkeypatch):
    """主文件损坏但存在历史备份：从备份恢复并保留损坏现场。"""
    broken = "{ 这不是合法 JSON"
    cfg = _point_config(tmp_path, monkeypatch, broken)
    backup_data = {
        "active_model_id": "m1",
        "providers": [
            {
                "id": "p1",
                "name": "厂商A",
                "base_url": "https://a.example.com",
                "api_key": "",
                "protocol": "openai",
                "models": [{"id": "m1", "name": "a", "litellm_model": "openai/a"}],
            },
            {
                "id": "p2",
                "name": "厂商B",
                "base_url": "https://b.example.com",
                "api_key": "",
                "protocol": "openai",
                "models": [],
            },
        ],
    }
    backup = tmp_path / "config.json.bak.20260101000000"
    backup.write_text(json.dumps(backup_data, ensure_ascii=False), encoding="utf-8")

    data = model_manager.load_config()

    assert [provider["id"] for provider in data["providers"]] == ["p1", "p2"]
    assert cfg.read_text(encoding="utf-8") != broken
    assert list(tmp_path.glob("config.json.corrupt-*"))
    # 自愈后走正常读取路径，仍然是两个厂商
    assert len(model_manager.load_config()["providers"]) == 2


def test_save_config_value_refuses_when_config_unreadable(tmp_path, monkeypatch):
    """读不出配置时拒绝单键写入，避免把文件覆盖成只剩这一个键。"""
    broken = "{oops"
    cfg = _point_config(tmp_path, monkeypatch, broken)

    assert config.save_config_value("theme", "dark") is False
    assert cfg.read_text(encoding="utf-8") == broken


def test_update_config_refuses_when_config_unreadable(tmp_path, monkeypatch):
    """读不出配置时连 update_config 的 mutator 都不该执行，更不该落盘。"""
    broken = "{oops"
    cfg = _point_config(tmp_path, monkeypatch, broken)
    called = []

    data = config.update_config(lambda current: called.append(True))

    assert data == {}
    assert not called
    assert cfg.read_text(encoding="utf-8") == broken
