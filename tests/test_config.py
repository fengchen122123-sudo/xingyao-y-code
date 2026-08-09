import json
import threading
from pathlib import Path

import config
import core.model_manager as model_manager


def _provider_config():
    return {
        "active_model_id": "m1",
        "providers": [
            {
                "id": "p1",
                "name": "测试厂商",
                "base_url": "https://api.example.com",
                "api_key": "sk-test-key-123",
                "protocol": "openai",
                "models": [
                    {
                        "id": "m1",
                        "name": "gpt-4o",
                        "litellm_model": "openai/gpt-4o",
                        "context_window": 128000,
                    }
                ],
            }
        ],
    }


def _use_tmp_config(tmp_path, monkeypatch, data=None):
    cfg = tmp_path / "config.json"
    if data is not None:
        cfg.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(model_manager, "_CONFIG_FILE", str(cfg))
    # model_manager 写路径经由 config.update_config，指向同一份临时配置
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    return cfg


def test_load_valid_config(tmp_path, monkeypatch):
    """测试是否能正确读取多厂商结构的 config.json 并解析激活模型"""
    _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    data = model_manager.load_config()
    assert data["active_model_id"] == "m1"

    info = model_manager.get_active_model()
    assert info["litellm_model"] == "openai/gpt-4o"
    assert info["api_key"] == "sk-test-key-123"
    assert info["base_url"] == "https://api.example.com"


def test_load_config_file_not_found_creates_default(tmp_path, monkeypatch):
    """配置文件不存在时，自动初始化默认多厂商结构而不是报错"""
    cfg = _use_tmp_config(tmp_path, monkeypatch)

    data = model_manager.load_config()

    assert data["providers"], "应自动写入默认 providers 结构"
    assert cfg.exists(), "默认配置应被持久化到磁盘"


def test_default_deepseek_v4_models_use_explicit_thinking_contract(
    tmp_path, monkeypatch
):
    _use_tmp_config(tmp_path, monkeypatch)

    data = model_manager.load_config()
    deepseek_models = next(
        provider["models"]
        for provider in data["providers"]
        if provider["base_url"] == "https://api.deepseek.com"
    )

    assert deepseek_models
    assert all(model["enable_thinking"] is True for model in deepseek_models)
    assert all(model["reasoning_effort"] == "max" for model in deepseek_models)


def test_corrupt_config_is_restored_from_backup(tmp_path, monkeypatch):
    """主配置写坏时优先从 .bak 恢复（含 API Key），而不是静默重置为默认"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    # 通过一次正常写入生成 .bak 备份
    model_manager._write_config(model_manager.load_config())
    assert Path(str(cfg) + ".bak").exists()

    # 模拟写一半崩溃造成的损坏
    cfg.write_text('{"active_model_id": "m1", "provi', encoding="utf-8")

    data = model_manager.load_config()

    assert data["active_model_id"] == "m1"
    provider = data["providers"][0]
    assert provider["api_key"] == "sk-test-key-123"
    # 自愈：主文件已被备份内容回写
    assert json.loads(cfg.read_text(encoding="utf-8"))["active_model_id"] == "m1"


def test_update_config_mutator_runs_and_persists(tmp_path, monkeypatch):
    """update_config 在锁内 load→mutate→原子写，结果持久化到磁盘"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"a": 1}), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))

    result = config.update_config(lambda data: data.__setitem__("b", 2))

    assert result == {"a": 1, "b": 2}  # 返回实际写入的配置 dict
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk == {"a": 1, "b": 2}


def test_update_config_concurrent_increments_lose_nothing(tmp_path, monkeypatch):
    """并发读-改-写必须在锁内完成：4 线程 x 25 次自增结果必须是 100（无 TOCTOU 丢失更新）"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"count": 0}), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))

    def worker():
        for _ in range(25):
            config.update_config(lambda data: data.__setitem__("count", data["count"] + 1))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk["count"] == 100


def test_set_active_model_preserves_unrelated_keys(tmp_path, monkeypatch):
    """set_active_model 走锁内读-改-写，不得覆盖其它并发写入的配置键"""
    data = _provider_config()
    data["theme"] = "dark"
    cfg = _use_tmp_config(tmp_path, monkeypatch, data)

    model_manager.set_active_model("m1")

    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk["active_model_id"] == "m1"
    assert on_disk["theme"] == "dark"


def test_add_model_with_prefixed_name_not_double_prefixed(tmp_path, monkeypatch):
    """用户输入带前缀模型名（openai/gpt-4o）不得被加成 openai/openai/gpt-4o"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    mid = model_manager.add_model_to_provider("p1", "openai/gpt-4o", 128000)

    data = json.loads(cfg.read_text(encoding="utf-8"))
    added = [m for m in data["providers"][0]["models"] if m["id"] == mid][0]
    assert added["litellm_model"] == "openai/gpt-4o"


def test_add_model_without_prefix_gets_protocol_prefix(tmp_path, monkeypatch):
    """裸模型名仍按厂商协议补前缀"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    mid = model_manager.add_model_to_provider("p1", "gpt-4o-mini", 128000)

    data = json.loads(cfg.read_text(encoding="utf-8"))
    added = [m for m in data["providers"][0]["models"] if m["id"] == mid][0]
    assert added["litellm_model"] == "openai/gpt-4o-mini"


def test_update_provider_and_model_prefixed_name_not_double_prefixed(tmp_path, monkeypatch):
    """编辑保存路径同样对已含 / 前缀的模型名防重"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    model_manager.update_provider_and_model(
        "p1", "m1", "https://api.example.com", "sk-k", "anthropic/claude", 200000,
    )

    data = json.loads(cfg.read_text(encoding="utf-8"))
    model = data["providers"][0]["models"][0]
    assert model["litellm_model"] == "anthropic/claude"
    assert model["context_window"] == 200000


def test_set_model_reasoning_effort_roundtrip(tmp_path, monkeypatch):
    """reasoning_effort 按模型写入/恢复默认，且落盘"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    ok, _ = model_manager.set_model_reasoning_effort("m1", "low")
    assert ok
    assert model_manager.get_model_reasoning_effort("m1") == "low"
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk["providers"][0]["models"][0]["reasoning_effort"] == "low"

    ok, _ = model_manager.set_model_reasoning_effort("m1", "default")
    assert ok
    assert model_manager.get_model_reasoning_effort("m1") is None
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert "reasoning_effort" not in on_disk["providers"][0]["models"][0]


def test_set_model_reasoning_effort_rejects_invalid(tmp_path, monkeypatch):
    """非法档位与不存在的 model_id 都不写盘"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    ok, msg = model_manager.set_model_reasoning_effort("m1", "extreme")
    assert not ok
    assert "extreme" in msg

    ok, msg = model_manager.set_model_reasoning_effort("missing", "low")
    assert not ok

    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert "reasoning_effort" not in on_disk["providers"][0]["models"][0]
