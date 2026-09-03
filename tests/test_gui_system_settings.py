import json
import sys

import config
from gui_bridge import ApiBridge


def _bare_bridge():
    return ApiBridge.__new__(ApiBridge)


def _use_tmp_config(tmp_path, monkeypatch, data):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    return cfg


def test_bridge_exposes_extensible_system_settings_snapshot(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "cmd"})
    cmd = r"C:\Windows\System32\cmd.exe"
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    monkeypatch.setattr(
        "core.command_shell.shutil.which",
        lambda name: {"cmd.exe": cmd, "powershell.exe": powershell}.get(name),
    )

    result = _bare_bridge().get_system_settings()

    assert result["command_shell"] == "cmd"
    assert result["command_shell_available"] is True
    assert [item["id"] for item in result["command_shell_options"]] == [
        "auto", "pwsh", "powershell", "cmd",
    ]


def test_bridge_persists_an_available_command_shell(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "auto"})
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    monkeypatch.setattr(
        "core.command_shell.shutil.which",
        lambda name: powershell if name == "powershell.exe" else None,
    )

    result = _bare_bridge().set_command_shell("powershell")

    assert result["ok"] is True
    assert result["settings"]["command_shell"] == "powershell"
    assert json.loads(cfg.read_text(encoding="utf-8"))["command_shell"] == "powershell"


def test_bridge_rejects_an_unavailable_command_shell_without_changing_config(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "cmd"})
    monkeypatch.setattr("core.command_shell.shutil.which", lambda _name: None)

    result = _bare_bridge().set_command_shell("pwsh")

    assert result["ok"] is False
    assert "未安装" in result["msg"]
    assert json.loads(cfg.read_text(encoding="utf-8"))["command_shell"] == "cmd"


def test_system_settings_snapshot_exposes_turn_step_limit(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {"agent_turn_step_limit": 240})

    result = _bare_bridge().get_system_settings()

    assert result["agent_turn_step_limit"] == 240
    assert result["agent_turn_step_limit_min"] == 10
    assert result["agent_turn_step_limit_max"] == 1000


def test_bridge_persists_valid_turn_step_limit(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, {"agent_turn_step_limit": 100})

    result = _bare_bridge().set_agent_turn_step_limit(300)

    assert result["ok"] is True
    assert result["settings"]["agent_turn_step_limit"] == 300
    assert json.loads(cfg.read_text(encoding="utf-8"))["agent_turn_step_limit"] == 300


def test_bridge_rejects_invalid_turn_step_limit_without_changing_config(
    tmp_path, monkeypatch
):
    cfg = _use_tmp_config(tmp_path, monkeypatch, {"agent_turn_step_limit": 100})

    result = _bare_bridge().set_agent_turn_step_limit(1001)

    assert result["ok"] is False
    assert "10" in result["msg"] and "1000" in result["msg"]
    assert json.loads(cfg.read_text(encoding="utf-8"))["agent_turn_step_limit"] == 100


def test_system_settings_snapshot_exposes_computer_control_settings(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {})

    result = _bare_bridge().get_system_settings()

    # 缺失键安全默认：两个开关关闭、默认浏览器 edge
    assert result["browser_control_enabled"] is False
    assert result["computer_control_enabled"] is False
    assert result["computer_browser"] == "edge"
    options = {item["browser"]: item for item in result["computer_browser_options"]}
    assert set(options) == {"edge", "chrome"}
    for item in options.values():
        assert set(item) == {"browser", "label", "available", "executable", "unavailable_reason"}
    assert "browser-profiles" in result["computer_profile_path"].replace("/", "\\")
    assert result["computer_platform_supported"] is (sys.platform == "win32")


def test_system_settings_snapshot_reflects_persisted_computer_values(tmp_path, monkeypatch):
    _use_tmp_config(
        tmp_path,
        monkeypatch,
        {"browser_control_enabled": True, "computer_browser": "chrome"},
    )

    result = _bare_bridge().get_system_settings()

    assert result["browser_control_enabled"] is True
    assert result["computer_control_enabled"] is False
    assert result["computer_browser"] == "chrome"
    assert result["computer_profile_path"].replace("/", "\\").endswith("browser-profiles\\chrome")


def test_system_settings_snapshot_exposes_computer_hotkey_state(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {})

    result = _bare_bridge().get_system_settings()

    # 无活动热键实例时保守上报不可用；键名固定 Ctrl+Alt+Esc
    assert result["computer_hotkey_available"] is False
    assert result["computer_hotkey"] == "Ctrl+Alt+Esc"


def test_system_settings_snapshot_reflects_registered_hotkey(tmp_path, monkeypatch):
    from core.computer.hotkey import EmergencyStopHotkey
    from tests.computer_fakes import FakeHotkeyApi

    _use_tmp_config(tmp_path, monkeypatch, {})
    hotkey = EmergencyStopHotkey(FakeHotkeyApi(), on_stop=lambda: None)
    hotkey.start()
    try:
        result = _bare_bridge().get_system_settings()
        assert result["computer_hotkey_available"] is True
        assert result["computer_hotkey"] == "Ctrl+Alt+Esc"
    finally:
        hotkey.close()


def test_system_settings_snapshot_exposes_computer_run(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {})

    result = _bare_bridge().get_system_settings()

    run = result["computer_run"]
    # 无活动 run 时为 idle 默认快照；字段与 ComputerRunSnapshot.to_public_dict 一致
    assert run["status"] == "idle"
    assert run["run_id"] == ""
    for key in (
        "run_id", "owner_session_id", "owner_turn_id", "status", "target",
        "browser", "protocol", "started_at", "action_count", "current_action",
        "current_surface", "preview_url", "pause_reason", "stop_reason",
        "error_code", "hotkey_available",
    ):
        assert key in run
    # 快照不得包含输入正文、截图 base64 或本地路径字段
    assert "png_path" not in run
    json.dumps(run)
