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
