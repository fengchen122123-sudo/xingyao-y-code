"""tests/test_managed_policy.py — T6 受管策略层（借鉴 Codex requirements.toml）。

验收面：
- 受管 > 用户优先级成立（读取链降档）；
- 请求超限被降档且留警告；
- disabled_tools 工具在注册表不可用；
- 受管文件缺失时零行为变化。

隔离策略（沿用 tests/test_config_toml_overlay.py 的既有手法）：
- ``YCODE_MANAGED_FILE`` 指向 pytest tmp_path 下的 managed.json；
- ``config._CONFIG_FILE`` / ``config._TOML_CONFIG_FILE`` 指向 tmp_path；
- 每个用例前后清空 config 的 mtime 缓存与 managed_policy 的进程内状态；
- 不写 providers 数组，避开 DPAPI 加解密路径。
"""

import json
import os
from pathlib import Path

import pytest

import config
from core import app_context, managed_policy
from core.permission_policy import (
    PermissionAction,
    PermissionFacts,
    PermissionRisk,
    decide_permission,
)
from core.tool_registry import build_default_registry


def _write_managed(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_config(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_config(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def managed_env(tmp_path, monkeypatch):
    """隔离 config 与受管文件路径；返回 (managed.json 路径, config.json 路径)。"""
    managed_file = tmp_path / "managed.json"
    cfg_path = tmp_path / "config.json"
    toml_path = tmp_path / "config.toml"
    monkeypatch.setenv("YCODE_MANAGED_FILE", str(managed_file))
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(toml_path))
    config._CONFIG_CACHE.clear()
    config._TOML_CACHE.clear()
    managed_policy._reset_state_for_tests()
    yield managed_file, cfg_path
    config._CONFIG_CACHE.clear()
    config._TOML_CACHE.clear()
    managed_policy._reset_state_for_tests()


@pytest.fixture
def captured_warnings(monkeypatch):
    """捕获 managed_policy 的运行告警文本（不落真实日志）。"""
    captured = []
    monkeypatch.setattr(
        managed_policy,
        "_log_warning",
        lambda msg, *args: captured.append(msg % args if args else msg),
    )
    return captured


# ---------------------------------------------------------------------------
# 受管文件定位与读取
# ---------------------------------------------------------------------------

def test_env_override_controls_managed_path(managed_env):
    managed_file, _ = managed_env
    assert managed_policy.managed_file_path() == str(managed_file)


def test_default_managed_path_by_platform(monkeypatch):
    monkeypatch.delenv("YCODE_MANAGED_FILE", raising=False)
    if os.name == "nt":
        program_data = os.environ.get("ProgramData") or r"C:\ProgramData"
        assert managed_policy.managed_file_path() == os.path.join(
            program_data, "Y-code", "managed.json"
        )
    else:
        assert managed_policy.managed_file_path() == "/etc/ycode/managed.json"


def test_missing_managed_file_is_noop(managed_env, captured_warnings):
    """验收④：受管文件缺失时读取链与现状完全一致，且不产生任何告警。"""
    managed_file, cfg_path = managed_env
    assert not managed_file.exists()
    _write_config(cfg_path, {"permission_mode": "full", "theme": "cyan"})

    assert managed_policy.load_managed_policy() == {}
    assert config._load() == {"permission_mode": "full", "theme": "cyan"}
    assert captured_warnings == []


def test_invalid_managed_json_returns_empty_with_warning(managed_env, captured_warnings):
    managed_file, _ = managed_env
    managed_file.write_text("{broken", encoding="utf-8")

    assert managed_policy.load_managed_policy() == {}
    assert any("managed.json" in warning for warning in captured_warnings)


def test_non_object_managed_json_returns_empty(managed_env, captured_warnings):
    managed_file, _ = managed_env
    managed_file.write_text("[1, 2]", encoding="utf-8")

    assert managed_policy.load_managed_policy() == {}
    assert any("managed.json" in warning for warning in captured_warnings)


# ---------------------------------------------------------------------------
# apply_managed_policy：纯函数语义与降档
# ---------------------------------------------------------------------------

def test_apply_clamps_over_ceiling_mode(managed_env, captured_warnings):
    data = {"permission_mode": "full", "theme": "cyan"}
    policy = {"permission_mode_max": "accept_edits"}

    result = managed_policy.apply_managed_policy(data, policy)

    assert result["permission_mode"] == "accept_edits"
    assert result["theme"] == "cyan"
    assert data["permission_mode"] == "full"  # 不污染入参
    assert any("降档" in warning and "accept_edits" in warning for warning in captured_warnings)


def test_apply_keeps_mode_within_ceiling(managed_env, captured_warnings):
    data = {"permission_mode": "plan", "theme": "cyan"}

    result = managed_policy.apply_managed_policy(
        data, {"permission_mode_max": "accept_edits"}
    )

    assert result is data
    assert captured_warnings == []


def test_apply_without_policy_is_identity(managed_env):
    data = {"permission_mode": "full"}
    assert managed_policy.apply_managed_policy(data, {}) is data


def test_apply_invalid_ceiling_value_ignored_with_warning(managed_env, captured_warnings):
    data = {"permission_mode": "full"}

    result = managed_policy.apply_managed_policy(
        data, {"permission_mode_max": "banana"}
    )

    assert result["permission_mode"] == "full"
    assert any("permission_mode_max" in warning for warning in captured_warnings)


# ---------------------------------------------------------------------------
# config 读取链：受管 > 用户
# ---------------------------------------------------------------------------

def test_config_load_downgrades_mode_above_ceiling(managed_env, captured_warnings):
    """验收①②：用户配置 full 被受管上限压到 accept_edits，并留下降档警告。"""
    managed_file, cfg_path = managed_env
    _write_config(cfg_path, {"permission_mode": "full", "theme": "mine"})
    _write_managed(managed_file, {"permission_mode_max": "accept_edits"})

    data = config._load()

    assert data["permission_mode"] == "accept_edits"
    assert data["theme"] == "mine"
    assert any("降档" in warning for warning in captured_warnings)


def test_config_load_keeps_user_mode_within_ceiling(managed_env):
    managed_file, cfg_path = managed_env
    _write_config(cfg_path, {"permission_mode": "confirm"})
    _write_managed(managed_file, {"permission_mode_max": "accept_edits"})

    assert config._load()["permission_mode"] == "confirm"


def test_config_load_managed_ceiling_beats_toml_overlay(managed_env):
    """TOML overlay 是用户可写的读取覆盖层，同样不得绕过受管上限。"""
    managed_file, cfg_path = managed_env
    _write_config(cfg_path, {"permission_mode": "confirm"})
    Path(config._TOML_CONFIG_FILE).write_text(
        'permission_mode = "full"\n', encoding="utf-8"
    )
    _write_managed(managed_file, {"permission_mode_max": "accept_edits"})

    assert config._load()["permission_mode"] == "accept_edits"


# ---------------------------------------------------------------------------
# 保存流程：受管锁定键写入拒绝
# ---------------------------------------------------------------------------

def test_save_config_value_rejects_over_ceiling_mode(managed_env, captured_warnings):
    managed_file, cfg_path = managed_env
    _write_config(cfg_path, {"permission_mode": "confirm"})
    _write_managed(managed_file, {"permission_mode_max": "accept_edits"})

    assert config.save_config_value("permission_mode", "full") is False

    assert _read_config(cfg_path)["permission_mode"] == "confirm"
    assert any("由管理员锁定" in warning for warning in captured_warnings)


def test_save_config_value_allows_mode_within_ceiling(managed_env, captured_warnings):
    managed_file, cfg_path = managed_env
    _write_config(cfg_path, {"permission_mode": "confirm"})
    _write_managed(managed_file, {"permission_mode_max": "accept_edits"})

    assert config.save_config_value("permission_mode", "plan") is True

    assert _read_config(cfg_path)["permission_mode"] == "plan"
    assert captured_warnings == []


def test_update_config_rejects_over_ceiling_mode_but_writes_other_keys(
    managed_env, captured_warnings
):
    managed_file, cfg_path = managed_env
    _write_config(cfg_path, {"permission_mode": "confirm", "theme": "old"})
    _write_managed(managed_file, {"permission_mode_max": "accept_edits"})

    result = config.update_config(
        lambda data: data.update({"permission_mode": "full", "theme": "new"})
    )

    assert result["theme"] == "new"
    assert result["permission_mode"] == "confirm"
    assert _read_config(cfg_path) == {"permission_mode": "confirm", "theme": "new"}
    assert any("由管理员锁定" in warning for warning in captured_warnings)


# ---------------------------------------------------------------------------
# 权限消费方：模式上限校验（降档 + 警告）
# ---------------------------------------------------------------------------

def test_get_permission_mode_downgrades_and_warns_once(managed_env, captured_warnings):
    """验收②：运行时请求 full 也被降档；同一降档只留一条警告（防刷屏）。"""
    managed_file, _ = managed_env
    _write_managed(managed_file, {"permission_mode_max": "accept_edits"})

    app_context.set_permission_mode("full")

    assert app_context.get_permission_mode() == "accept_edits"
    assert app_context.get_permission_mode() == "accept_edits"
    downgrades = [warning for warning in captured_warnings if "降档" in warning]
    assert len(downgrades) == 1


def test_downgraded_mode_changes_permission_decision(managed_env):
    """降档后的模式进入权限决策：full 对敏感变更自动放行，降为 accept_edits 后必须询问。"""
    managed_file, _ = managed_env
    _write_managed(managed_file, {"permission_mode_max": "accept_edits"})

    app_context.set_permission_mode("full")
    decision = decide_permission(
        PermissionFacts(
            mode=app_context.get_permission_mode(),
            risk=PermissionRisk.SENSITIVE_MUTATION,
        )
    )

    assert decision.action is PermissionAction.ASK


def test_get_permission_mode_unchanged_without_managed_file(managed_env, captured_warnings):
    """验收④：无受管文件时权限模式读取零变化。"""
    app_context.set_permission_mode("full")

    assert app_context.get_permission_mode() == "full"
    assert captured_warnings == []


# ---------------------------------------------------------------------------
# 工具注册：disabled_tools 过滤
# ---------------------------------------------------------------------------

def test_disabled_tools_removed_from_registry(managed_env):
    """验收③：禁用工具不在可用列表（schema / all / get / execute 全链路）。"""
    managed_file, _ = managed_env
    _write_managed(managed_file, {"disabled_tools": ["read_file"]})

    registry = build_default_registry()

    names = [spec.name for spec in registry.all()]
    assert "read_file" not in names
    assert "write_file" in names  # 未禁用的工具保留
    with pytest.raises(KeyError):
        registry.get("read_file")
    assert "read_file" not in [schema["function"]["name"] for schema in registry.api_schemas()]
    result = registry.execute("read_file", file_path="x.txt")
    assert result.is_error and "Unknown tool" in result.content


def test_disabled_tools_absent_keeps_registry_intact(managed_env):
    """验收④：无受管文件时工具注册表与现状一致。"""
    registry = build_default_registry()

    assert registry.get("read_file").name == "read_file"
    assert registry.get("execute_command") is not None


def test_disabled_tools_unknown_names_ignored(managed_env):
    managed_file, _ = managed_env
    _write_managed(managed_file, {"disabled_tools": ["不存在的工具", "read_file"]})

    registry = build_default_registry()

    assert "read_file" not in [spec.name for spec in registry.all()]


def test_disabled_tools_invalid_type_does_not_break_registry(managed_env, captured_warnings):
    managed_file, _ = managed_env
    _write_managed(managed_file, {"disabled_tools": "read_file"})  # 应为字符串列表

    registry = build_default_registry()

    assert registry.get("read_file") is not None
    assert any("disabled_tools" in warning for warning in captured_warnings)