import json
import threading
from types import SimpleNamespace

import config
import core.model_manager as model_manager
import core.llm as llm
from gui_bridge import ApiBridge


def _bare_bridge():
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._sync_active_model_context = lambda: None
    # 绕过 __init__ 时补齐会被 get_initial_state 后台线程读取的真实默认值。
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._lan_server = None
    bridge._lan_lock = threading.Lock()
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


def test_model_settings_active_model_follows_session_binding(tmp_path, monkeypatch):
    """设置快照的 active_model_id 必须是会话生效模型，而非陈旧的全局默认。

    回归：GUI 按会话绑定模型、不更新全局 active_model_id；当全局值属于
    另一厂商的同名模型时，设置快照若直接返回全局值，前端会把界面选中态
    打回错误厂商（表现为"选了 A 厂商的模型自动跳到 B 厂商"）。
    """
    _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    bridge = _bare_bridge()
    bridge._current_session = "sess-kimi"
    monkeypatch.setattr(
        "gui_bridge.get_active_model",
        lambda session=None: {"id": "m-session", "name": "session-model"},
    )

    snapshot = bridge.get_model_settings()

    assert snapshot["active_model_id"] == "m-session"


def test_model_settings_active_model_falls_back_to_global(tmp_path, monkeypatch):
    """会话尚未绑定任何模型时，active_model_id 回退到全局默认值。"""
    _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    bridge = _bare_bridge()
    bridge._current_session = ""
    monkeypatch.setattr("gui_bridge.get_active_model", lambda session=None: None)

    snapshot = bridge.get_model_settings()

    assert snapshot["active_model_id"] == "m1"


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


def test_get_initial_state_includes_secondary_model_fields(tmp_path, monkeypatch):
    """启动快照必须携带副模型配置，否则设置页会显示"无/全勾选"假象。"""
    data = _configured_data()
    data["secondary_model_id"] = "m1"
    data["secondary_model_roles"] = {"vision": True, "compression": False, "subagent": False}
    _use_tmp_config(tmp_path, monkeypatch, data)

    bridge = _bare_bridge()
    bridge._current_session = ""
    bridge._session_workdir = lambda session: "GLOBAL"
    bridge._recalc_token_usage = lambda session: None
    bridge._current_permission_mode = "confirm"
    bridge.get_sessions = lambda: []
    bridge._build_skill_list = lambda: []
    bridge._build_mcp_list = lambda: []
    bridge.get_plugins = lambda: []
    bridge._load_session_messages = lambda session: []
    bridge.get_token_usage = lambda: {}
    bridge._ensure_lan_server = lambda: None

    state = bridge.get_initial_state()

    assert state["secondary_model_id"] == "m1"
    assert state["secondary_model_roles"] == {
        "vision": True, "compression": False, "subagent": False,
    }
    assert state["system_settings"]["command_shell"] == "auto"
    assert state["system_settings"]["command_shell_available"] is True
