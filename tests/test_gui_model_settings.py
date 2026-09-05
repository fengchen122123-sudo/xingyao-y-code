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


# ---------------------------------------------------------------------------
# 模型能力字段（Task 12：supports_function_calling / computer_protocol）
# ---------------------------------------------------------------------------


def test_model_settings_snapshot_includes_capability_fields(tmp_path, monkeypatch):
    data = _configured_data()
    data["providers"][0]["models"][0]["supports_function_calling"] = True
    data["providers"][0]["models"][0]["computer_protocol"] = "generic"
    _use_tmp_config(tmp_path, monkeypatch, data)

    snapshot = _bare_bridge().get_model_settings()

    model = snapshot["models"][0]
    assert model["supports_function_calling"] is True
    assert model["computer_protocol"] == "generic"
    provider_model = snapshot["providers"][0]["models"][0]
    assert provider_model["supports_function_calling"] is True
    assert provider_model["computer_protocol"] == "generic"


def test_legacy_model_defaults_to_auto_protocol(tmp_path, monkeypatch):
    """旧模型配置（缺 supports_function_calling 与 computer_protocol 键）：
    展示层 fc 缺省 false、协议缺省 ""（自动）。由于 fc 为 false，自动判定
    不会赋予电脑操控资格（设计 9.1 的能力门槛不变，只是协议不再误导性显示
    为显式关闭）。"""
    _use_tmp_config(tmp_path, monkeypatch, _configured_data())

    snapshot = _bare_bridge().get_model_settings()

    assert snapshot["models"][0]["supports_function_calling"] is False
    assert snapshot["models"][0]["computer_protocol"] == ""


def test_add_and_update_model_persist_capability_fields(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    bridge = _bare_bridge()

    added = bridge.add_model("p1", "model-two", 300000, True, True, "generic")
    assert added["ok"] is True
    added_model = next(
        model for provider in added["settings"]["providers"]
        for model in provider["models"] if model["id"] == added["model_id"]
    )
    assert added_model["supports_function_calling"] is True
    assert added_model["computer_protocol"] == "generic"

    updated = bridge.update_model(
        "p1", added["model_id"], "model-two-v2", 400000, True, True, "generic"
    )
    assert updated["ok"] is True
    updated_model = next(
        model for provider in updated["settings"]["providers"]
        for model in provider["models"] if model["id"] == added["model_id"]
    )
    assert updated_model["computer_protocol"] == "generic"

    cleared = bridge.update_model(
        "p1", added["model_id"], "model-two-v2", 400000, True, False, "none"
    )
    assert cleared["ok"] is True
    cleared_model = next(
        model for provider in cleared["settings"]["providers"]
        for model in provider["models"] if model["id"] == added["model_id"]
    )
    assert cleared_model["supports_function_calling"] is False
    assert cleared_model["computer_protocol"] == "none"
    stored = json.loads(cfg.read_text(encoding="utf-8"))
    stored_model = next(
        model for provider in stored["providers"]
        for model in provider["models"] if model["id"] == added["model_id"]
    )
    assert stored_model["computer_protocol"] == "none"


def test_generic_protocol_requires_vision_and_function_calling(tmp_path, monkeypatch):
    """model generic 的三字段校验：generic 但视觉未开 → 后端拒绝保存且不落盘。"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    bridge = _bare_bridge()

    result = bridge.update_model("p1", "m1", "model-one", 200000, False, True, "generic")

    assert result["ok"] is False
    assert result["msg"] == (
        "computer_protocol=generic 要求同时声明 supports_vision 与 supports_function_calling"
    )
    stored = json.loads(cfg.read_text(encoding="utf-8"))
    assert "computer_protocol" not in stored["providers"][0]["models"][0]
    assert "supports_function_calling" not in stored["providers"][0]["models"][0]


def test_add_model_rejects_generic_without_required_capabilities(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    bridge = _bare_bridge()

    result = bridge.add_model("p1", "model-bad", 100000, False, True, "generic")

    assert result["ok"] is False
    assert "computer_protocol=generic" in result["msg"]
    stored_names = [
        model["name"]
        for provider in json.loads(cfg.read_text(encoding="utf-8"))["providers"]
        for model in provider["models"]
    ]
    assert "model-bad" not in stored_names


# ---------------- 缺省协议自动判定（自动激活视觉模型） ----------------


def test_add_model_default_protocol_stays_unset_for_auto(tmp_path, monkeypatch):
    """add_model 缺省/传空：不落 computer_protocol 键，由能力元数据自动判定。

    用户裁决（2026-09-05）：新添加的视觉+工具调用模型无需逐个配置协议，
    开关开启后即可电脑操控。旧实现把缺省强转为 "none"（显式关闭）导致
    新模型永不自动激活——本测试钉死修复。
    """
    cfg = _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    bridge = _bare_bridge()

    added = bridge.add_model("p1", "model-auto", 200000, True, True)
    assert added["ok"] is True
    stored_model = next(
        model
        for provider in json.loads(cfg.read_text(encoding="utf-8"))["providers"]
        for model in provider["models"]
        if model["id"] == added["model_id"]
    )
    assert "computer_protocol" not in stored_model

    # 能力解析：视觉+工具调用双勾 ⇒ 自动 generic 启用
    # （enabled_settings 是 pytest fixture 不能直接调用，此处内联构造同等快照）
    from core.computer.capability import resolve_computer_capability
    from core.computer.settings import ComputerSettingsSnapshot
    from tests.computer_fakes import openai_compatible_provider

    capability = resolve_computer_capability(
        stored_model,
        openai_compatible_provider(),
        ComputerSettingsSnapshot(
            browser_control_enabled=True,
            computer_control_enabled=True,
            computer_browser="edge",
            browser_options=(),
            profile_display_path="",
            platform_supported=True,
            generation=1,
        ),
        "local",
    )
    assert capability.enabled is True
    assert capability.protocol == "generic"


def test_add_model_empty_string_protocol_means_auto(tmp_path, monkeypatch):
    """前端「自动」选项传 ""：同样不落键（与缺省一致），不得强转为 "none"。"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    bridge = _bare_bridge()

    added = bridge.add_model("p1", "model-auto2", 200000, True, True, "")
    assert added["ok"] is True
    stored_model = next(
        model
        for provider in json.loads(cfg.read_text(encoding="utf-8"))["providers"]
        for model in provider["models"]
        if model["id"] == added["model_id"]
    )
    assert "computer_protocol" not in stored_model


def test_update_model_empty_string_protocol_clears_to_auto(tmp_path, monkeypatch):
    """update_model 传 ""：把已存的显式协议清回自动（删除键）；None 仍表示保留。"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    bridge = _bare_bridge()

    added = bridge.add_model("p1", "model-flip", 200000, True, True, "generic")
    assert added["ok"] is True

    cleared = bridge.update_model(
        "p1", added["model_id"], "model-flip", 200000, True, True, ""
    )
    assert cleared["ok"] is True
    stored_model = next(
        model
        for provider in json.loads(cfg.read_text(encoding="utf-8"))["providers"]
        for model in provider["models"]
        if model["id"] == added["model_id"]
    )
    assert "computer_protocol" not in stored_model

    # None = 保留既有值（不触碰）
    kept = bridge.update_model(
        "p1", added["model_id"], "model-flip", 200000, True, True, None
    )
    assert kept["ok"] is True
    stored_model = next(
        model
        for provider in json.loads(cfg.read_text(encoding="utf-8"))["providers"]
        for model in provider["models"]
        if model["id"] == added["model_id"]
    )
    assert "computer_protocol" not in stored_model


def test_all_models_reports_unset_protocol_as_empty_string(tmp_path, monkeypatch):
    """展示层（_all_models）：缺键模型的 computer_protocol 返回 ""（自动），
    前端编辑表单据此回填「自动」选项，而非误导性的 "none"。"""
    _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    bridge = _bare_bridge()

    models = bridge.get_model_settings()["models"]
    model_one = next(model for model in models if model["name"] == "model-one")
    assert model_one["computer_protocol"] == ""
