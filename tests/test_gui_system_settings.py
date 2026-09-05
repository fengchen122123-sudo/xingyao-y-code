import json
import sys
from unittest.mock import Mock

import config
from core.computer.models import ComputerControlError, ComputerRunSnapshot
from gui_bridge import ApiBridge


def _bare_bridge():
    return ApiBridge.__new__(ApiBridge)


def _use_tmp_config(tmp_path, monkeypatch, data):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    return cfg


def _use_isolated_config(tmp_path, monkeypatch, data):
    """Task 12：同时隔离 JSON 与 TOML overlay，避免开发机 config.toml 污染断言。"""
    cfg = _use_tmp_config(tmp_path, monkeypatch, data)
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(tmp_path / "config.toml"))
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


# ---------------------------------------------------------------------------
# 电脑操控严格 setter（Task 12：Bridge 只调领域函数，失败不吞为 success）
# ---------------------------------------------------------------------------


class _FakeControllerService:
    """ComputerControlService 最小 fake：快照可注入活动 run，stop 只记录调用。"""

    def __init__(self, run_id: str = "", status: str = "idle", target: str = ""):
        self._snapshot = ComputerRunSnapshot(
            run_id=run_id, status=status, target=target
        )
        self.stop_calls: list[tuple[str, str]] = []

    def snapshot(self) -> ComputerRunSnapshot:
        return self._snapshot

    def stop(self, run_id: str, reason: str) -> ComputerRunSnapshot:
        self.stop_calls.append((run_id, reason))
        # 刻意不改状态：模拟“清理尚在进行”，开关快照仍必须已是 false
        return self._snapshot


def test_bridge_enable_failure_returns_unchanged_snapshot(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {})
    bridge = _bare_bridge()
    monkeypatch.setattr(
        "core.computer.settings.set_browser_control_enabled",
        Mock(side_effect=ComputerControlError("computer_hotkey_unavailable", "热键不可用")),
    )

    result = bridge.set_browser_control_enabled(True)

    assert result["ok"] is False
    assert result["settings"]["browser_control_enabled"] is False
    assert result["error_code"] == "computer_hotkey_unavailable"
    assert result["msg"] == "热键不可用"


def test_bridge_browser_enable_persists_after_preflight(tmp_path, monkeypatch):
    cfg = _use_isolated_config(tmp_path, monkeypatch, {})
    bridge = _bare_bridge()
    # pytest 绝不真实探测热键：preflight 以桩替换，仅验证 Bridge → 领域 setter 接线
    monkeypatch.setattr(bridge, "_browser_control_preflight", lambda: None)

    result = bridge.set_browser_control_enabled(True)

    assert result["ok"] is True
    assert result["settings"]["browser_control_enabled"] is True
    assert json.loads(cfg.read_text(encoding="utf-8"))["browser_control_enabled"] is True


def test_browser_enable_preflight_combines_selected_browser_and_hotkey(tmp_path, monkeypatch):
    """开启 preflight 组合所选浏览器可用 + 全局热键检查；热键不可用即不落盘。"""
    cfg = _use_isolated_config(tmp_path, monkeypatch, {})
    bridge = _bare_bridge()
    monkeypatch.setattr(
        "core.computer.settings.shutil.which",
        lambda name: r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
        if name == "msedge.exe"
        else None,
    )
    monkeypatch.setattr("core.computer.settings.os.path.isfile", lambda _p: False)
    monkeypatch.setattr("core.computer.win32_adapter._default_hotkey_probe", lambda: False)

    result = bridge.set_browser_control_enabled(True)

    assert result["ok"] is False
    assert result["error_code"] == "computer_hotkey_unavailable"
    assert "browser_control_enabled" not in json.loads(cfg.read_text(encoding="utf-8"))


def test_browser_enable_preflight_rejects_unavailable_selected_browser(tmp_path, monkeypatch):
    cfg = _use_isolated_config(tmp_path, monkeypatch, {})
    bridge = _bare_bridge()
    monkeypatch.setattr("core.computer.settings.shutil.which", lambda _name: None)
    monkeypatch.setattr("core.computer.settings.os.path.isfile", lambda _p: False)

    result = bridge.set_browser_control_enabled(True)

    assert result["ok"] is False
    assert result["error_code"] == "computer_browser_unavailable"
    assert "Edge" in result["msg"]
    assert "browser_control_enabled" not in json.loads(cfg.read_text(encoding="utf-8"))


def test_computer_enable_preflight_runs_windows_backend_checks(tmp_path, monkeypatch):
    """完整电脑操控 preflight 必须跑 Windows backend 检查（平台/DPI/桌面/热键）。"""
    from core.computer.models import ComputerControlError as DomainError

    cfg = _use_isolated_config(tmp_path, monkeypatch, {})
    bridge = _bare_bridge()
    constructed = []

    class _FailingAdapter:
        def __init__(self):
            constructed.append("constructed")

        def preflight(self):
            raise DomainError("computer_unsupported_os", "完整电脑操控目前仅支持 Windows")

    monkeypatch.setattr("core.computer.win32_adapter.Win32InputAdapter", _FailingAdapter)

    result = bridge.set_computer_control_enabled(True)

    assert constructed == ["constructed"]
    assert result["ok"] is False
    assert result["error_code"] == "computer_unsupported_os"
    assert "computer_control_enabled" not in json.loads(cfg.read_text(encoding="utf-8"))


def test_computer_enable_persists_after_windows_preflight(tmp_path, monkeypatch):
    cfg = _use_isolated_config(tmp_path, monkeypatch, {})
    bridge = _bare_bridge()

    class _PassingAdapter:
        def preflight(self):
            return None

    monkeypatch.setattr("core.computer.win32_adapter.Win32InputAdapter", _PassingAdapter)

    result = bridge.set_computer_control_enabled(True)

    assert result["ok"] is True
    assert result["settings"]["computer_control_enabled"] is True
    assert json.loads(cfg.read_text(encoding="utf-8"))["computer_control_enabled"] is True


def test_bridge_disable_persists_false_and_stops_browser_run(tmp_path, monkeypatch):
    """关闭成功后调用 controller.stop；即使清理尚在进行，快照开关已为 false。"""
    cfg = _use_isolated_config(tmp_path, monkeypatch, {"browser_control_enabled": True})
    bridge = _bare_bridge()
    service = _FakeControllerService(run_id="run-1", status="running", target="browser")
    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service", lambda: service
    )

    result = bridge.set_browser_control_enabled(False)

    assert result["ok"] is True
    assert result["settings"]["browser_control_enabled"] is False
    assert json.loads(cfg.read_text(encoding="utf-8"))["browser_control_enabled"] is False
    assert service.stop_calls == [("run-1", "settings_disabled")]
    # fake 的 stop 刻意不变更状态：run 仍在清理中，开关快照已是 false
    assert result["settings"]["computer_run"]["status"] == "running"


def test_bridge_disable_computer_switch_keeps_browser_run(tmp_path, monkeypatch):
    """两个开关独立（设计 8.4）：关闭完整电脑操控不打断 browser target 的 run。"""
    cfg = _use_isolated_config(tmp_path, monkeypatch, {"computer_control_enabled": True})
    bridge = _bare_bridge()
    service = _FakeControllerService(run_id="run-1", status="running", target="browser")
    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service", lambda: service
    )

    result = bridge.set_computer_control_enabled(False)

    assert result["ok"] is True
    assert result["settings"]["computer_control_enabled"] is False
    assert json.loads(cfg.read_text(encoding="utf-8"))["computer_control_enabled"] is False
    assert service.stop_calls == []


def test_bridge_switch_enable_refreshes_emergency_hotkey(tmp_path, monkeypatch):
    """开关切换成功后同步全局紧急停止热键（Task 16：用户开启时首次注册）。"""
    _use_isolated_config(tmp_path, monkeypatch, {})
    bridge = _bare_bridge()
    monkeypatch.setattr(bridge, "_browser_control_preflight", lambda: None)
    refreshes = []
    monkeypatch.setattr(
        "core.computer.runtime.refresh_computer_runtime_hotkey",
        lambda: refreshes.append(1),
    )

    result = bridge.set_browser_control_enabled(True)

    assert result["ok"] is True
    assert refreshes == [1]


def test_bridge_switch_disable_also_refreshes_hotkey(tmp_path, monkeypatch):
    """关闭开关同样触发热键同步（可能需要注销；活动 run 语义由 refresh 判定）。"""
    _use_isolated_config(tmp_path, monkeypatch, {"computer_control_enabled": True})
    bridge = _bare_bridge()
    service = _FakeControllerService(run_id="run-1", status="running", target="desktop")
    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service", lambda: service
    )
    refreshes = []
    monkeypatch.setattr(
        "core.computer.runtime.refresh_computer_runtime_hotkey",
        lambda: refreshes.append(1),
    )

    result = bridge.set_computer_control_enabled(False)

    assert result["ok"] is True
    assert refreshes == [1]


def test_bridge_switch_failure_skips_hotkey_refresh(tmp_path, monkeypatch):
    """setter 失败（配置未落盘）绝不触发热键同步。"""
    _use_isolated_config(tmp_path, monkeypatch, {})
    bridge = _bare_bridge()
    monkeypatch.setattr(
        "core.computer.settings.set_browser_control_enabled",
        Mock(side_effect=ComputerControlError("computer_hotkey_unavailable", "热键不可用")),
    )
    refreshes = []
    monkeypatch.setattr(
        "core.computer.runtime.refresh_computer_runtime_hotkey",
        lambda: refreshes.append(1),
    )

    result = bridge.set_browser_control_enabled(True)

    assert result["ok"] is False
    assert refreshes == []


def test_bridge_hotkey_refresh_failure_does_not_fail_setter(tmp_path, monkeypatch):
    """热键同步失败只留痕：配置已落盘，开关结果仍成功（能力单独降级）。"""
    _use_isolated_config(tmp_path, monkeypatch, {})
    bridge = _bare_bridge()
    monkeypatch.setattr(bridge, "_browser_control_preflight", lambda: None)
    monkeypatch.setattr(
        "core.computer.runtime.refresh_computer_runtime_hotkey",
        Mock(side_effect=RuntimeError("boom")),
    )

    result = bridge.set_browser_control_enabled(True)

    assert result["ok"] is True
    assert result["settings"]["browser_control_enabled"] is True


def test_bridge_browser_select_fails_during_active_run(tmp_path, monkeypatch):
    """浏览器选择在活动会话中不可修改（设计 8.1），失败且配置不变。"""
    cfg = _use_isolated_config(tmp_path, monkeypatch, {"computer_browser": "edge"})
    bridge = _bare_bridge()
    service = _FakeControllerService(run_id="run-1", status="running", target="browser")
    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service", lambda: service
    )

    result = bridge.set_computer_browser("chrome")

    assert result["ok"] is False
    assert result["error_code"] == "computer_session_busy"
    assert "先停止" in result["msg"]
    assert json.loads(cfg.read_text(encoding="utf-8"))["computer_browser"] == "edge"


def test_bridge_browser_select_persists_when_idle(tmp_path, monkeypatch):
    cfg = _use_isolated_config(tmp_path, monkeypatch, {"computer_browser": "edge"})
    bridge = _bare_bridge()
    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service",
        lambda: _FakeControllerService(),
    )

    result = bridge.set_computer_browser("chrome")

    assert result["ok"] is True
    assert result["settings"]["computer_browser"] == "chrome"
    assert json.loads(cfg.read_text(encoding="utf-8"))["computer_browser"] == "chrome"
