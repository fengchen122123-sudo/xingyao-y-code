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
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    return cfg


def test_load_valid_config(tmp_path, monkeypatch):
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
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    model_manager._write_config(model_manager.load_config())
    assert Path(str(cfg) + ".bak").exists()

    cfg.write_text('{"active_model_id": "m1", "provi', encoding="utf-8")

    data = model_manager.load_config()

    assert data["active_model_id"] == "m1"
    provider = data["providers"][0]
    assert provider["api_key"] == "sk-test-key-123"
    assert json.loads(cfg.read_text(encoding="utf-8"))["active_model_id"] == "m1"


def test_update_config_mutator_runs_and_persists(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"a": 1}), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))

    result = config.update_config(lambda data: data.__setitem__("b", 2))

    assert result == {"a": 1, "b": 2}
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk == {"a": 1, "b": 2}


def test_update_config_concurrent_increments_lose_nothing(tmp_path, monkeypatch):
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
    data = _provider_config()
    data["theme"] = "dark"
    cfg = _use_tmp_config(tmp_path, monkeypatch, data)

    model_manager.set_active_model("m1")

    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk["active_model_id"] == "m1"
    assert on_disk["theme"] == "dark"


def test_add_model_with_prefixed_name_not_double_prefixed(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    mid = model_manager.add_model_to_provider("p1", "openai/gpt-4o", 128000)

    data = model_manager.load_config()
    added = [m for m in data["providers"][0]["models"] if m["id"] == mid][0]
    assert added["litellm_model"] == "openai/gpt-4o"


def test_add_model_without_prefix_gets_protocol_prefix(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    mid = model_manager.add_model_to_provider("p1", "gpt-4o-mini", 128000)

    data = model_manager.load_config()
    added = [m for m in data["providers"][0]["models"] if m["id"] == mid][0]
    assert added["litellm_model"] == "openai/gpt-4o-mini"


def test_update_provider_and_model_prefixed_name_not_double_prefixed(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())

    model_manager.update_provider_and_model(
        "p1", "m1", "https://api.example.com", "sk-k", "anthropic/claude", 200000,
    )

    data = json.loads(cfg.read_text(encoding="utf-8"))
    model = data["providers"][0]["models"][0]
    assert model["litellm_model"] == "anthropic/claude"
    assert model["context_window"] == 200000


def test_reasoning_effort_persists_with_thinking_enabled(tmp_path, monkeypatch):
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

    before = cfg.read_bytes()
    ok, message = model_manager.set_model_reasoning_choice("m1", "on")
    assert not ok
    assert "on" in message
    assert cfg.read_bytes() == before


def test_explicitly_hidden_model_rejects_reasoning_choice(tmp_path, monkeypatch):
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
    cfg = _use_tmp_config(tmp_path, monkeypatch, _provider_config())
    before = cfg.read_bytes()

    with pytest.raises(ValueError):
        model_manager.add_model_to_provider("p1", "bad-model", 128000, **kwargs)

    assert cfg.read_bytes() == before
    models = model_manager.load_config()["providers"][0]["models"]
    assert all(m["name"] != "bad-model" for m in models)


def test_add_model_generic_accepts_vision_with_undeclared_function_calling(tmp_path, monkeypatch):
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
    _use_tmp_config(tmp_path, monkeypatch, _provider_config())
    model_manager.update_model(
        "p1", "m1", name="gpt-4o", context_window=128000, supports_vision=True,
        supports_function_calling=True, computer_protocol="generic",
    )
    model = model_manager.load_config()["providers"][0]["models"][0]
    assert model["supports_function_calling"] is True
    assert model["computer_protocol"] == "generic"

    model_manager.update_model(
        "p1", "m1", name="gpt-4o", context_window=64000, supports_vision=True,
    )
    model = model_manager.load_config()["providers"][0]["models"][0]
    assert model["context_window"] == 64000
    assert model["supports_function_calling"] is True
    assert model["computer_protocol"] == "generic"


def test_update_model_rejects_half_valid_generic_combination(tmp_path, monkeypatch):
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
