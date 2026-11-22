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
    broken = '{ "active_model_id": "abc", "providers": [ {"id": "p1"'
    cfg = _point_config(tmp_path, monkeypatch, broken)

    data = model_manager.load_config()

    assert len(data.get("providers") or []) == 1
    assert cfg.read_text(encoding="utf-8") == broken


def test_load_config_recovers_from_history_backup(tmp_path, monkeypatch):
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
    assert len(model_manager.load_config()["providers"]) == 2


def test_save_config_value_refuses_when_config_unreadable(tmp_path, monkeypatch):
    broken = "{oops"
    cfg = _point_config(tmp_path, monkeypatch, broken)

    assert config.save_config_value("theme", "dark") is False
    assert cfg.read_text(encoding="utf-8") == broken


def test_update_config_refuses_when_config_unreadable(tmp_path, monkeypatch):
    broken = "{oops"
    cfg = _point_config(tmp_path, monkeypatch, broken)
    called = []

    data = config.update_config(lambda current: called.append(True))

    assert data == {}
    assert not called
    assert cfg.read_text(encoding="utf-8") == broken
 
def _two_providers_config(): 
    return { 
 
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
 
 
def test_empty_object_config_not_wiped_by_single_key_save(tmp_path, monkeypatch): 
    cfg = _point_config(tmp_path, monkeypatch, "{}") 
 
    saved = config.save_config_value("theme", "dark") 
 
    raw = cfg.read_text(encoding="utf-8") 
    data = json.loads(raw) if raw.strip() else {} 
    assert "providers" in data or (saved is False and data == {}), ( 
        f"空对象配置被单键写入覆盖成了 {data!r}（返回值 {saved!r}）：" 
        "用户的 providers 会被整体清空" 
    ) 
 
 
def test_zero_byte_config_not_wiped_by_single_key_save(tmp_path, monkeypatch): 
    cfg = _point_config(tmp_path, monkeypatch, "") 
 
    saved = config.save_config_value("theme", "dark") 
 
    raw = cfg.read_text(encoding="utf-8") 
    data = json.loads(raw) if raw.strip() else {} 
    assert "providers" in data or (saved is False and raw.strip() == ""), ( 
        f"0 字节配置被单键写入覆盖成了 {data!r}（返回值 {saved!r}）" 
    ) 
    assert data != {"theme": "dark"}, "0 字节配置被写成了只含单个键的配置" 
 
 
def test_deleted_config_restored_from_backup_on_read(tmp_path, monkeypatch): 
    cfg = _point_config(tmp_path, monkeypatch, "") 
    cfg.unlink()
    backup = tmp_path / "config.json.bak" 
    backup.write_text(json.dumps(_two_providers_config(), ensure_ascii=False), encoding="utf-8") 
 
    data = model_manager.load_config() 
 
    assert [provider["id"] for provider in data.get("providers") or []] == ["p1", "p2"] 
    assert json.loads(backup.read_text(encoding="utf-8"))["providers"] 
 
 
def test_write_refused_when_providers_dropped_to_empty(tmp_path, monkeypatch): 
    original = _two_providers_config() 
    cfg = _point_config(tmp_path, monkeypatch, json.dumps(original, ensure_ascii=False)) 
 
    data = config.update_config(lambda current: current.update({"providers": []})) 
 
    on_disk = json.loads(cfg.read_text(encoding="utf-8")) 
    assert [provider["id"] for provider in on_disk.get("providers") or []] == ["p1", "p2"], ( 
        f"providers 被清空的写入落盘了：{on_disk!r}" 
    ) 
    assert [provider["id"] for provider in data.get("providers") or []] == ["p1", "p2"], ( 
        "写入被拒绝时应返回原配置，而不是被清空后的配置" 
    ) 
