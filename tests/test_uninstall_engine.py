import os
import winreg

import pytest

import ycode_uninstall
from ycode_uninstall import detect_scope_for_install, run_uninstall


def _fake_install_dir(tmp_path):
    """构造一个最小化的已安装目录结构。"""
    install_dir = tmp_path / "installed"
    (install_dir / "_internal" / "frontend").mkdir(parents=True)
    (install_dir / "cli").mkdir(parents=True)
    (install_dir / "tui").mkdir(parents=True)
    (install_dir / "Y-code.exe").write_text("gui", encoding="utf-8")
    (install_dir / "ycode.exe").write_text("cli", encoding="utf-8")
    (install_dir / "ycode_uninstall.exe").write_text("uninst", encoding="utf-8")
    (install_dir / "_internal" / "version.txt").write_text("9.9.9", encoding="utf-8")
    return install_dir


@pytest.fixture()
def patched_env(tmp_path, monkeypatch):
    """隔离注册表 / 桌面 / 用户目录等真实系统副作用。"""
    calls = {"user_path": [], "system_path": [], "shortcut": [], "registry": []}
    monkeypatch.setattr(
        ycode_uninstall, "remove_from_user_path",
        lambda d, log=print: calls["user_path"].append(d))
    monkeypatch.setattr(
        ycode_uninstall, "remove_from_system_path",
        lambda d, log=print: calls["system_path"].append(d))
    monkeypatch.setattr(
        ycode_uninstall, "remove_shortcut",
        lambda name, dirs, log=print: calls["shortcut"].append((name, set(dirs))))
    monkeypatch.setattr(
        ycode_uninstall, "remove_uninstall_registry_key",
        lambda name, hive=winreg.HKEY_CURRENT_USER, log=print:
            calls["registry"].append((name, hive)))
    monkeypatch.setattr(
        ycode_uninstall, "get_desktop_dir", lambda: str(tmp_path / "Desktop"))
    # conftest 全局设置了 YCODE_DATA_DIR 隔离目录；卸载引擎按 config.py 逻辑
    # 优先读它（修复 #2），测试需先移除，让 expanduser mock 下的 ~/.ycode 生效
    monkeypatch.delenv("YCODE_DATA_DIR", raising=False)
    monkeypatch.setattr(
        ycode_uninstall, "get_common_desktop_dir", lambda: str(tmp_path / "PublicDesktop"))
    monkeypatch.setattr(
        ycode_uninstall.os.path, "expanduser", lambda p: str(tmp_path / "home"))
    return calls


def test_user_scope_uninstall_removes_everything(tmp_path, patched_env):
    install_dir = _fake_install_dir(tmp_path)
    (tmp_path / "home" / ".ycode").mkdir(parents=True)
    progress_log = []

    res = run_uninstall(str(install_dir), scope="user", delete_config=True,
                        log=lambda m: None, progress=progress_log.append)

    assert res["deleted_config"] is True
    # 程序文件被清理（卸载程序自身保留给自销毁流程）
    assert not (install_dir / "Y-code.exe").exists()
    assert not (install_dir / "ycode.exe").exists()
    assert not (install_dir / "_internal").exists()
    assert not (install_dir / "cli").exists()
    assert not (install_dir / "tui").exists()  # 旧版 CLI 变体残留目录一并清理
    assert (install_dir / "ycode_uninstall.exe").exists()
    # 用户配置被删除
    assert not (tmp_path / "home" / ".ycode").exists()
    # user 范围：只清用户 PATH，不碰系统 PATH；注册表只清 HKCU
    assert patched_env["user_path"] == [os.path.normpath(str(install_dir))]
    assert patched_env["system_path"] == []
    assert all(hive == winreg.HKEY_CURRENT_USER for _, hive in patched_env["registry"])
    # 注册表：现役两个入口 + 旧版 TUI 命名兼容清理
    assert {name for name, _ in patched_env["registry"]} == {
        "Y-code", "Y-code-CLI", "Y-code-TUI"}
    # 快捷方式：全部命名都尝试，目录不含公共桌面
    assert {name for name, _ in patched_env["shortcut"]} == set(
        ycode_uninstall.SHORTCUT_NAMES)
    for _, dirs in patched_env["shortcut"]:
        assert str(tmp_path / "PublicDesktop") not in dirs
    assert progress_log[-1] == 95.0


def test_user_scope_uninstall_keeps_config_by_default(tmp_path, patched_env):
    install_dir = _fake_install_dir(tmp_path)
    (tmp_path / "home" / ".ycode").mkdir(parents=True)

    res = run_uninstall(str(install_dir), scope="user", delete_config=False,
                        log=lambda m: None)

    assert res["deleted_config"] is False
    assert (tmp_path / "home" / ".ycode").exists()


def test_machine_scope_uninstall_uses_system_locations(tmp_path, patched_env):
    install_dir = _fake_install_dir(tmp_path)

    res = run_uninstall(str(install_dir), scope="machine", delete_config=False,
                        log=lambda m: None)

    assert res["scope"] == "machine"
    # machine 范围：系统 PATH 与用户 PATH 都清
    assert patched_env["system_path"] == [os.path.normpath(str(install_dir))]
    assert patched_env["user_path"] == [os.path.normpath(str(install_dir))]
    # 注册表：HKLM 与 HKCU 两个 hive 都清
    hives = {hive for _, hive in patched_env["registry"]}
    assert hives == {winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER}
    # 快捷方式目录包含公共桌面
    all_dirs = set().union(*(dirs for _, dirs in patched_env["shortcut"]))
    assert str(tmp_path / "PublicDesktop") in all_dirs


def test_detect_scope_machine_from_registry(tmp_path, monkeypatch):
    install_dir = str(tmp_path / "PF" / "Y-code")
    opened = []

    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(hive, path):
        opened.append(hive)
        if hive == winreg.HKEY_LOCAL_MACHINE:
            return _FakeKey()
        raise OSError("not found")

    monkeypatch.setattr(ycode_uninstall.winreg, "OpenKey", fake_open)
    monkeypatch.setattr(ycode_uninstall.winreg, "QueryValueEx",
                        lambda key, name: (install_dir, winreg.REG_SZ))

    assert detect_scope_for_install(install_dir) == "machine"
    assert opened[0] == winreg.HKEY_LOCAL_MACHINE  # machine 优先


def test_detect_scope_user_from_registry(tmp_path, monkeypatch):
    install_dir = os.path.join(str(tmp_path), "Apps", "Y-code")

    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(hive, path):
        if hive == winreg.HKEY_CURRENT_USER:
            return _FakeKey()
        raise OSError("not found")

    monkeypatch.setattr(ycode_uninstall.winreg, "OpenKey", fake_open)
    monkeypatch.setattr(ycode_uninstall.winreg, "QueryValueEx",
                        lambda key, name: (install_dir, winreg.REG_SZ))

    assert detect_scope_for_install(install_dir) == "user"


def test_detect_scope_fallback_by_location(tmp_path, monkeypatch):
    def fake_open(hive, path):
        raise OSError("not found")

    monkeypatch.setattr(ycode_uninstall.winreg, "OpenKey", fake_open)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))

    pf_dir = str(tmp_path / "Program Files" / "Y-code")
    user_dir = str(tmp_path / "Users" / "someone" / "Y-code")
    assert detect_scope_for_install(pf_dir) == "machine"
    assert detect_scope_for_install(user_dir) == "user"


def test_self_destruct_never_recursively_deletes_install_dir(tmp_path, monkeypatch):
    install_dir = str(tmp_path / "Y-code")
    captured = {}
    monkeypatch.setattr(ycode_uninstall.sys, "executable", os.path.join(install_dir, "ycode_uninstall.exe"))
    monkeypatch.setattr(
        ycode_uninstall.subprocess,
        "Popen",
        lambda command, **kwargs: captured.update(command=command, kwargs=kwargs),
    )

    ycode_uninstall.schedule_self_destruct(install_dir)

    argv = captured["command"]
    assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-File"]
    assert captured["kwargs"]["shell"] is False
    script = open(argv[-1], "r", encoding="utf-8-sig").read()
    assert "Directory]::Delete($installDir, $false)" in script
    assert "$true" not in script
    assert "ycode_uninstall.exe" in script
    os.unlink(argv[-1])


# ── 审计修复回归：T22 / M47 注册表归属标记 ─────────────────────────────

def test_detect_scope_matches_program_files_x86(tmp_path, monkeypatch):
    """T22：Program Files (x86) 之下同样判定 machine（旧实现只查 ProgramFiles）。"""
    def fake_open(hive, path):
        raise OSError("not found")

    monkeypatch.setattr(ycode_uninstall.winreg, "OpenKey", fake_open)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "Program Files (x86)"))

    pf86_dir = str(tmp_path / "Program Files (x86)" / "Y-code")
    user_dir = str(tmp_path / "Users" / "someone" / "Y-code")
    assert detect_scope_for_install(pf86_dir) == "machine"
    assert detect_scope_for_install(user_dir) == "user"


def test_detect_scope_conservative_when_env_missing(tmp_path, monkeypatch):
    """T22：两个 Program Files 环境变量都读不到时保守按 machine 处理并提示。"""
    def fake_open(hive, path):
        raise OSError("not found")

    monkeypatch.setattr(ycode_uninstall.winreg, "OpenKey", fake_open)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)

    logs = []
    assert detect_scope_for_install(str(tmp_path / "anywhere" / "Y-code"),
                                    log=logs.append) == "machine"
    assert any("Program Files" in m for m in logs)


def test_verify_install_dir_accepts_registry_marker(tmp_path, monkeypatch):
    """M47+High-1：version.txt 缺失时，注册表归属标记可作为第二归属证据。"""
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    (install_dir / "Y-code.exe").write_text("gui", encoding="utf-8")

    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    values = {"InstallLocation": str(install_dir), "YcodeInstallMarker": "Y-code"}
    monkeypatch.setattr(ycode_uninstall.winreg, "OpenKey",
                        lambda hive, path: _FakeKey())
    monkeypatch.setattr(ycode_uninstall.winreg, "QueryValueEx",
                        lambda key, name: (values[name], winreg.REG_SZ))

    assert ycode_uninstall._verify_ycode_install_dir(str(install_dir)) is True

    # 标记指向其他目录时不得通过
    values["InstallLocation"] = str(tmp_path / "elsewhere")
    assert ycode_uninstall._verify_ycode_install_dir(str(install_dir)) is False
