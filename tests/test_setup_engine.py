import os
import subprocess
import zipfile

import pytest

import ycode_setup
from ycode_setup import InstallError, InstallOptions, run_install


def _fake_bundle(tmp_path, as_zip=False):
    """构造一个最小化的安装包目录结构。as_zip=True 时载荷为单文件压缩包。"""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    files = {
        "ycode.exe": "tui",
        "Y-code.exe": "gui",
        "_internal/core.dll": "dll",
        "_internal/frontend/index.html": "html",
    }
    if as_zip:
        with zipfile.ZipFile(bundle / "ycode_pkg.zip", "w") as zf:
            for rel, content in files.items():
                zf.writestr(rel, content)
    else:
        for rel, content in files.items():
            p = bundle / "ycode_pkg" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    (bundle / "ycode_uninstall.exe").write_text("uninst", encoding="utf-8")
    (bundle / "config.example.json").write_text("{}", encoding="utf-8")
    (bundle / "version.txt").write_text("9.9.9", encoding="utf-8")
    return bundle


@pytest.fixture()
def patched_env(tmp_path, monkeypatch):
    """隔离注册表 / 桌面 / 用户目录等真实系统副作用。"""
    calls = {"path": [], "shortcut": [], "registry": []}
    monkeypatch.setattr(ycode_setup, "add_to_user_path",
                        lambda d, log=print: calls["path"].append(d))
    monkeypatch.setattr(ycode_setup, "create_shortcut",
                        lambda *a, **k: calls["shortcut"].append(a))
    monkeypatch.setattr(ycode_setup, "write_uninstall_registry_key",
                        lambda **kw: calls["registry"].append(kw))
    monkeypatch.setattr(ycode_setup, "get_desktop_dir", lambda: str(tmp_path / "Desktop"))
    monkeypatch.setattr(ycode_setup.os.path, "expanduser", lambda p: str(tmp_path / "home"))
    return calls


def _startupinfo():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si


def test_full_install_with_cli_and_shortcut(tmp_path, patched_env):
    bundle = _fake_bundle(tmp_path)
    install_dir = str(tmp_path / "installed")
    progress_log = []
    res = run_install(
        "full", str(bundle),
        InstallOptions(install_dir, install_cli=True, create_desktop_shortcut=True),
        _startupinfo(), log=lambda m: None, progress=progress_log.append,
    )
    assert res["installed_cli"] is True
    assert res["desktop_shortcut"] is True
    assert (tmp_path / "installed" / "ycode.exe").exists()
    assert (tmp_path / "installed" / "Y-code.exe").exists()
    assert (tmp_path / "installed" / "_internal" / "frontend" / "index.html").exists()
    assert (tmp_path / "installed" / "ycode_uninstall.exe").exists()
    assert patched_env["path"] == [install_dir]
    assert len(patched_env["shortcut"]) == 1
    assert patched_env["registry"][0]["app_key_name"] == "Y-code"
    assert patched_env["registry"][0]["version"] == "9.9.9"
    assert progress_log[-1] == 100.0


def test_full_install_without_cli_removes_tui_exe(tmp_path, patched_env):
    bundle = _fake_bundle(tmp_path)
    install_dir = str(tmp_path / "installed")
    res = run_install(
        "full", str(bundle),
        InstallOptions(install_dir, install_cli=False, create_desktop_shortcut=False),
        _startupinfo(), log=lambda m: None,
    )
    assert res["installed_cli"] is False
    assert res["desktop_shortcut"] is False
    # 未选 CLI：ycode.exe 被移除，不写 PATH、不建快捷方式
    assert not (tmp_path / "installed" / "ycode.exe").exists()
    assert (tmp_path / "installed" / "Y-code.exe").exists()
    assert patched_env["path"] == []
    assert patched_env["shortcut"] == []


def test_tui_install_skips_frontend(tmp_path, patched_env):
    bundle = _fake_bundle(tmp_path)
    install_dir = str(tmp_path / "installed_tui")
    res = run_install(
        "tui", str(bundle), InstallOptions(install_dir), _startupinfo(), log=lambda m: None,
    )
    assert res["installed_cli"] is True
    assert (tmp_path / "installed_tui" / "ycode.exe").exists()
    assert not (tmp_path / "installed_tui" / "Y-code.exe").exists()
    assert (tmp_path / "installed_tui" / "_internal" / "core.dll").exists()
    # TUI 独立安装不携带 GUI 前端资源
    assert not (tmp_path / "installed_tui" / "_internal" / "frontend").exists()
    assert patched_env["registry"][0]["app_key_name"] == "Y-code-CLI"


def test_missing_payload_raises_install_error(tmp_path, patched_env):
    bundle = tmp_path / "empty_bundle"
    bundle.mkdir()
    with pytest.raises(InstallError):
        run_install("full", str(bundle), InstallOptions(str(tmp_path / "x")),
                    _startupinfo(), log=lambda m: None)


def test_zip_payload_full_install(tmp_path, patched_env):
    """单文件压缩包载荷（正式构建使用的形式）能正确释放全部文件。"""
    bundle = _fake_bundle(tmp_path, as_zip=True)
    install_dir = str(tmp_path / "installed_zip")
    progress_log = []
    res = run_install(
        "full", str(bundle),
        InstallOptions(install_dir, install_cli=True, create_desktop_shortcut=True),
        _startupinfo(), log=lambda m: None, progress=progress_log.append,
    )
    assert res["installed_cli"] is True
    assert (tmp_path / "installed_zip" / "ycode.exe").read_text() == "tui"
    assert (tmp_path / "installed_zip" / "Y-code.exe").read_text() == "gui"
    assert (tmp_path / "installed_zip" / "_internal" / "frontend" / "index.html").exists()
    assert progress_log[-1] == 100.0


def test_zip_payload_tui_install_skips_frontend(tmp_path, patched_env):
    bundle = _fake_bundle(tmp_path, as_zip=True)
    install_dir = str(tmp_path / "installed_zip_tui")
    res = run_install(
        "tui", str(bundle), InstallOptions(install_dir), _startupinfo(), log=lambda m: None,
    )
    assert (tmp_path / "installed_zip_tui" / "ycode.exe").exists()
    assert (tmp_path / "installed_zip_tui" / "_internal" / "core.dll").exists()
    assert not (tmp_path / "installed_zip_tui" / "_internal" / "frontend").exists()
    assert not (tmp_path / "installed_zip_tui" / "Y-code.exe").exists()


# ── 安装范围（user/machine）与重装检测 ─────────────────────────────

def test_full_install_machine_scope_uses_hklm_system_path_and_public_desktop(tmp_path, monkeypatch):
    """machine 范围：系统 PATH + 公共桌面快捷方式 + HKLM 卸载注册表"""
    import winreg as _wr
    bundle = _fake_bundle(tmp_path)
    calls = {"syspath": [], "userpath": [], "registry": [], "shortcut": []}
    monkeypatch.setattr(ycode_setup, "add_to_system_path",
                        lambda d, log=print: calls["syspath"].append(d))
    monkeypatch.setattr(ycode_setup, "add_to_user_path",
                        lambda d, log=print: calls["userpath"].append(d))
    monkeypatch.setattr(ycode_setup, "write_uninstall_registry_key",
                        lambda **kw: calls["registry"].append(kw))
    monkeypatch.setattr(ycode_setup, "create_shortcut",
                        lambda *a, **k: calls["shortcut"].append(a))
    monkeypatch.setattr(ycode_setup, "get_common_desktop_dir",
                        lambda: str(tmp_path / "PublicDesktop"))
    monkeypatch.setattr(ycode_setup.os.path, "expanduser", lambda p: str(tmp_path / "home"))

    install_dir = str(tmp_path / "installed_machine")
    res = run_install(
        "full", str(bundle),
        InstallOptions(install_dir, install_cli=True, create_desktop_shortcut=True,
                       scope="machine"),
        _startupinfo(), log=lambda m: None,
    )

    assert res["installed_cli"] is True
    assert calls["syspath"] == [install_dir]
    assert calls["userpath"] == []  # machine 范围绝不写用户 PATH
    assert calls["registry"][0]["hive"] == _wr.HKEY_LOCAL_MACHINE
    assert calls["shortcut"][0][0].startswith(str(tmp_path / "PublicDesktop"))


def test_full_install_user_scope_writes_hkcu(tmp_path, patched_env):
    """user 范围（默认）：卸载注册表写 HKCU，行为与改造前一致"""
    import winreg as _wr
    bundle = _fake_bundle(tmp_path)
    run_install(
        "full", str(bundle), InstallOptions(str(tmp_path / "installed")),
        _startupinfo(), log=lambda m: None,
    )
    assert patched_env["registry"][0]["hive"] == _wr.HKEY_CURRENT_USER


def test_default_install_dir_by_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LA"))
    assert ycode_setup.default_install_dir("machine") == str(tmp_path / "PF" / "Y-code")
    assert ycode_setup.default_install_dir("user") == str(tmp_path / "LA" / "Programs" / "Y-code")
    # 无参数默认 user（向后兼容）
    assert ycode_setup.default_install_dir() == str(tmp_path / "LA" / "Programs" / "Y-code")


class _FakeRegKey:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patch_registry(monkeypatch, user_location=None, machine_location=None):
    """按 hive 模拟卸载注册表中的 InstallLocation，返回可设置当前查询结果的对象。"""
    def fake_openkey(hive, path):
        import winreg as _wr
        if hive == _wr.HKEY_CURRENT_USER and user_location is not None:
            return _FakeRegKey()
        if hive == _wr.HKEY_LOCAL_MACHINE and machine_location is not None:
            return _FakeRegKey()
        raise OSError("key not found")

    def fake_queryvalue(key, name):
        # OpenKey 已按 hive 过滤，能走到这里说明该 hive 有记录，返回其路径
        return fake_queryvalue.location, 1

    monkeypatch.setattr(ycode_setup.winreg, "OpenKey", fake_openkey)
    monkeypatch.setattr(ycode_setup.winreg, "QueryValueEx", fake_queryvalue)
    return fake_queryvalue


def test_detect_previous_install_user_scope(tmp_path, monkeypatch):
    prev = tmp_path / "prev_user"
    prev.mkdir()
    hook = _patch_registry(monkeypatch, user_location=str(prev))
    hook.location = str(prev)

    scope, found = ycode_setup.detect_previous_install()

    assert scope == "user"
    assert found == os.path.normpath(str(prev))


def test_detect_previous_install_machine_wins(tmp_path, monkeypatch):
    """两个 hive 都有记录时以 machine 为准（覆盖安装必须回到同一范围）"""
    prev_m = tmp_path / "prev_machine"
    prev_m.mkdir()
    prev_u = tmp_path / "prev_user"
    prev_u.mkdir()
    hook = _patch_registry(monkeypatch, user_location=str(prev_u), machine_location=str(prev_m))
    hook.location = str(prev_m)

    scope, found = ycode_setup.detect_previous_install()

    assert scope == "machine"
    assert found == os.path.normpath(str(prev_m))


def test_detect_previous_install_none(tmp_path, monkeypatch):
    _patch_registry(monkeypatch)
    assert ycode_setup.detect_previous_install() == (None, None)


def test_parse_elevated_args():
    assert ycode_setup._parse_elevated_args([]) is None
    assert ycode_setup._parse_elevated_args(["--machine"]) is None  # 缺 --elevated
    opts = ycode_setup._parse_elevated_args(
        ["--elevated", "--machine", '--dir="D:\\Program Files\\Y-code"',
         "--cli=0", "--shortcut=1"])
    assert opts == {"scope": "machine", "dir": "D:\\Program Files\\Y-code",
                    "cli": False, "shortcut": True}
    # user 范围 + 默认开关
    opts2 = ycode_setup._parse_elevated_args(["--elevated", "--dir=D:\\Y-code"])
    assert opts2 == {"scope": "user", "dir": "D:\\Y-code", "cli": True, "shortcut": True}
