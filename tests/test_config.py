import json
import threading
from pathlib import Path

import pytest

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
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    data = model_manager.load_config()
    assert data["active_model_id"] == "m1"

    info = model_manager.get_active_model()
    assert info["litellm_model"] == "openai/gpt-4o"
    assert info["api_key"] == "sk-test-key-123"
    assert info["base_url"] == "https://api.example.com"
    on_disk = cfg.read_text(encoding="utf-8")
    assert "sk-test-key-123" not in on_disk
    assert "dpapi:" in on_disk


def test_non_windows_api_keys_use_system_keyring(monkeypatch):
    import core.credentials as credentials

    stored = {}

    class FakeKeyring:
        @staticmethod
        def set_password(service, account, value):
            stored[(service, account)] = value

        @staticmethod
        def get_password(service, account):
            return stored.get((service, account))

    monkeypatch.setattr(credentials.sys, "platform", "linux")
    monkeypatch.setitem(__import__("sys").modules, "keyring", FakeKeyring)

    protected = credentials.protect_secret("sk-portable-secret")

    assert protected.startswith(credentials.KEYRING_PREFIX)
    assert "sk-portable-secret" not in protected
    assert credentials.unprotect_secret(protected) == "sk-portable-secret"


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
    deepseek_provider = next(
        provider for provider in data["providers"]
        if provider["base_url"] == "https://api.deepseek.com"
    )
    assert deepseek_provider["protocol"] == "openai"
    assert deepseek_provider["preset_id"] == "deepseek"
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

    data = model_manager.load_config()
    added = [m for m in data["providers"][0]["models"] if m["id"] == mid][0]
    assert added["litellm_model"] == "openai/gpt-4o"


def test_add_model_without_prefix_gets_protocol_prefix(tmp_path, monkeypatch):
    """裸模型名仍按厂商协议补前缀"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    mid = model_manager.add_model_to_provider("p1", "gpt-4o-mini", 128000)

    data = model_manager.load_config()
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


def test_reasoning_effort_persists_with_thinking_enabled(tmp_path, monkeypatch):
    """写入具体强度时不能遗漏思考开启状态。"""
    data = _provider_config()
    model = data["providers"][0]["models"][0]
    model["name"] = "deepseek-v4-flash"
    model["litellm_model"] = "openai/deepseek-v4-flash"
    cfg = _use_tmp_config(tmp_path, monkeypatch, data)

    ok, _ = model_manager.set_model_reasoning_choice("m1", "high")

    assert ok
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))["providers"][0]["models"][0]
    assert on_disk["reasoning_choice"] == "high"
    assert on_disk["enable_thinking"] is True
    assert on_disk["reasoning_effort"] == "high"
    assert model_manager.get_model_reasoning_choice("m1") == "high"


def test_reasoning_off_and_default_remove_conflicting_overrides(tmp_path, monkeypatch):
    """关闭或恢复默认时不能残留会重新开启思考的强度字段。"""
    data = _provider_config()
    model = data["providers"][0]["models"][0]
    model["name"] = "deepseek-v4-flash"
    model["litellm_model"] = "openai/deepseek-v4-flash"
    model["thinking_budget"] = 4096
    cfg = _use_tmp_config(tmp_path, monkeypatch, data)

    assert model_manager.set_model_reasoning_choice("m1", "max")[0]
    assert model_manager.set_model_reasoning_choice("m1", "off")[0]
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))["providers"][0]["models"][0]
    assert on_disk["reasoning_choice"] == "off"
    assert on_disk["enable_thinking"] is False
    assert "reasoning_effort" not in on_disk
    assert "thinking_budget" not in on_disk

    assert model_manager.set_model_reasoning_choice("m1", "default")[0]
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))["providers"][0]["models"][0]
    assert "reasoning_choice" not in on_disk
    assert "enable_thinking" not in on_disk
    assert "reasoning_effort" not in on_disk
    assert "thinking_budget" not in on_disk
    assert model_manager.get_model_reasoning_choice("m1") == "default"


def test_fixed_reasoning_model_rejects_changes_without_writing(tmp_path, monkeypatch):
    """固定思考模型不能被设置接口伪装成可关闭模型。"""
    data = _provider_config()
    model = data["providers"][0]["models"][0]
    model["name"] = "minimax-m2.5"
    model["litellm_model"] = "openai/minimax-m2.5"
    cfg = _use_tmp_config(tmp_path, monkeypatch, data)
    model_manager.load_config()
    before = cfg.read_bytes()

    ok, message = model_manager.set_model_reasoning_choice("m1", "off")

    assert not ok
    assert "始终开启" in message
    assert cfg.read_bytes() == before


def test_unregistered_model_accepts_generic_reasoning_effort(tmp_path, monkeypatch):
    """未登记模型拿到通用强度能力后必须能真正持久化 default/off/强度档。

    未登记模型不再是 hidden（hidden 已被 model_manager 拦截为"未声明可调思考能力"），
    而是 effort 能力，用户要能为其保存 reasoning_effort；历史遗留的 "on" 已不在
    档位表里，必须被拒绝。
    """
    data = _provider_config()
    model = data["providers"][0]["models"][0]
    model["name"] = "brand-new-model"
    model["litellm_model"] = "openai/brand-new-model"
    cfg = _use_tmp_config(tmp_path, monkeypatch, data)
    model_manager.load_config()

    for choice in ("off", "low", "high", "max"):
        assert model_manager.set_model_reasoning_choice("m1", choice)[0]
        on_disk = json.loads(cfg.read_text(encoding="utf-8"))["providers"][0][
            "models"
        ][0]
        assert on_disk["reasoning_choice"] == choice
        assert model_manager.get_model_reasoning_choice("m1") == choice

    assert model_manager.set_model_reasoning_choice("m1", "default")[0]
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))["providers"][0]["models"][0]
    assert "reasoning_choice" not in on_disk

    # 历史遗留的 "on" 不再合法（兜底档位表已无 on），必须被拒绝且不写盘
    before = cfg.read_bytes()
    ok, message = model_manager.set_model_reasoning_choice("m1", "on")
    assert not ok
    assert "on" in message
    assert cfg.read_bytes() == before


def test_explicitly_hidden_model_rejects_reasoning_choice(tmp_path, monkeypatch):
    """显式登记 hidden 的模型依旧被拦截，不可写入思考选择。"""
    data = _provider_config()
    model = data["providers"][0]["models"][0]
    model["name"] = "sensenova-6.7-flash-lite"
    model["litellm_model"] = "openai/sensenova-6.7-flash-lite"
    cfg = _use_tmp_config(tmp_path, monkeypatch, data)
    model_manager.load_config()
    before = cfg.read_bytes()

    ok, message = model_manager.set_model_reasoning_choice("m1", "off")

    assert not ok
    assert "未声明可调思考能力" in message
    assert cfg.read_bytes() == before


def test_reasoning_choice_rejects_invalid_or_missing_model(tmp_path, monkeypatch):
    """模型不支持的档位与不存在的 model_id 都不能落盘。"""
    data = _provider_config()
    model = data["providers"][0]["models"][0]
    model["name"] = "qwen3.8-max"
    model["litellm_model"] = "openai/qwen3.8-max"
    cfg = _use_tmp_config(tmp_path, monkeypatch, data)
    model_manager.load_config()
    before = cfg.read_bytes()

    ok, message = model_manager.set_model_reasoning_choice("m1", "max")
    assert not ok
    assert "max" in message
    assert not model_manager.set_model_reasoning_choice("missing", "low")[0]
    assert cfg.read_bytes() == before


def test_provider_protocol_must_be_registered(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    try:
        model_manager.add_provider("Bad", "https://bad.example", "key", "made-up")
    except ValueError as exc:
        assert "made-up" in str(exc)
    else:
        raise AssertionError("未注册协议必须立即失败")


def test_add_preset_provider_uses_shared_catalog_metadata(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, {"providers": [], "active_model_id": None})

    pid = model_manager.add_preset_provider("kimi-coding-openai", "sk-deepseek")

    data = model_manager.load_config()
    provider = next(item for item in data["providers"] if item["id"] == pid)
    assert provider["preset_id"] == "kimi-coding-openai"
    assert provider["name"] == "月之暗面kimi-for-coding（OpenAI格式）"
    assert provider["protocol"] == "openai"
    assert provider["api_key"] == "sk-deepseek"
    assert "sk-deepseek" not in cfg.read_text(encoding="utf-8")
    # 预置厂商只预填厂商信息，不携带模型（模型由用户经「获取模型列表」添加）
    assert provider["models"] == []



def _preset_provider_config():
    return {
        "active_model_id": "m1",
        "providers": [
            {
                "id": "p1",
                "name": "假预置厂商",
                "base_url": "https://fake.example.com",
                "api_key": "sk-test-key-123",
                "protocol": "openai",
                "preset_id": "fake-preset",
                "models": [
                    {"id": "m1", "name": "model-a", "litellm_model": "openai/model-a", "context_window": 128000},
                    {"id": "m2", "name": "custom-x", "litellm_model": "openai/custom-x", "context_window": 64000},
                ],
            }
        ],
    }


def test_update_provider_rewrites_model_route_and_preserves_blank_key(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    model_manager.update_provider(
        "p1",
        name="新厂商",
        base_url="https://responses.example.com",
        api_key="",
        protocol="responses",
    )

    provider = model_manager.load_config()["providers"][0]
    assert provider["name"] == "新厂商"
    assert provider["api_key"] == "sk-test-key-123"
    assert provider["protocol"] == "responses"
    assert provider["models"][0]["litellm_model"] == "openai/gpt-4o"


def test_delete_last_model_preserves_provider(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    model_manager.delete_model("m1")

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert [provider["id"] for provider in data["providers"]] == ["p1"]
    assert data["providers"][0]["models"] == []
    assert data["active_model_id"] is None


def test_delete_provider_removes_models_and_active_selection(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    model_manager.delete_provider("p1")

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["providers"] == []
    assert data["active_model_id"] is None


def test_update_provider_rejects_blank_name(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    try:
        model_manager.update_provider(
            "p1", name="", base_url="https://api.example.com", api_key="", protocol="openai"
        )
    except ValueError as exc:
        assert "名称" in str(exc)
    else:
        raise AssertionError("空厂商名称必须立即失败")


def test_update_model_rejects_invalid_public_fields(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    for name, context_window in (("", 128000), ("model", 0)):
        try:
            model_manager.update_model(
                "p1", "m1", name=name, context_window=context_window, supports_vision=False
            )
        except ValueError:
            pass
        else:
            raise AssertionError("无效模型公开字段必须立即失败")


def test_add_model_persists_explicit_computer_capability(tmp_path, monkeypatch):
    """add_model_to_provider 显式保存 supports_function_calling 与 computer_protocol。"""
    _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    mid = model_manager.add_model_to_provider(
        "p1", "cap-model", 128000,
        supports_vision=True, supports_function_calling=True, computer_protocol="generic",
    )

    model = next(
        m for m in model_manager.load_config()["providers"][0]["models"] if m["id"] == mid
    )
    assert model["supports_function_calling"] is True
    assert model["computer_protocol"] == "generic"


@pytest.mark.parametrize("kwargs", [
    {"supports_function_calling": 1},
    {"supports_function_calling": "yes"},
    {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "auto"},
    {"supports_vision": True, "supports_function_calling": True, "computer_protocol": 1},
    {"supports_vision": True, "supports_function_calling": False, "computer_protocol": "generic"},
    {"supports_function_calling": True, "computer_protocol": "generic"},
], ids=[
    "fc-int", "fc-str", "proto-unknown", "proto-non-str",
    "generic-no-fc", "generic-missing-vision",
])
def test_add_model_rejects_invalid_computer_capability(tmp_path, monkeypatch, kwargs):
    """非严格 bool、集合外协议值、缺能力的 generic 组合保存即拒绝且不落盘。
    2026-09-05 修订：generic 缺 supports_function_calling 不再拒绝
    （未声明视为支持；显式 False 才拒绝——见 generic-no-fc）。"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())
    before = cfg.read_bytes()

    with pytest.raises(ValueError):
        model_manager.add_model_to_provider("p1", "bad-model", 128000, **kwargs)

    assert cfg.read_bytes() == before
    models = model_manager.load_config()["providers"][0]["models"]
    assert all(m["name"] != "bad-model" for m in models)


def test_add_model_generic_accepts_vision_with_undeclared_function_calling(tmp_path, monkeypatch):
    """2026-09-05 修订：视觉声明 + 工具调用未声明的 generic 组合可保存。"""
    _use_tmp_config(tmp_path, monkeypatch, _provider_config())
    model_manager.add_model_to_provider(
        "p1", "vision-undeclared-fc", 128000,
        supports_vision=True, computer_protocol="generic",
    )
    model = next(
        m for m in model_manager.load_config()["providers"][0]["models"]
        if m["name"] == "vision-undeclared-fc"
    )
    assert model["computer_protocol"] == "generic"


def test_update_model_preserves_computer_capability_when_not_passed(tmp_path, monkeypatch):
    """update_model 不传新字段时保留既有值，传入时严格校验后覆盖。"""
    _use_tmp_config(tmp_path, monkeypatch, _provider_config())
    model_manager.update_model(
        "p1", "m1", name="gpt-4o", context_window=128000, supports_vision=True,
        supports_function_calling=True, computer_protocol="generic",
    )
    model = model_manager.load_config()["providers"][0]["models"][0]
    assert model["supports_function_calling"] is True
    assert model["computer_protocol"] == "generic"

    # 不传新字段的普通编辑不得丢失能力元数据
    model_manager.update_model(
        "p1", "m1", name="gpt-4o", context_window=64000, supports_vision=True,
    )
    model = model_manager.load_config()["providers"][0]["models"][0]
    assert model["context_window"] == 64000
    assert model["supports_function_calling"] is True
    assert model["computer_protocol"] == "generic"


def test_update_model_rejects_half_valid_generic_combination(tmp_path, monkeypatch):
    """update_model 把 generic 降级为缺能力组合时保存即拒绝。"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())
    model_manager.update_model(
        "p1", "m1", name="gpt-4o", context_window=128000, supports_vision=True,
        supports_function_calling=True, computer_protocol="generic",
    )
    before = cfg.read_bytes()

    with pytest.raises(ValueError):
        model_manager.update_model(
            "p1", "m1", name="gpt-4o", context_window=128000, supports_vision=True,
            supports_function_calling=False,
        )

    assert cfg.read_bytes() == before


def test_active_model_export_includes_computer_capability(tmp_path, monkeypatch):
    """get_active_model/get_secondary_model 导出新字段；旧配置缺字段导出为 None。"""
    data = _provider_config()
    model = data["providers"][0]["models"][0]
    model["supports_vision"] = True
    model["supports_function_calling"] = True
    model["computer_protocol"] = "generic"
    data["secondary_model_id"] = "m1"
    _use_tmp_config(tmp_path, monkeypatch, data)

    active = model_manager.get_active_model()
    assert active["supports_function_calling"] is True
    assert active["computer_protocol"] == "generic"
    secondary = model_manager.get_secondary_model()
    assert secondary["supports_function_calling"] is True
    assert secondary["computer_protocol"] == "generic"

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    _use_tmp_config(legacy_dir, monkeypatch, _provider_config())
    legacy = model_manager.get_active_model()
    assert legacy["supports_function_calling"] is None
    assert legacy["computer_protocol"] is None
