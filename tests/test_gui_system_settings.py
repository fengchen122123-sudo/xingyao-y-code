import json

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
