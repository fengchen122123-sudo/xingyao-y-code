import json
from types import SimpleNamespace

import config
import core.model_manager as model_manager
import core.llm as llm
from gui_bridge import ApiBridge


def _bare_bridge():
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._sync_active_model_context = lambda: None
    return bridge


def _use_tmp_config(tmp_path, monkeypatch, data):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(model_manager, "_CONFIG_FILE", str(cfg))
    return cfg


def _configured_data():
    return {
        "active_model_id": "m1",
        "providers": [
            {
                "id": "p1",
                "name": "测试厂商",
                "base_url": "https://api.example.com",
                "api_key": "sk-secret-value",
                "protocol": "responses",
                "models": [
                    {
                        "id": "m1",
                        "name": "model-one",
                        "litellm_model": "openai/model-one",
                        "context_window": 200000,
                        "supports_vision": True,
                    }
                ],
            }
        ],
    }


def test_model_settings_snapshot_is_complete_without_secret(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, _configured_data())

    snapshot = _bare_bridge().get_model_settings()

    assert [item["id"] for item in snapshot["protocols"]] == [
        "anthropic", "openai", "responses"
    ]
    assert any(item["id"] == "deepseek" for item in snapshot["presets"])
    provider = snapshot["providers"][0]
    assert provider["has_api_key"] is True
    assert "api_key" not in provider
    assert provider["models"][0]["context_window"] == 200000
    assert provider["models"][0]["supports_vision"] is True
    assert "sk-secret-value" not in json.dumps(snapshot, ensure_ascii=False)


def test_add_preset_provider_returns_refreshed_snapshot(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {"active_model_id": None, "providers": []})

    result = _bare_bridge().add_preset_provider("deepseek", "sk-new")

    assert result["ok"] is True
    deepseek = next(
        provider for provider in result["settings"]["providers"]
        if provider.get("preset_id") == "deepseek"
    )
    assert deepseek["protocol"] == "responses"
    assert deepseek["has_api_key"] is True
    assert deepseek["models"][0]["supports_vision"] is False


def test_add_and_update_model_persist_multimodal_flag(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    bridge = _bare_bridge()

    added = bridge.add_model("p1", "model-two", 300000, True)
    model_id = added["model_id"]
    added_model = next(
        model for provider in added["settings"]["providers"]
        for model in provider["models"] if model["id"] == model_id
    )
    assert added_model["supports_vision"] is True

    updated = bridge.update_model("p1", model_id, "model-two-v2", 400000, False)
    updated_model = next(
        model for provider in updated["settings"]["providers"]
        for model in provider["models"] if model["id"] == model_id
    )
    assert updated_model["name"] == "model-two-v2"
    assert updated_model["context_window"] == 400000
    assert updated_model["supports_vision"] is False


def test_responses_model_test_uses_responses_protocol(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    called = []

    def fake_responses(**kwargs):
        called.append(kwargs)
        response = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2)
        )
        return [SimpleNamespace(type="response.completed", response=response)]

    monkeypatch.setattr(llm, "_litellm_responses", fake_responses)
    monkeypatch.setattr(
        llm,
        "completion",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("不得调用 completion")),
    )
    monkeypatch.setattr(
        llm,
        "_get_litellm",
        lambda: SimpleNamespace(
            completion=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("不得调用 completion")
            )
        ),
    )

    result = _bare_bridge().test_model("m1")

    assert result == {"ok": True, "msg": "连接成功"}
    assert called[0]["input"] == [{"role": "user", "content": "ping"}]
