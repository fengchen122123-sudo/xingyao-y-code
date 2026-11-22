import json
from unittest.mock import Mock

import pytest

import config
from core import subagent_settings
from core import tools as tools_module
from core import tools_admin_ops
from core.subagent_settings import SubagentSettingError
from core.tools import run_subagent
from gui_bridge import ApiBridge


def _use_isolated_config(tmp_path, monkeypatch, data=None):
    cfg = tmp_path / "config.json"
    payload = data if data else {"active_model_id": None, "providers": []}
    cfg.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(tmp_path / "config.toml"))
    return cfg


def _stored(cfg):
    return json.loads(cfg.read_text(encoding="utf-8"))


def test_enabled_by_default_when_not_configured(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"command_shell": "auto"})

    assert subagent_settings.subagent_enabled() is True


def test_non_boolean_config_value_falls_back_to_enabled(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"subagent_enabled": "yes"})

    assert subagent_settings.subagent_enabled() is True


def test_unreadable_config_falls_back_to_enabled(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text("{ 这不是 JSON", encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(tmp_path / "config.toml"))

    assert subagent_settings.subagent_enabled() is True


def test_set_disabled_persists_and_reads_back(tmp_path, monkeypatch):
    cfg = _use_isolated_config(tmp_path, monkeypatch, {})

    assert subagent_settings.set_subagent_enabled(False) is False
    assert _stored(cfg)["subagent_enabled"] is False
    assert subagent_settings.subagent_enabled() is False


def test_set_invalid_value_raises_and_keeps_config_untouched(tmp_path, monkeypatch):
    cfg = _use_isolated_config(tmp_path, monkeypatch, {"subagent_enabled": True})
    before = cfg.read_text(encoding="utf-8")

    with pytest.raises(SubagentSettingError):
        subagent_settings.set_subagent_enabled("maybe")

    assert cfg.read_text(encoding="utf-8") == before
    assert subagent_settings.subagent_enabled() is True


@pytest.mark.parametrize(
    ("base_allowed", "switch_on", "expected"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
)
def test_orchestration_allowed_truth_table(
    tmp_path, monkeypatch, base_allowed, switch_on, expected
):
    _use_isolated_config(tmp_path, monkeypatch, {"subagent_enabled": switch_on})

    assert subagent_settings.orchestration_allowed(base_allowed) is expected


def test_run_subagent_returns_failed_when_switch_disabled(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"subagent_enabled": False})
    manager_run = Mock(return_value="SENTINEL")
    monkeypatch.setattr(tools_module.subagent_manager, "run", manager_run)

    result = json.loads(
        run_subagent(
            "做一点小事",
            parent_session="session-1",
            parent_turn_id="turn-1",
            plan_item_id="item-7",
        )
    )

    assert result["status"] == "failed"
    assert "子代理已在设置中关闭" in result["error"]
    assert result["plan_item_id"] == "item-7"
    assert manager_run.call_count == 0


def test_run_subagent_not_blocked_when_switch_enabled(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"subagent_enabled": True})
    manager_run = Mock(return_value="SENTINEL-RESULT")
    monkeypatch.setattr(tools_module.subagent_manager, "run", manager_run)

    result = run_subagent(
        "做一点小事", parent_session="session-1", parent_turn_id="turn-1"
    )

    assert result == "SENTINEL-RESULT"
    assert manager_run.call_count == 1


def test_system_settings_get_exposes_boolean_subagent_enabled(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"subagent_enabled": False})

    payload = json.loads(tools_admin_ops.system_settings(action="get"))

    assert payload["ok"] is True
    assert payload["settings"]["subagent_enabled"]["value"] is False
    assert isinstance(payload["settings"]["subagent_enabled"]["value"], bool)


def test_system_settings_set_false_persists(tmp_path, monkeypatch):
    cfg = _use_isolated_config(tmp_path, monkeypatch, {"subagent_enabled": True})

    payload = json.loads(
        tools_admin_ops.system_settings(
            action="set", key="subagent_enabled", value="false"
        )
    )

    assert payload["ok"] is True
    assert payload["setting"]["value"] is False
    assert _stored(cfg)["subagent_enabled"] is False


def test_system_settings_rejects_invalid_value_without_writing(tmp_path, monkeypatch):
    cfg = _use_isolated_config(tmp_path, monkeypatch, {"subagent_enabled": True})
    before = cfg.read_text(encoding="utf-8")

    payload = json.loads(
        tools_admin_ops.system_settings(
            action="set", key="subagent_enabled", value="maybe"
        )
    )

    assert payload["ok"] is False
    assert "只接受布尔值" in payload["error"]
    assert cfg.read_text(encoding="utf-8") == before


class _CapturedRequestTools(Exception):
    pass


_FAKE_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "run_subagent",
            "description": "派子代理",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_write",
            "description": "写计划",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_update",
            "description": "更新计划",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读文件",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _capture_request_tools(monkeypatch):
    import core.llm as llm

    captured = {"tools": None}

    def _spy(model_name, api_base, request_tools):
        captured["tools"] = list(request_tools)
        raise _CapturedRequestTools()

    monkeypatch.setattr(llm, "_select_request_tools_for_route", _spy)
    monkeypatch.setattr(llm, "get_tools_schema", lambda: list(_FAKE_SCHEMAS))
    monkeypatch.setattr("core.memory_kairos.build_memory_system_section", lambda: "")
    try:
        monkeypatch.setattr(
            "core.self_prompt.build_identity_prompt_block_for_session", lambda _s: ""
        )
    except (ImportError, AttributeError):
        # 成长系统（core.self_prompt）是独立提交的新模块：本分支上可能不存在，
        # 缺席时无需打桩；llm 侧的注入逻辑本身有 try/except 兜底，不影响本用例。
        pass

    generator = llm._chat_with_model_generator(
        "fake-model",
        "sk-test-key",
        [{"role": "user", "content": "你好"}],
        api_base="http://127.0.0.1:1",
        stream=True,
        cache_scope="sub",
        consume_background_notifications=False,
    )
    with pytest.raises(_CapturedRequestTools):
        next(generator)

    assert captured["tools"] is not None, "未走到 request_tools 构造点"
    return {
        schema.get("function", {}).get("name") for schema in captured["tools"]
    }


def test_llm_request_tools_exclude_orchestration_when_disabled(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"subagent_enabled": False})

    names = _capture_request_tools(monkeypatch)

    assert "run_subagent" not in names
    assert "plan_write" not in names
    assert "plan_update" not in names
    assert "read_file" in names


def test_llm_request_tools_include_run_subagent_when_enabled(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"subagent_enabled": True})

    names = _capture_request_tools(monkeypatch)

    assert "run_subagent" in names
    assert "plan_write" in names
    assert "plan_update" in names


def _bare_bridge():
    return ApiBridge.__new__(ApiBridge)


def test_bridge_snapshot_exposes_subagent_enabled_false(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"subagent_enabled": False})

    result = _bare_bridge().get_system_settings()

    assert result["subagent_enabled"] is False


def test_bridge_snapshot_exposes_subagent_enabled_true(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"subagent_enabled": True})

    result = _bare_bridge().get_system_settings()

    assert result["subagent_enabled"] is True
    assert isinstance(result["subagent_enabled"], bool)