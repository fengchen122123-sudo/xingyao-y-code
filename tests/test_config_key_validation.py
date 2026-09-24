"""T2 配置未知键校验测试。

需求（待开发/Codex与ZCode增量改进/实施计划与验收清单.md · T2）：
① config.example.json 全键零误报；
② 未知键（含嵌套）命中，返回点分路径（如 memory_compression.retain_cout）；
③ strict_config=true 时未知键报错阻断启动（默认仅警告不阻断）；
④ config.toml overlay 中的合法键计入已知，不误报。

隔离策略（与 tests/test_config_toml_overlay.py 一致）：
- monkeypatch config._CONFIG_FILE / _TOML_CONFIG_FILE 到 pytest tmp_path；
- 每个用例前后 dict.clear() 清空 config._CONFIG_CACHE 与 _TOML_CACHE；
- 不写 providers 数组，避开 DPAPI 加解密路径。
"""

import json
from pathlib import Path

import pytest

import config

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    """隔离配置环境：返回 (config.json 路径, config.toml 路径)。"""
    cfg_path = tmp_path / "config.json"
    toml_path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(toml_path))
    config._CONFIG_CACHE.clear()
    config._TOML_CACHE.clear()
    yield cfg_path, toml_path
    config._CONFIG_CACHE.clear()
    config._TOML_CACHE.clear()


@pytest.fixture
def captured_warnings(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        config,
        "_log_warning",
        lambda message, *args: warnings.append(message % args if args else message),
    )
    return warnings


# ---------------------------------------------------------------------------
# ① 全键零误报
# ---------------------------------------------------------------------------

def test_example_config_all_keys_are_known():
    """config.example.json 的所有顶层/嵌套键都必须是已知键（验收①）。"""
    data = json.loads((_REPO_ROOT / "config.example.json").read_text(encoding="utf-8"))

    assert config.find_unknown_config_keys(data) == []


def test_builtin_default_config_all_keys_are_known():
    """config.py 模块级初始化的默认配置结构不得被误报。"""
    data = config._load()

    assert data.get("providers"), "隔离数据目录应已由 config.py 写入默认配置"
    assert config.find_unknown_config_keys(data) == []


def test_known_optional_keys_are_not_reported():
    """运行时写入/读取的可选键（含 T1 的 shell_env_policy、provider/model 扩展键）零误报。"""
    data = {
        "theme": "dark",
        "gui_theme_light": True,
        "gui_theme_id": "builtin.dark",
        "ai_output_dir": "~/.ycode/tmp",
        "browser_control_enabled": False,
        "computer_control_enabled": False,
        "computer_browser": "edge",
        "agent_turn_step_limit": 200,
        "remote_token_ttl": 86400,
        "remote_pair_token": "abc",
        "remote_pair_token_expires_at": 0,
        "remote_pair_port": 8765,
        "media_v1_migration_done": True,
        "mermaid_auto_open": False,
        "read_file_result_max_chars": 0,
        "fetch_url_allow_hosts": ["example.com"],
        "fetch_url_strict_ssrf": True,
        "expose_mcp_admin_tools": False,
        "project_hook_full_input": False,
        "skill_search_paths": ["./.agents/skills"],
        "strict_config": False,
        "model": "openai/gpt-4o",
        "api_key": "",
        "base_url": "https://api.example.com",
        "shell_env_policy": {
            "inherit": "all",
            "exclude_sensitive": True,
            "exclude": ["FOO_*"],
            "include_only": [],
            "set": {"KEY": "value"},
        },
        "providers": [
            {
                "id": "p1",
                "name": "厂商",
                "base_url": "https://api.example.com",
                "api_key": "sk-x",
                "protocol": "openai",
                "preset_id": "deepseek",
                "enable_prompt_cache": True,
                "prompt_cache": False,
                "enable_cache_control": False,
                "cache_control": False,
                "models": [
                    {
                        "id": "m1",
                        "name": "model-a",
                        "litellm_model": "openai/model-a",
                        "context_window": 128000,
                        "supports_vision": True,
                        "supports_function_calling": True,
                        "max_tokens": 8192,
                        "thinking_budget": None,
                        "enable_thinking": True,
                        "reasoning_effort": "max",
                        "reasoning_choice": "max",
                        "computer_protocol": "generic",
                        "system_prompt_extra": "x",
                        "enable_prompt_cache": True,
                        "prompt_cache": False,
                        "enable_cache_control": False,
                        "cache_control": False,
                    }
                ],
            }
        ],
    }

    assert config.find_unknown_config_keys(data) == []


def test_cache_route_config_keys_are_known():
    """Task0：三个缓存路由配置键必须登记为已知键，零误报。

    三个键分别被 core/route_overrides.py（route_request_overrides）与
    core/llm_dialects.py（cache_keepalive_routes / deepseek_cache_routes）读取。
    """
    data = {
        "route_request_overrides": [],
        "cache_keepalive_routes": [],
        "deepseek_cache_routes": [],
    }

    assert config.find_unknown_config_keys(data) == []


# ---------------------------------------------------------------------------
# ② 未知键命中（含嵌套）
# ---------------------------------------------------------------------------

def test_unknown_top_level_key_detected():
    assert config.find_unknown_config_keys({"retain_cout": 3}) == ["retain_cout"]


def test_unknown_nested_key_detected_with_dotted_path():
    """嵌套未知键返回点分路径（验收②示例：memory_compression.retain_cout）。"""
    data = {"memory_compression": {"retain_count": 8, "retain_cout": 8}}

    assert config.find_unknown_config_keys(data) == ["memory_compression.retain_cout"]


def test_unknown_provider_and_model_keys_detected():
    data = {
        "providers": [
            {
                "id": "p1",
                "name": "厂商",
                "models": [
                    {"id": "m1", "name": "model-a", "context_winodw": 128000},
                ],
            },
            {"id": "p2", "nam": "拼错的厂商名"},
        ]
    }

    unknown = config.find_unknown_config_keys(data)

    assert "providers[0].models[0].context_winodw" in unknown
    assert "providers[1].nam" in unknown


def test_historical_removed_preset_models_not_reported():
    """历史功能遗留键（dd839a0 前「预设模型删除痕迹」）不得对老配置误报。"""
    data = {
        "providers": [
            {"id": "p1", "name": "厂商", "removed_preset_models": ["minimax-m2.5"]},
        ]
    }

    assert config.find_unknown_config_keys(data) == []


def test_unknown_second_level_key_detected():
    data = {"shell_env_policy": {"inherit": "all", "exclude_sensitve": False}}

    assert config.find_unknown_config_keys(data) == [
        "shell_env_policy.exclude_sensitve"
    ]


def test_find_unknown_config_keys_tolerates_invalid_input():
    assert config.find_unknown_config_keys(None) == []
    assert config.find_unknown_config_keys([]) == []


# ---------------------------------------------------------------------------
# ③ strict_config 行为 + 默认仅警告
# ---------------------------------------------------------------------------

def test_unknown_keys_only_warn_by_default(cfg_env, captured_warnings):
    """默认（strict_config 缺省/false）：未知键通过现有告警通道提示，不阻断。"""
    cfg_path, _ = cfg_env
    cfg_path.write_text(
        json.dumps({"mermiad_auto_open": True}), encoding="utf-8"
    )

    unknown = config.audit_config_keys()

    assert unknown == ["mermiad_auto_open"]
    joined = "\n".join(captured_warnings)
    assert "mermiad_auto_open" in joined
    # difflib 提示拼写相近的正确键
    assert "mermaid_auto_open" in joined


def test_strict_config_true_raises_and_blocks_startup(cfg_env, captured_warnings):
    """strict_config=true：未知键抛 UnknownConfigKeysError（验收③），且不再写警告。"""
    cfg_path, _ = cfg_env
    cfg_path.write_text(
        json.dumps({"strict_config": True, "unknown_key": 1}), encoding="utf-8"
    )

    with pytest.raises(config.UnknownConfigKeysError) as excinfo:
        config.audit_config_keys()

    assert "unknown_key" in str(excinfo.value)
    assert captured_warnings == []


def test_strict_config_true_with_clean_config_passes(cfg_env, captured_warnings):
    cfg_path, _ = cfg_env
    cfg_path.write_text(json.dumps({"strict_config": True}), encoding="utf-8")

    assert config.audit_config_keys() == []
    assert captured_warnings == []


def test_strict_config_true_with_cache_route_keys_passes(cfg_env, captured_warnings):
    """Task0：strict_config=true 且含三个缓存路由配置键时可正常启动（零告警、不抛错）。"""
    cfg_path, _ = cfg_env
    cfg_path.write_text(
        json.dumps({
            "strict_config": True,
            "route_request_overrides": [],
            "cache_keepalive_routes": [],
            "deepseek_cache_routes": [],
        }),
        encoding="utf-8",
    )

    assert config.audit_config_keys() == []
    assert captured_warnings == []


def test_audit_config_keys_does_not_modify_config_file(cfg_env, captured_warnings):
    """自检只读：不得改写 config.json（写路径行为不变）。"""
    cfg_path, _ = cfg_env
    cfg_path.write_text(json.dumps({"unknown_key": 1}), encoding="utf-8")
    before = cfg_path.read_bytes()

    config.audit_config_keys()

    assert cfg_path.read_bytes() == before


# ---------------------------------------------------------------------------
# ④ TOML overlay 键计入已知
# ---------------------------------------------------------------------------

def test_toml_overlay_known_keys_are_not_reported(cfg_env, captured_warnings):
    """config.toml 覆盖层里出现的合法键（含嵌套）必须计入已知，不误报（验收④）。"""
    cfg_path, toml_path = cfg_env
    cfg_path.write_text(json.dumps({"active_model_id": "m1"}), encoding="utf-8")
    toml_path.write_text(
        'shell_env_policy = { inherit = "all", exclude_sensitive = true }\n'
        'agent_turn_step_limit = 300\n'
        "[secondary_model_roles]\n"
        "vision = true\n",
        encoding="utf-8",
    )

    assert config.audit_config_keys() == []
    assert captured_warnings == []


def test_toml_overlay_unknown_key_is_reported(cfg_env, captured_warnings):
    """TOML overlay 自身写了未知键同样命中（overlay 与 JSON 同一校验口径）。"""
    cfg_path, toml_path = cfg_env
    cfg_path.write_text(json.dumps({}), encoding="utf-8")
    toml_path.write_text("unknown_toml_key = 1\n", encoding="utf-8")

    assert config.audit_config_keys() == ["unknown_toml_key"]
    assert any("unknown_toml_key" in warning for warning in captured_warnings)


# ---------------------------------------------------------------------------
# Task5：保活证据门开关登记
# ---------------------------------------------------------------------------

def test_cache_keepalive_evidence_gate_key_is_known():
    """Task5：cache_keepalive_evidence_gate 登记为已知键，不出现在未知键列表。

    该键被 core/llm_dialects.py 的保活证据门读取；不登记则 strict_config
    用户会因未知键告警被阻断启动。
    """
    data = {"cache_keepalive_evidence_gate": True}

    assert config.find_unknown_config_keys(data) == []


def test_strict_config_true_with_keepalive_evidence_gate_passes(
    cfg_env, captured_warnings
):
    """Task5：strict_config=true 且含该键时 audit_config_keys() 返回 []、零告警。"""
    cfg_path, _ = cfg_env
    cfg_path.write_text(
        json.dumps({"strict_config": True, "cache_keepalive_evidence_gate": False}),
        encoding="utf-8",
    )

    assert config.audit_config_keys() == []
    assert captured_warnings == []