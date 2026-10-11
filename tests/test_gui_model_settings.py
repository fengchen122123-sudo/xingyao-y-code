import json
import threading
from types import SimpleNamespace

import config
import core.model_manager as model_manager
import core.llm as llm
from core.llm_policy import TOKENRHYTHM_AGENT_NAME
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
    assert any(item["id"] == "kimi-coding-openai" for item in snapshot["presets"])
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

    result = _bare_bridge().add_preset_provider("kimi-coding-openai", "sk-new")

    assert result["ok"] is True
    kimi = next(
        provider for provider in result["settings"]["providers"]
        if provider.get("preset_id") == "kimi-coding-openai"
    )
    assert kimi["protocol"] == "openai"
    assert kimi["has_api_key"] is True
    # 预置厂商只预填厂商信息，模型列表为空（用户经「获取模型列表」添加）
    assert kimi["models"] == []


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

    monkeypatch.setattr(llm, "_dispatch_responses", fake_responses)
    monkeypatch.setattr(
        llm,
        "completion",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("不得调用 completion")),
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
    bridge._load_session_messages = lambda session: ([], False, None)
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
        "computer_protocol=generic 要求声明 supports_vision，且 supports_function_calling 未被显式关闭"
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


def test_fetch_provider_models_returns_sorted_list(tmp_path, monkeypatch):
    """「获取模型列表」：正常路径返回模型 ID 列表，语音/图像类排到末尾。"""
    import httpx

    _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    captured = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {"id": "zeta-tts", "model_type": "语音合成"},
                    {"id": "Beta", "model_type": "大语言模型"},
                    {"id": "alpha", "model_type": "大语言模型"},
                ]
            }

    def _fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(httpx, "get", _fake_get)

    result = _bare_bridge().fetch_provider_models("p1")

    assert result["ok"] is True
    assert [m["id"] for m in result["models"]] == ["alpha", "Beta", "zeta-tts"]
    assert result["models"][0]["model_type"] == "大语言模型"
    assert result["count"] == 3
    # 双鉴权头兼容 OpenAI 网关（Bearer）与 Anthropic 端点（x-api-key）
    assert captured["url"] == "https://api.example.com/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-secret-value"
    assert captured["headers"]["x-api-key"] == "sk-secret-value"


def test_fetch_provider_models_reports_http_error(tmp_path, monkeypatch):
    import httpx

    _use_tmp_config(tmp_path, monkeypatch, _configured_data())

    class _FakeResponse:
        status_code = 401
        text = '{"error":"invalid key"}'

    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: _FakeResponse())

    result = _bare_bridge().fetch_provider_models("p1")

    assert result["ok"] is False
    assert "401" in result["msg"]
    assert "invalid key" in result["msg"]


def test_fetch_provider_models_unknown_provider(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, _configured_data())

    result = _bare_bridge().fetch_provider_models("no-such-provider")

    assert result["ok"] is False
    assert "未找到供应商" in result["msg"]


def test_fetch_provider_models_handles_v1_base_url(tmp_path, monkeypatch):
    """base_url 已含版本段（…/v1）时不得重复拼接（/v1/v1/models 404 教训）。"""
    import httpx

    data = _configured_data()
    data["providers"][0]["base_url"] = "https://api.example.com/v1"
    _use_tmp_config(tmp_path, monkeypatch, data)
    captured = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "m-a"}]}

    def _fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResponse()

    monkeypatch.setattr(httpx, "get", _fake_get)

    result = _bare_bridge().fetch_provider_models("p1")

    assert result["ok"] is True
    assert captured["url"] == "https://api.example.com/v1/models"


def test_fetch_provider_models_adds_tokenrhythm_agent_header(tmp_path, monkeypatch):
    """基元律动厂商：拉取模型列表的请求必须携带 X-TokenRhythm-Agent 客户端标识。"""
    import httpx

    data = _configured_data()
    data["providers"][0]["base_url"] = "https://tokenrhythm.studio/v1"
    _use_tmp_config(tmp_path, monkeypatch, data)
    captured = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "alpha"}]}

    def _fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse()

    monkeypatch.setattr(httpx, "get", _fake_get)

    result = _bare_bridge().fetch_provider_models("p1")

    assert result["ok"] is True
    assert captured["headers"]["X-TokenRhythm-Agent"] == TOKENRHYTHM_AGENT_NAME


def test_fetch_provider_models_omits_tokenrhythm_agent_header_for_other_providers(
    tmp_path, monkeypatch
):
    """非基元律动厂商：拉取模型列表不得携带该标识头。"""
    import httpx

    _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    captured = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"id": "alpha"}]}

    def _fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResponse()

    monkeypatch.setattr(httpx, "get", _fake_get)

    result = _bare_bridge().fetch_provider_models("p1")

    assert result["ok"] is True
    assert "X-TokenRhythm-Agent" not in captured["headers"]


def test_fetch_provider_models_extracts_context_and_vision(tmp_path, monkeypatch):
    """元数据提取：阶跃 max_input_tokens/enable_vision_input 形状与
    OpenRouter context_length/architecture.input_modalities 形状都要能填表；
    缺元数据的条目返回 None（前端保持表单原值）。"""
    import httpx

    _use_tmp_config(tmp_path, monkeypatch, _configured_data())

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {
                        "id": "step-x",
                        "model_type": "大语言模型",
                        "max_input_tokens": 1024000,
                        "enable_vision_input": True,
                    },
                    {
                        "id": "or-y",
                        "context_length": 131072,
                        "architecture": {"input_modalities": ["text", "image"]},
                    },
                    {"id": "plain-z"},
                ]
            }

    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: _FakeResponse())

    result = _bare_bridge().fetch_provider_models("p1")

    assert result["ok"] is True
    by_id = {m["id"]: m for m in result["models"]}
    assert by_id["step-x"]["context_window"] == 1024000
    assert by_id["step-x"]["supports_vision"] is True
    assert by_id["or-y"]["context_window"] == 131072
    assert by_id["or-y"]["supports_vision"] is True
    assert by_id["plain-z"]["context_window"] is None
    assert by_id["plain-z"]["supports_vision"] is None


def test_model_max_tokens_roundtrip(tmp_path, monkeypatch):
    """最大输出：快照回显、新增写入、更新改值、0 清除回内置默认。"""
    _use_tmp_config(tmp_path, monkeypatch, _configured_data())
    bridge = _bare_bridge()
    cfg = tmp_path / "config.json"

    added = bridge.add_model(
        "p1", "model-cap", 200000, True, True, "", 65536
    )
    assert added["ok"] is True
    stored = next(
        model
        for provider in json.loads(cfg.read_text(encoding="utf-8"))["providers"]
        for model in provider["models"]
        if model["id"] == added["model_id"]
    )
    assert stored["max_tokens"] == 65536
    assert added["settings"]["models"][0]["max_tokens"] is None  # 旧模型未设置

    # 改值
    updated = bridge.update_model(
        "p1", added["model_id"], "model-cap", 200000, True, True, "", 131072
    )
    assert updated["ok"] is True
    stored = next(
        model
        for provider in json.loads(cfg.read_text(encoding="utf-8"))["providers"]
        for model in provider["models"]
        if model["id"] == added["model_id"]
    )
    assert stored["max_tokens"] == 131072

    # 0 = 清除键，回到内置默认
    cleared = bridge.update_model(
        "p1", added["model_id"], "model-cap", 200000, True, True, "", 0
    )
    assert cleared["ok"] is True
    stored = next(
        model
        for provider in json.loads(cfg.read_text(encoding="utf-8"))["providers"]
        for model in provider["models"]
        if model["id"] == added["model_id"]
    )
    assert "max_tokens" not in stored
    snapshot_item = next(
        m for m in cleared["settings"]["models"] if m["id"] == added["model_id"]
    )
    assert snapshot_item["max_tokens"] is None

    # 非法负值拒绝
    rejected = bridge.update_model(
        "p1", added["model_id"], "model-cap", 200000, True, True, "", -5
    )
    assert rejected["ok"] is False
    assert "正整数" in rejected["msg"]


def test_fetch_provider_models_enriches_from_spec_table(tmp_path, monkeypatch):
    """厂商未提供元数据的字段由本地规格表回填（官方文档核实值）；
    厂商自带元数据永远优先。"""
    import httpx

    _use_tmp_config(tmp_path, monkeypatch, _configured_data())

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {"id": "deepseek-flash"},
                    {"id": "step-x", "max_input_tokens": 1000},
                    {"id": "unknown-model-xyz"},
                ]
            }

    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: _FakeResponse())

    result = _bare_bridge().fetch_provider_models("p1")

    by_id = {m["id"]: m for m in result["models"]}
    # 规格表回填（deepseek-flash = 1M / 384K，官方定价页核实值）
    assert by_id["deepseek-flash"]["context_window"] == 1048576
    assert by_id["deepseek-flash"]["max_tokens"] == 384000
    # 厂商自带上下文优先于规格表；step-x 不在规格表 → 输出 None
    assert by_id["step-x"]["context_window"] == 1000
    assert by_id["step-x"]["max_tokens"] is None
    # 两边都没有 → 保持 None，由前端留空走内置默认
    assert by_id["unknown-model-xyz"]["context_window"] is None
    assert by_id["unknown-model-xyz"]["max_tokens"] is None


def test_model_settings_carries_reasoning_choice_and_options(tmp_path, monkeypatch):
    """模型条目必须带上思考设置字段，供模型编辑弹窗回填「思考设置」下拉。

    - 已登记模型（deepseek-v4-pro）给出真实档位 default/off/high/max，
      并带上官方默认档位 high；
    - 未登记模型回落通用强度兜底 default/off/low/medium/high/max（无内置默认
      档位，default_choice 为空串），而不是隐藏思考设置。
    """
    data = _configured_data()
    data["providers"][0]["models"] = [
        {
            "id": "m1",
            "name": "deepseek-v4-pro",
            "litellm_model": "openai/deepseek-v4-pro",
            "context_window": 200000,
        },
        {
            "id": "m2",
            "name": "hy4-preview",
            "litellm_model": "openai/hy4-preview",
            "context_window": 128000,
        },
    ]
    _use_tmp_config(tmp_path, monkeypatch, data)
    bridge = _bare_bridge()

    def _snapshot_models():
        return {m["id"]: m for m in bridge.get_model_settings()["models"]}

    known = _snapshot_models()["m1"]
    assert known["reasoning_choice"] == "default"
    assert known["reasoning_visible"] is True
    assert known["reasoning_kind"] == "effort"
    assert "default" in [opt["value"] for opt in known["reasoning_options"]]
    assert known["reasoning_disabled_reason"] == ""
    assert [opt["value"] for opt in known["reasoning_options"]] == [
        "default", "off", "high", "max"
    ]
    # 已登记模型的官方默认档位随条目下发，供弹窗自动预选
    assert known["reasoning_default_choice"] == "high"

    # 未登记模型：通用强度兜底，GUI 仍显示可操作的思考设置
    unknown = _snapshot_models()["m2"]
    assert unknown["reasoning_visible"] is True
    assert [opt["value"] for opt in unknown["reasoning_options"]] == [
        "default", "off", "low", "medium", "high", "max"
    ]
    assert unknown["reasoning_default_choice"] == ""

    ok, msg = model_manager.set_model_reasoning_choice("m1", "high")
    assert ok is True, msg
    assert _snapshot_models()["m1"]["reasoning_choice"] == "high"


# ---------------- 思考设置随模型新增/编辑落库（reasoning_choice） ----------------


def _data_with_model(name):
    """构造只含一个指定名称模型的临时配置。"""
    data = _configured_data()
    data["providers"][0]["models"] = [
        {
            "id": "m1",
            "name": name,
            "litellm_model": f"openai/{name}",
            "context_window": 200000,
        }
    ]
    return data


def test_add_model_persists_reasoning_choice(tmp_path, monkeypatch):
    """add_model 传 reasoning_choice="max"：新增成功且思考档位落库。"""
    _use_tmp_config(tmp_path, monkeypatch, _data_with_model("deepseek-v4-pro"))
    bridge = _bare_bridge()

    added = bridge.add_model(
        "p1", "deepseek-v4-pro", 200000, True, True, "", None, "max"
    )

    assert added["ok"] is True, added["msg"]
    model_id = added["model_id"]
    stored = next(
        model
        for provider in json.loads(
            (tmp_path / "config.json").read_text(encoding="utf-8")
        )["providers"]
        for model in provider["models"]
        if model["id"] == model_id
    )
    assert stored["reasoning_choice"] == "max"
    assert stored["reasoning_effort"] == "max"
    assert model_manager.get_model_reasoning_choice(model_id) == "max"

    # 不传 reasoning_choice 的旧调用不得改动思考设置（向后兼容）
    plain = bridge.add_model("p1", "deepseek-v4-pro-b", 200000)
    assert plain["ok"] is True, plain["msg"]
    assert model_manager.get_model_reasoning_choice(plain["model_id"]) == "default"


def test_update_model_persists_reasoning_choice(tmp_path, monkeypatch):
    """update_model 传 reasoning_choice="high"：编辑成功且思考档位落库。"""
    _use_tmp_config(tmp_path, monkeypatch, _data_with_model("deepseek-v4-pro"))
    bridge = _bare_bridge()

    result = bridge.update_model(
        "p1", "m1", "deepseek-v4-pro", 200000, True, True, "", None, "high"
    )

    assert result["ok"] is True, result["msg"]
    stored = next(
        model
        for provider in json.loads(
            (tmp_path / "config.json").read_text(encoding="utf-8")
        )["providers"]
        for model in provider["models"]
        if model["id"] == "m1"
    )
    assert stored["reasoning_choice"] == "high"
    assert model_manager.get_model_reasoning_choice("m1") == "high"


def test_update_model_rejects_unsupported_reasoning_choice_without_failing(
    tmp_path, monkeypatch
):
    """非法档位（glm-5.3 不支持 off）：模型主体照常保存，思考设置只警告。"""
    _use_tmp_config(tmp_path, monkeypatch, _data_with_model("glm-5.3"))
    bridge = _bare_bridge()

    result = bridge.update_model(
        "p1", "m1", "glm-5.3-renamed", 256000, True, True, "", 8192, "off"
    )

    assert result["ok"] is True
    assert "思考设置未生效" in result["msg"]
    assert "off" in result["msg"]
    stored = next(
        model
        for provider in json.loads(
            (tmp_path / "config.json").read_text(encoding="utf-8")
        )["providers"]
        for model in provider["models"]
        if model["id"] == "m1"
    )
    # 模型主体已更新，未被思考设置失败回滚
    assert stored["name"] == "glm-5.3-renamed"
    assert stored["context_window"] == 256000
    assert stored["max_tokens"] == 8192
    # 思考设置未生效，仍是默认
    assert model_manager.get_model_reasoning_choice("m1") == "default"
    assert "reasoning_choice" not in stored


def test_stale_reasoning_choice_keeps_row_visible(tmp_path, monkeypatch):
    """配置里存了能力表已不支持的档位（deepseek-v4-pro 无 low）时：

    整行思考设置必须仍然可见（reasoning_visible is True），只把选择回退成
    默认档。若在这里把 visible 置 False，用户就再也无法从 UI 改回合法档位
    ——回归：此前 get_model_settings 的兜底会把整行隐藏。
    """
    data = _data_with_model("deepseek-v4-pro")
    data["providers"][0]["models"][0]["reasoning_choice"] = "low"
    _use_tmp_config(tmp_path, monkeypatch, data)

    item = next(
        model for model in _bare_bridge().get_model_settings()["models"]
        if model["id"] == "m1"
    )

    assert item["reasoning_visible"] is True
    assert item["reasoning_kind"] == "effort"
    assert item["reasoning_choice"] == "default"
    assert [opt["value"] for opt in item["reasoning_options"]] == [
        "default", "off", "high", "max"
    ]


def test_get_reasoning_snapshot_by_name():
    """按未落库的模型名预览思考能力（新增/改名时用），choice 恒为 default。"""
    bridge = _bare_bridge()

    known = bridge.get_reasoning_snapshot_by_name("deepseek-v4-pro")
    assert known["ok"] is True
    assert known["reasoning"]["kind"] == "effort"
    assert [opt["value"] for opt in known["reasoning"]["options"]] == [
        "default", "off", "high", "max"
    ]
    assert known["reasoning"]["choice"] == "default"
    assert known["reasoning"]["visible"] is True
    assert known["reasoning"]["default_choice"] == "high"

    unknown = bridge.get_reasoning_snapshot_by_name("hy4-preview")
    assert unknown["ok"] is True
    assert unknown["reasoning"]["kind"] == "effort"
    assert [opt["value"] for opt in unknown["reasoning"]["options"]] == [
        "default", "off", "low", "medium", "high", "max"
    ]
    assert unknown["reasoning"]["default_choice"] == ""
    assert unknown["reasoning"]["visible"] is True

    hidden = bridge.get_reasoning_snapshot_by_name("sensenova-6.7-flash-lite")
    assert hidden["ok"] is True
    assert hidden["reasoning"]["visible"] is False
    assert hidden["reasoning"]["kind"] == "hidden"

    empty = bridge.get_reasoning_snapshot_by_name("")
    assert empty["ok"] is False
