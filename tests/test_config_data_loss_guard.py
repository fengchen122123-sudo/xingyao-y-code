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
 
def _two_providers_config(): 
    """最小可用的两厂商配置：用于断言写入操作没有把 providers 清空。""" 
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
    """合法但空的 ``{}`` 配置：单键写入不得把文件覆盖成「只剩这一个键」。

    机制：``config._config_read_failed(path)`` 只在「文件存在且有内容但解析失败」时
    返回 True，内容是合法空对象 ``{}`` 时返回 False，于是 save_config_value 会拿空
    dict 加上一个键整体写盘，用户的全部 providers 被清空，插件自举再重建托管厂商，
    用户侧表现就是「模型配置全丢」。空对象同样意味着「读不到用户数据」，必须与读
    失败一样拒绝覆盖，或至少保留 providers 键。
    """ 
    cfg = _point_config(tmp_path, monkeypatch, "{}") 
 
    saved = config.save_config_value("theme", "dark") 
 
    raw = cfg.read_text(encoding="utf-8") 
    data = json.loads(raw) if raw.strip() else {} 
    # 两种可接受行为：写入被拒绝且文件原样不动；或写入后 providers 键依旧存在 
    assert "providers" in data or (saved is False and data == {}), ( 
        f"空对象配置被单键写入覆盖成了 {data!r}（返回值 {saved!r}）：" 
        "用户的 providers 会被整体清空" 
    ) 
 
 
def test_zero_byte_config_not_wiped_by_single_key_save(tmp_path, monkeypatch): 
    """0 字节配置：单键写入不得把文件覆盖成单键配置。

    机制：``_config_read_failed`` 对 0 字节文件直接 ``return False``（把「空文件」
    当成「没有用户数据可丢」），save_config_value 于是用空 dict 写盘，只留下
    ``{"theme": "dark"}``，用户原有的厂商/模型配置无法从主文件恢复。0 字节与
    「有内容但解析失败」在数据丢失风险上等价，写路径必须一并拒绝。
    """ 
    cfg = _point_config(tmp_path, monkeypatch, "") 
 
    saved = config.save_config_value("theme", "dark") 
 
    raw = cfg.read_text(encoding="utf-8") 
    data = json.loads(raw) if raw.strip() else {} 
    assert "providers" in data or (saved is False and raw.strip() == ""), ( 
        f"0 字节配置被单键写入覆盖成了 {data!r}（返回值 {saved!r}）" 
    ) 
    assert data != {"theme": "dark"}, "0 字节配置被写成了只含单个键的配置" 
 
 
def test_deleted_config_restored_from_backup_on_read(tmp_path, monkeypatch): 
    """主文件被删除但同目录有 ``config.json.bak``：读取应恢复 2 个厂商。

    机制：``_read_json_with_backup`` 只在 ``FileNotFoundError``（主文件不存在）时
    直接返回 default，不会走 ``_recover_from_history_backups``；``load_config`` 又用
    ``os.path.exists(_CONFIG_FILE)`` 判断「读失败」，文件不存在时判定为「首次运行」，
    于是写回 DeepSeek 默认结构并自举重建托管厂商，「文件被删」被当成首次运行，
    用户的 2 个厂商配置就此丢失。存在可用备份时，删除等同读取失败，必须从备份恢复。
    """ 
    cfg = _point_config(tmp_path, monkeypatch, "") 
    cfg.unlink()  # 模拟用户/进程把主文件删掉 
    backup = tmp_path / "config.json.bak" 
    backup.write_text(json.dumps(_two_providers_config(), ensure_ascii=False), encoding="utf-8") 
 
    data = model_manager.load_config() 
 
    assert [provider["id"] for provider in data.get("providers") or []] == ["p1", "p2"] 
    # 备份文件本身不应被就地消耗掉 
    assert json.loads(backup.read_text(encoding="utf-8"))["providers"] 
 
 
def test_write_refused_when_providers_dropped_to_empty(tmp_path, monkeypatch): 
    """已有 2 个厂商时把 providers 改成 [] 的写入必须被拒绝。

    机制：``update_config`` 只靠 ``_config_read_failed`` 挡住「解析失败」的文件，
    对能正常解析的配置不做任何数据保全校验，因此 mutator 里 ``providers: []`` 会被
    原样写盘（随后自举再重建 3 个托管厂商），用户看到的仍是「配置全丢」。写入路径
    必须在 providers 从非空掉到空时拒绝落盘并返回原配置。
    """ 
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
