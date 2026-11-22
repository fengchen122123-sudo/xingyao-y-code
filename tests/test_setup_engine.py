import os
import subprocess
import zipfile
import ctypes
from types import SimpleNamespace

import pytest

import ycode_setup
from ycode_setup import InstallError, InstallOptions, run_install


def _fake_bundle(tmp_path, as_zip=False):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    files = {
        "ycode.exe": "cli",
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
    (bundle / "Y-code-uninstall.exe").write_text("uninst", encoding="utf-8")
    (bundle / "config.example.json").write_text("{}", encoding="utf-8")
    (bundle / "version.txt").write_text("9.9.9", encoding="utf-8")
    return bundle


@pytest.fixture()
def patched_env(tmp_path, monkeypatch):
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
    assert (tmp_path / "installed" / "Y-code-uninstall.exe").exists()
    assert patched_env["path"] == [install_dir]
    assert len(patched_env["shortcut"]) == 1
    assert patched_env["registry"][0]["app_key_name"] == "Y-code"
    assert patched_env["registry"][0]["version"] == "9.9.9"
    assert progress_log[-1] == 100.0


def test_full_install_without_cli_removes_cli_exe(tmp_path, patched_env):
    bundle = _fake_bundle(tmp_path)
    install_dir = str(tmp_path / "installed")
    res = run_install(
        "full", str(bundle),
        InstallOptions(install_dir, install_cli=False, create_desktop_shortcut=False),
        _startupinfo(), log=lambda m: None,
    )
    assert res["installed_cli"] is False
    assert res["desktop_shortcut"] is False
    assert not (tmp_path / "installed" / "ycode.exe").exists()
    assert (tmp_path / "installed" / "Y-code.exe").exists()
    assert patched_env["path"] == []
    assert patched_env["shortcut"] == []


def test_cli_install_skips_frontend(tmp_path, patched_env):
    bundle = _fake_bundle(tmp_path)
    install_dir = str(tmp_path / "installed_cli")
    res = run_install(
        "cli", str(bundle), InstallOptions(install_dir), _startupinfo(), log=lambda m: None,
    )
    assert res["installed_cli"] is True
    assert (tmp_path / "installed_cli" / "ycode.exe").exists()
    assert not (tmp_path / "installed_cli" / "Y-code.exe").exists()
    assert (tmp_path / "installed_cli" / "_internal" / "core.dll").exists()
    assert not (tmp_path / "installed_cli" / "_internal" / "frontend").exists()
    assert patched_env["registry"][0]["app_key_name"] == "Y-code-CLI"


def test_cli_install_without_uninstaller_skips_registry(tmp_path, patched_env):
    bundle = _fake_bundle(tmp_path)
    (bundle / "Y-code-uninstall.exe").unlink()
    install_dir = str(tmp_path / "installed_cli_no_uninst")
    run_install(
        "cli", str(bundle), InstallOptions(install_dir), _startupinfo(), log=lambda m: None,
    )
    assert not (tmp_path / "installed_cli_no_uninst" / "Y-code-uninstall.exe").exists()
    assert patched_env["registry"] == []


def test_missing_payload_raises_install_error(tmp_path, patched_env):
    bundle = tmp_path / "empty_bundle"
    bundle.mkdir()
    with pytest.raises(InstallError):
        run_install("full", str(bundle), InstallOptions(str(tmp_path / "x")),
                    _startupinfo(), log=lambda m: None)


def test_zip_payload_full_install(tmp_path, patched_env):
    bundle = _fake_bundle(tmp_path, as_zip=True)
    install_dir = str(tmp_path / "installed_zip")
    progress_log = []
    res = run_install(
        "full", str(bundle),
        InstallOptions(install_dir, install_cli=True, create_desktop_shortcut=True),
        _startupinfo(), log=lambda m: None, progress=progress_log.append,
    )
    assert res["installed_cli"] is True
    assert (tmp_path / "installed_zip" / "ycode.exe").read_text() == "cli"
    assert (tmp_path / "installed_zip" / "Y-code.exe").read_text() == "gui"
    assert (tmp_path / "installed_zip" / "_internal" / "frontend" / "index.html").exists()
    assert progress_log[-1] == 100.0


def test_zip_payload_cli_install_skips_frontend(tmp_path, patched_env):
    bundle = _fake_bundle(tmp_path, as_zip=True)
    install_dir = str(tmp_path / "installed_zip_cli")
    res = run_install(
        "cli", str(bundle), InstallOptions(install_dir), _startupinfo(), log=lambda m: None,
    )
    assert (tmp_path / "installed_zip_cli" / "ycode.exe").exists()
    assert (tmp_path / "installed_zip_cli" / "_internal" / "core.dll").exists()
    assert not (tmp_path / "installed_zip_cli" / "_internal" / "frontend").exists()
    assert not (tmp_path / "installed_zip_cli" / "Y-code.exe").exists()


def test_zip_payload_rejects_parent_traversal(tmp_path, patched_env):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with zipfile.ZipFile(bundle / "ycode_pkg.zip", "w") as zf:
        zf.writestr("ycode.exe", "cli")
        zf.writestr("Y-code.exe", "gui")
        zf.writestr("../outside.txt", "owned")

    with pytest.raises(InstallError, match="非法路径"):
        run_install(
            "full",
            str(bundle),
            InstallOptions(str(tmp_path / "installed")),
            _startupinfo(),
            log=lambda m: None,
        )

    assert not (tmp_path / "outside.txt").exists()


def test_zip_payload_rejects_existing_junction_before_writing(tmp_path, monkeypatch):
    bundle = _fake_bundle(tmp_path, as_zip=True)
    with zipfile.ZipFile(bundle / "ycode_pkg.zip", "a") as zf:
        zf.writestr("linked/escape.txt", "owned")
    install_dir = tmp_path / "installed"
    linked_dir = install_dir / "linked"
    linked_dir.mkdir(parents=True)
    real_isjunction = getattr(ycode_setup.os.path, "isjunction", lambda path: False)
    monkeypatch.setattr(
        ycode_setup.os.path,
        "isjunction",
        lambda path: os.path.normcase(os.path.abspath(path))
        == os.path.normcase(os.path.abspath(linked_dir))
        or real_isjunction(path),
        raising=False,
    )

    with pytest.raises(InstallError, match="符号链接|目录联接"):
        ycode_setup._copy_payload(
            "zip",
            str(bundle / "ycode_pkg.zip"),
            str(install_dir),
            cli_only=False,
            log=lambda message: None,
            progress=lambda value: None,
        )

    assert not (install_dir / "ycode.exe").exists()
    assert not (linked_dir / "escape.txt").exists()


def test_create_shortcut_escapes_powershell_single_quotes(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ycode_setup.subprocess,
        "run",
        lambda argv, **kwargs: captured.update(argv=argv, kwargs=kwargs),
    )

    ycode_setup.create_shortcut(
        r"C:\Tom's Desktop\Y-code.lnk",
        r"C:\Tom's Apps\Y-code.exe",
        r"C:\Tom's Apps",
        _startupinfo(),
    )

    command = captured["argv"][-1]
    assert "Tom''s Desktop" in command
    assert "Tom''s Apps" in command
    assert "Tom's Apps" not in command


def test_desktop_shortcut_uses_new_brand_name_and_removes_legacy(tmp_path, patched_env):
    assert ycode_setup.DESKTOP_SHORTCUT_NAME == "星瑶code.lnk"

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    legacy = desktop / "Y-code.lnk"
    legacy.write_text("old", encoding="utf-8")
    assert legacy.exists()

    bundle = _fake_bundle(tmp_path)
    install_dir = str(tmp_path / "installed")
    res = run_install(
        "full", str(bundle),
        InstallOptions(install_dir, install_cli=True, create_desktop_shortcut=True),
        _startupinfo(), log=lambda m: None,
    )

    assert res["desktop_shortcut"] is True
    assert len(patched_env["shortcut"]) == 1
    lnk_path = patched_env["shortcut"][0][0]
    assert lnk_path == os.path.join(str(desktop), "星瑶code.lnk")
    assert lnk_path.endswith("星瑶code.lnk")
    assert not legacy.exists()


def test_desktop_shortcut_legacy_cleanup_failure_does_not_abort(tmp_path, patched_env,
                                                               monkeypatch):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    legacy = desktop / "Y-code.lnk"
    legacy.write_text("old", encoding="utf-8")

    real_remove = os.remove

    def _boom(path, *args, **kwargs):
        if str(path).endswith("Y-code.lnk"):
            raise PermissionError("locked")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(ycode_setup.os, "remove", _boom)
    logs = []

    bundle = _fake_bundle(tmp_path)
    install_dir = str(tmp_path / "installed")
    res = run_install(
        "full", str(bundle),
        InstallOptions(install_dir, install_cli=True, create_desktop_shortcut=True),
        _startupinfo(), log=logs.append,
    )

    assert res["desktop_shortcut"] is True
    lnk_path = patched_env["shortcut"][0][0]
    assert lnk_path.endswith("星瑶code.lnk")
    assert any("清理旧版桌面快捷方式失败" in m for m in logs)


@pytest.mark.parametrize("func_name", ["add_to_user_path", "add_to_system_path"])
def test_path_registration_rejects_semicolon_in_install_dir(monkeypatch, func_name):
    create_key_called = []
    monkeypatch.setattr(
        ycode_setup.winreg,
        "CreateKey",
        lambda *args: create_key_called.append(args),
    )

    with pytest.raises(InstallError, match="分号"):
        getattr(ycode_setup, func_name)(r"C:\Y-code;C:\malicious")

    assert create_key_called == []


def test_relaunch_elevated_quotes_windows_arguments(monkeypatch):
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    def parse_windows_command_line(command):
        argc = ctypes.c_int()
        argv = shell32.CommandLineToArgvW(command, ctypes.byref(argc))
        assert argv
        try:
            return [argv[index] for index in range(argc.value)]
        finally:
            kernel32.LocalFree(argv)

    captured = {}
    fake_shell32 = SimpleNamespace(
        ShellExecuteW=lambda verb, action, exe, params, cwd, show: captured.update(
            exe=exe, params=params
        ) or 42
    )
    monkeypatch.setattr(ctypes.windll, "shell32", fake_shell32, raising=False)
    monkeypatch.setattr(ycode_setup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(ycode_setup.sys, "executable", r"C:\setup.exe")

    options = InstallOptions(r'C:\Apps\quote" --cli=0 --shortcut=0', scope="machine")

    assert ycode_setup.relaunch_elevated(options) is True
    expected = [
        "--elevated", "--machine", f"--dir={options.install_dir}",
        "--cli=1", "--shortcut=1",
    ]
    parsed = parse_windows_command_line(
        subprocess.list2cmdline(["placeholder.exe"]) + " " + captured["params"]
    )[1:]
    assert parsed == expected
    elevated_options = ycode_setup._parse_elevated_args(parsed)
    assert elevated_options == {
        "scope": "machine",
        "dir": options.install_dir,
        "cli": True,
        "shortcut": True,
    }


def test_seed_user_config_does_not_follow_existing_symlink(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "config.example.json").write_text('{"safe": true}', encoding="utf-8")
    (bundle / "system_prompt.md").write_text("safe prompt", encoding="utf-8")
    home = tmp_path / "home"
    ycode_dir = home / ".ycode"
    ycode_dir.mkdir(parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text("do not overwrite", encoding="utf-8")
    try:
        (ycode_dir / "config.json").symlink_to(victim)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建测试符号链接")
    monkeypatch.setattr(ycode_setup.os.path, "expanduser", lambda p: str(home))

    ycode_setup.seed_user_config(str(bundle), log=lambda message: None)

    assert victim.read_text(encoding="utf-8") == "do not overwrite"


def test_seed_user_config_seeds_empty_config_instead_of_example(tmp_path, monkeypatch):
    import json

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "config.example.json").write_text(
        json.dumps({
            "active_model_id": "model_1",
            "providers": [
                {"id": "p1", "name": "Agnes AI", "api_key": "sk-placeholder"},
                {"id": "p2", "name": "深度求索", "api_key": "sk-placeholder"},
            ],
        }),
        encoding="utf-8",
    )
    home = tmp_path / "home"
    monkeypatch.setattr(ycode_setup.os.path, "expanduser", lambda p: str(home))

    ycode_setup.seed_user_config(str(bundle), log=lambda message: None)

    config_path = home / ".ycode" / "config.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data == {"active_model_id": None, "providers": []}


def test_full_install_machine_scope_uses_hklm_system_path_and_public_desktop(tmp_path, monkeypatch):
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
    monkeypatch.setenv("ProgramFiles", str(tmp_path))

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


def test_machine_install_rejects_location_outside_program_files(tmp_path, monkeypatch):
    bundle = _fake_bundle(tmp_path)
    protected_root = tmp_path / "Program Files"
    protected_root.mkdir()
    monkeypatch.setenv("ProgramFiles", str(protected_root))

    with pytest.raises(InstallError, match="Program Files"):
        run_install(
            "full", str(bundle),
            InstallOptions(str(tmp_path / "attacker-writable"), scope="machine"),
            _startupinfo(), log=lambda _message: None,
        )

    assert not (tmp_path / "attacker-writable").exists()


def test_full_install_user_scope_writes_hkcu(tmp_path, patched_env):
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
    assert ycode_setup.default_install_dir() == str(tmp_path / "LA" / "Programs" / "Y-code")


class _FakeRegKey:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _patch_registry(monkeypatch, user_location=None, machine_location=None):
    def fake_openkey(hive, path):
        import winreg as _wr
        if hive == _wr.HKEY_CURRENT_USER and user_location is not None:
            return _FakeRegKey()
        if hive == _wr.HKEY_LOCAL_MACHINE and machine_location is not None:
            return _FakeRegKey()
        raise OSError("key not found")

    def fake_queryvalue(key, name):
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
    assert ycode_setup._parse_elevated_args(["--machine"]) is None
    opts = ycode_setup._parse_elevated_args(
        ["--elevated", "--machine", '--dir="D:\\Program Files\\Y-code"',
         "--cli=0", "--shortcut=1"])
    assert opts == {"scope": "machine", "dir": "D:\\Program Files\\Y-code",
                    "cli": False, "shortcut": True}
    opts2 = ycode_setup._parse_elevated_args(["--elevated", "--dir=D:\\Y-code"])
    assert opts2 == {"scope": "user", "dir": "D:\\Y-code", "cli": True, "shortcut": True}


def test_normalize_browsed_install_dir_appends_ycode_subdir():
    assert ycode_setup.normalize_browsed_install_dir("D:\\") == "D:\\Y-code"
    assert ycode_setup.normalize_browsed_install_dir("D:") == "D:\\Y-code"
    assert ycode_setup.normalize_browsed_install_dir("E:/") == "E:\\Y-code"
    assert ycode_setup.normalize_browsed_install_dir("D:\\MyApps") == "D:\\MyApps\\Y-code"
    assert ycode_setup.normalize_browsed_install_dir("D:\\tools\\") == "D:\\tools\\Y-code"


def test_normalize_browsed_install_dir_keeps_ycode_named_dir():
    assert ycode_setup.normalize_browsed_install_dir("D:\\Y-code") == "D:\\Y-code"
    assert ycode_setup.normalize_browsed_install_dir("D:\\y-code") == "D:\\y-code"
    assert ycode_setup.normalize_browsed_install_dir("D:\\y-CODE\\") == "D:\\y-CODE"


def test_parse_elevated_args_cli_10_is_not_true():
    opts = ycode_setup._parse_elevated_args(
        ["--elevated", "--dir=D:\\Y-code", "--cli=10", "--shortcut=10"])
    assert opts["cli"] is False
    assert opts["shortcut"] is False
    opts = ycode_setup._parse_elevated_args(
        ["--elevated", "--dir=D:\\Y-code", "--cli=1", "--shortcut=1"])
    assert opts["cli"] is True
    assert opts["shortcut"] is True


def test_copy_payload_rejects_symlink_target_file(tmp_path, monkeypatch):
    bundle = _fake_bundle(tmp_path, as_zip=True)
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    victim = install_dir / "ycode.exe"
    victim.write_text("old", encoding="utf-8")
    real_islink = os.path.islink
    monkeypatch.setattr(
        ycode_setup.os.path,
        "islink",
        lambda path: os.path.normcase(os.path.abspath(path))
        == os.path.normcase(os.path.abspath(str(victim)))
        or real_islink(path),
    )

    with pytest.raises(InstallError, match="符号链接|目录联接"):
        ycode_setup._copy_payload(
            "zip", str(bundle / "ycode_pkg.zip"), str(install_dir),
            cli_only=False, log=lambda m: None, progress=lambda p: None,
        )

    assert victim.read_text(encoding="utf-8") == "old"


def test_copy_payload_rolls_back_created_files_on_failure(tmp_path, patched_env):
    bundle = _fake_bundle(tmp_path, as_zip=True)
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    (install_dir / "Y-code.exe").mkdir()

    with pytest.raises(Exception):
        ycode_setup._copy_payload(
            "zip", str(bundle / "ycode_pkg.zip"), str(install_dir),
            cli_only=False, log=lambda m: None, progress=lambda p: None,
        )

    assert not (install_dir / "ycode.exe").exists()
    assert (install_dir / "Y-code.exe").is_dir()


def test_overwrite_install_prunes_files_removed_in_new_version(tmp_path, patched_env):
    bundle = _fake_bundle(tmp_path)
    install_dir = str(tmp_path / "installed")
    run_install("full", str(bundle),
                InstallOptions(install_dir, install_cli=False, create_desktop_shortcut=False),
                _startupinfo(), log=lambda m: None)
    assert (tmp_path / "installed" / "_internal" / "core.dll").exists()

    bundle2 = tmp_path / "bundle2"
    (bundle2 / "ycode_pkg" / "_internal").mkdir(parents=True)
    (bundle2 / "ycode_pkg" / "ycode.exe").write_text("cli2", encoding="utf-8")
    (bundle2 / "ycode_pkg" / "Y-code.exe").write_text("gui2", encoding="utf-8")
    (bundle2 / "Y-code-uninstall.exe").write_text("uninst", encoding="utf-8")
    (bundle2 / "version.txt").write_text("9.9.10", encoding="utf-8")
    run_install("full", str(bundle2),
                InstallOptions(install_dir, install_cli=False, create_desktop_shortcut=False),
                _startupinfo(), log=lambda m: None)

    assert not (tmp_path / "installed" / "_internal" / "core.dll").exists()
    assert (tmp_path / "installed" / "Y-code.exe").read_text() == "gui2"


def test_is_unrecognized_non_empty_dir(tmp_path):
    assert ycode_setup._is_unrecognized_non_empty_dir(str(tmp_path / "missing")) is False
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ycode_setup._is_unrecognized_non_empty_dir(str(empty)) is False
    other = tmp_path / "other"
    other.mkdir()
    (other / "readme.txt").write_text("x", encoding="utf-8")
    assert ycode_setup._is_unrecognized_non_empty_dir(str(other)) is True
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "Y-code.exe").write_text("gui", encoding="utf-8")
    assert ycode_setup._is_unrecognized_non_empty_dir(str(existing)) is False


def test_launch_gui_app_direct_when_not_admin(tmp_path, monkeypatch):
    (tmp_path / "Y-code.exe").write_text("gui", encoding="utf-8")
    calls = []
    monkeypatch.setattr(ycode_setup, "is_user_an_admin", lambda: False)
    monkeypatch.setattr(ycode_setup.subprocess, "Popen",
                        lambda args, **kw: calls.append((args, kw)))
    assert ycode_setup.launch_gui_app(str(tmp_path), _startupinfo()) is True
    assert calls[0][0] == [str(tmp_path / "Y-code.exe")]


def test_launch_gui_app_de_elevates_via_explorer_when_admin(tmp_path, monkeypatch):
    (tmp_path / "Y-code.exe").write_text("gui", encoding="utf-8")
    calls = []
    monkeypatch.setattr(ycode_setup, "is_user_an_admin", lambda: True)
    monkeypatch.setattr(ycode_setup.subprocess, "Popen",
                        lambda args, **kw: calls.append((args, kw)))
    assert ycode_setup.launch_gui_app(str(tmp_path), _startupinfo()) is True
    assert calls[0][0][0] == "explorer.exe"
    assert calls[0][0][1] == str(tmp_path / "Y-code.exe")


def test_launch_gui_app_missing_exe_returns_false(tmp_path):
    assert ycode_setup.launch_gui_app(str(tmp_path), _startupinfo()) is False


def test_launch_gui_app_popen_failure_returns_false(tmp_path, monkeypatch):
    (tmp_path / "Y-code.exe").write_text("gui", encoding="utf-8")
    monkeypatch.setattr(ycode_setup, "is_user_an_admin", lambda: False)

    def _boom(*a, **k):
        raise OSError("blocked")

    monkeypatch.setattr(ycode_setup.subprocess, "Popen", _boom)
    assert ycode_setup.launch_gui_app(str(tmp_path), _startupinfo()) is False


def test_copy_payload_failure_preserves_existing_install(tmp_path):
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    (install_dir / "_internal" / "core.dll").write_text("old-core", encoding="utf-8")
    (install_dir / "_internal" / "legacy.dll").write_text("old-only", encoding="utf-8")
    (install_dir / "_internal" / "ycode_install_manifest.txt").write_text(
        "ycode.exe\nY-code.exe\n_internal/core.dll\n_internal/legacy.dll\n",
        encoding="utf-8")
    (install_dir / "ycode.exe").mkdir()

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    zip_path = bundle / "ycode_pkg.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("_internal/core.dll", "new-core")
        zf.writestr("_internal/frontend/index.html", "new-html")
        zf.writestr("ycode.exe", "new-cli")
        zf.writestr("Y-code.exe", "new-gui")

    with pytest.raises(InstallError, match="ycode.exe"):
        ycode_setup._copy_payload(
            "zip", str(zip_path), str(install_dir),
            cli_only=False, log=lambda m: None, progress=lambda p: None,
        )

    assert (install_dir / "_internal" / "core.dll").read_text(encoding="utf-8") == "old-core"
    assert (install_dir / "_internal" / "legacy.dll").read_text(encoding="utf-8") == "old-only"
    assert (install_dir / "ycode.exe").is_dir()
    assert not (install_dir / "_internal" / "frontend" / "index.html").exists()
    leftovers = [p.name for p in install_dir.rglob("*")
                 if p.name.endswith((".ycode-bak", ".ycode-new"))]
    assert leftovers == []


def test_copy_payload_renames_aside_locked_file(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    target = install_dir / "Y-code.exe"
    target.write_text("old-gui", encoding="utf-8")

    bundle = _fake_bundle(tmp_path, as_zip=True)
    target_norm = os.path.normcase(os.path.abspath(str(target)))
    real_open = open

    def _locked_open(path, mode="r", *args, **kwargs):
        if (os.path.normcase(os.path.abspath(str(path))) == target_norm
                and "w" in str(mode) and os.path.exists(str(path))):
            raise PermissionError(13, "locked by running process")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(ycode_setup, "open", _locked_open, raising=False)

    ycode_setup._copy_payload(
        "zip", str(bundle / "ycode_pkg.zip"), str(install_dir),
        cli_only=False, log=lambda m: None, progress=lambda p: None,
    )

    assert target.read_text(encoding="utf-8") == "gui"
    assert not (install_dir / "Y-code.exe.ycode-bak").exists()


def test_copy_payload_prunes_only_after_successful_copy(tmp_path):
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    legacy = install_dir / "_internal" / "legacy.dll"
    legacy.write_text("old-only", encoding="utf-8")
    manifest = install_dir / "_internal" / "ycode_install_manifest.txt"
    manifest.write_text("ycode.exe\n_internal/legacy.dll\n", encoding="utf-8")

    (install_dir / "ycode.exe").mkdir()
    failing_zip = tmp_path / "fail.zip"
    with zipfile.ZipFile(failing_zip, "w") as zf:
        zf.writestr("ycode.exe", "new-cli")
    with pytest.raises(InstallError):
        ycode_setup._copy_payload(
            "zip", str(failing_zip), str(install_dir),
            cli_only=False, log=lambda m: None, progress=lambda p: None,
        )
    assert legacy.exists()

    (install_dir / "ycode.exe").rmdir()
    ok_zip = tmp_path / "ok.zip"
    with zipfile.ZipFile(ok_zip, "w") as zf:
        zf.writestr("ycode.exe", "new-cli")
    ycode_setup._copy_payload(
        "zip", str(ok_zip), str(install_dir),
        cli_only=False, log=lambda m: None, progress=lambda p: None,
    )
    assert not legacy.exists()
    assert not legacy.exists()
    assert "ycode.exe" in manifest.read_text(encoding="utf-8")


def test_copy_payload_premove_then_failure_restores_old_bytes(tmp_path):
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    core = install_dir / "_internal" / "core.dll"
    core.write_bytes(b"OLD-CORE-BYTES")
    (install_dir / "Y-code.exe").mkdir()

    zip_path = tmp_path / "payload.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("_internal/core.dll", "NEW-CORE-BYTES")
        zf.writestr("Y-code.exe", "new-gui")

    with pytest.raises(InstallError):
        ycode_setup._copy_payload(
            "zip", str(zip_path), str(install_dir),
            cli_only=False, log=lambda m: None, progress=lambda p: None,
        )

    assert core.read_bytes() == b"OLD-CORE-BYTES"
    leftovers = [p.name for p in install_dir.rglob("*")
                 if p.name.endswith((".ycode-bak", ".ycode-new"))]
    assert leftovers == []


def test_copy_payload_restores_orphan_bak_before_writing(tmp_path):
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    orphan = install_dir / "_internal" / "core.dll.ycode-bak"
    orphan.write_bytes(b"OLD-CORE")
    assert not (install_dir / "_internal" / "core.dll").exists()

    zip_path = tmp_path / "payload.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("ycode.exe", "cli")

    ycode_setup._copy_payload(
        "zip", str(zip_path), str(install_dir),
        cli_only=False, log=lambda m: None, progress=lambda p: None,
    )

    assert (install_dir / "_internal" / "core.dll").read_bytes() == b"OLD-CORE"
    assert not orphan.exists()


def test_copy_payload_removes_redundant_bak_when_target_present(tmp_path):
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    target = install_dir / "_internal" / "core.dll"
    target.write_bytes(b"CURRENT")
    redundant = install_dir / "_internal" / "core.dll.ycode-bak"
    redundant.write_bytes(b"STALE")

    zip_path = tmp_path / "payload.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("ycode.exe", "cli")

    ycode_setup._copy_payload(
        "zip", str(zip_path), str(install_dir),
        cli_only=False, log=lambda m: None, progress=lambda p: None,
    )

    assert target.read_bytes() == b"CURRENT"
    assert not redundant.exists()


def test_copy_payload_falls_back_when_rename_aside_fails(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    target = install_dir / "Y-code.exe"
    target.write_bytes(b"OLD-GUI")

    def _boom_replace(*args, **kwargs):
        raise OSError("rename blocked (no FILE_SHARE_DELETE)")

    monkeypatch.setattr(ycode_setup.os, "replace", _boom_replace)
    logs = []

    bundle = _fake_bundle(tmp_path, as_zip=True)
    ycode_setup._copy_payload(
        "zip", str(bundle / "ycode_pkg.zip"), str(install_dir),
        cli_only=False, log=logs.append, progress=lambda p: None,
    )

    assert target.read_bytes() == b"gui"
    assert not (install_dir / "Y-code.exe.ycode-bak").exists()
    assert any("改名让位失败" in m for m in logs)


def _patch_program_files(monkeypatch, pf):
    monkeypatch.setenv("ProgramFiles", str(pf))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)


def test_is_under_program_files_accepts_subdir(tmp_path, monkeypatch):
    pf = tmp_path / "Program Files"
    _patch_program_files(monkeypatch, pf)
    assert ycode_setup._is_under_program_files(str(pf / "Y-code")) is True


def test_is_under_program_files_rejects_root_and_trailing_slash(tmp_path, monkeypatch):
    pf = tmp_path / "Program Files"
    _patch_program_files(monkeypatch, pf)
    assert ycode_setup._is_under_program_files(str(pf)) is False
    assert ycode_setup._is_under_program_files(str(pf) + "\\") is False


def test_is_under_program_files_case_insensitive(tmp_path, monkeypatch):
    pf = tmp_path / "Program Files"
    _patch_program_files(monkeypatch, pf)
    assert ycode_setup._is_under_program_files(str(pf / "Y-CODE").upper()) is True


def test_is_under_program_files_rejects_unrelated_drive(tmp_path, monkeypatch):
    pf = tmp_path / "Program Files"
    _patch_program_files(monkeypatch, pf)
    assert ycode_setup._is_under_program_files(str(tmp_path / "other" / "Y-code")) is False


def test_is_under_program_files_matches_engine_validation(tmp_path, monkeypatch):
    pf = tmp_path / "Program Files"
    _patch_program_files(monkeypatch, pf)
    cases = [
        str(pf / "Y-code"),
        str(pf / "Y-CODE") + "\\",
        str(pf),
        str(tmp_path / "elsewhere"),
    ]
    for path in cases:
        helper_ok = ycode_setup._is_under_program_files(path)
        try:
            ycode_setup._validate_install_target(path, "machine")
            engine_ok = True
        except InstallError:
            engine_ok = False
        assert helper_ok == engine_ok, path


class _FakeVar:

    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _scope_wizard(tmp_path, monkeypatch, scope, cur_dir, previous=None):
    pf = tmp_path / "Program Files"
    _patch_program_files(monkeypatch, pf)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setattr(ycode_setup.os.path, "expanduser",
                        lambda p: str(tmp_path / "home"))
    wizard = ycode_setup.SetupWizard.__new__(ycode_setup.SetupWizard)
    wizard.previous_install = previous
    wizard.mode = "full"
    wizard._dir_hint = None
    wizard._dir_invalid = False
    wizard._dir_note = ""
    wizard.var_scope = _FakeVar(scope)
    wizard.var_dir = _FakeVar(cur_dir)
    return wizard


def test_on_scope_change_corrects_manual_path_to_machine_default(tmp_path, monkeypatch):
    wizard = _scope_wizard(tmp_path, monkeypatch, "machine",
                           str(tmp_path / "custom" / "Y-code"))
    wizard._on_scope_change()
    assert wizard.var_dir.get() == ycode_setup.default_install_dir("machine")
    assert "已自动改为" in wizard._dir_note


def test_on_scope_change_keeps_valid_machine_path(tmp_path, monkeypatch):
    valid = str(tmp_path / "Program Files" / "Y-code")
    wizard = _scope_wizard(tmp_path, monkeypatch, "machine", valid)
    wizard._on_scope_change()
    assert wizard.var_dir.get() == valid
    assert wizard._dir_note == ""


def test_on_scope_change_back_to_user_restores_user_default(tmp_path, monkeypatch):
    wizard = _scope_wizard(tmp_path, monkeypatch, "user", "")
    # 在环境隔离生效后再取 machine 默认路径（否则会读到真实 ProgramFiles）
    wizard.var_dir.set(ycode_setup.default_install_dir("machine"))
    wizard._on_scope_change()
    assert wizard.var_dir.get() == ycode_setup.default_install_dir("user")


def test_on_scope_change_locked_install_keeps_path(tmp_path, monkeypatch):
    old = str(tmp_path / "old-install")
    wizard = _scope_wizard(tmp_path, monkeypatch, "machine", old, previous=old)
    wizard._on_scope_change()
    assert wizard.var_dir.get() == old


def test_probe_locked_payload_files_detects_locked_exe(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    locked = install_dir / "Y-code.exe"
    locked.write_bytes(b"gui")
    (install_dir / "ycode.exe").write_bytes(b"cli")
    (install_dir / "readme.txt").write_text("x", encoding="utf-8")

    real_open = os.open

    def _open(path, flags, *args, **kwargs):
        if os.path.basename(str(path)) == "Y-code.exe":
            raise PermissionError(13, "sharing violation")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ycode_setup.os, "open", _open)
    assert ycode_setup._probe_locked_payload_files(str(install_dir)) == [str(locked)]


def test_probe_locked_payload_files_has_no_side_effect(tmp_path):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    exe = install_dir / "Y-code.exe"
    exe.write_bytes(b"gui")
    before = exe.stat().st_mtime_ns
    assert ycode_setup._probe_locked_payload_files(str(install_dir)) == []
    assert exe.read_bytes() == b"gui"
    assert exe.stat().st_mtime_ns == before


def test_resolve_occupied_files_retries_after_kill(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    target = install_dir / "Y-code.exe"
    target.write_bytes(b"gui")

    probes = {"n": 0}

    def _fake_probe(_dir):
        probes["n"] += 1
        return [str(target)] if probes["n"] == 1 else []

    monkeypatch.setattr(ycode_setup, "_probe_locked_payload_files", _fake_probe)
    monkeypatch.setattr(
        ycode_setup, "_iter_own_process_pids",
        lambda name, _dir: [4242] if name == "Y-code.exe" else [])

    calls = []
    monkeypatch.setattr(
        ycode_setup.subprocess, "run",
        lambda args, **kw: calls.append((args, kw)) or SimpleNamespace(returncode=0))

    asks = []
    result = ycode_setup._resolve_occupied_files(
        str(install_dir), log=lambda m: None,
        ask=lambda locked: asks.append(list(locked)) or "kill",
    )

    assert result == []
    assert asks == [[str(target)]]
    assert probes["n"] >= 2
    args_seen = [c[0] for c in calls]
    assert ["taskkill", "/F", "/PID", "4242"] in args_seen
    assert all("/IM" not in a for a in args_seen)
    assert all(isinstance(a, list) for a in args_seen)


def test_resolve_occupied_files_continue_choice_keeps_locked(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    target = install_dir / "Y-code.exe"
    target.write_bytes(b"gui")
    monkeypatch.setattr(ycode_setup, "_probe_locked_payload_files", lambda _d: [str(target)])

    def _boom(*a, **k):
        raise AssertionError("选择继续时不应结束进程")

    monkeypatch.setattr(ycode_setup.subprocess, "run", _boom)
    result = ycode_setup._resolve_occupied_files(
        str(install_dir), log=lambda m: None, ask=lambda locked: "continue")
    assert result == [str(target)]


def test_resolve_occupied_files_non_interactive_does_not_block(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    target = install_dir / "Y-code.exe"
    target.write_bytes(b"gui")
    monkeypatch.setattr(ycode_setup, "_probe_locked_payload_files", lambda _d: [str(target)])

    def _boom(*a, **k):
        raise AssertionError("非交互场景不应结束进程")

    monkeypatch.setattr(ycode_setup.subprocess, "run", _boom)
    logs = []
    result = ycode_setup._resolve_occupied_files(str(install_dir), log=logs.append, ask=None)
    assert result == [str(target)]
    assert any("非交互" in m for m in logs)


def test_run_install_cli_mode_skips_occupancy_prompt(tmp_path, patched_env, monkeypatch):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    (install_dir / "ycode.exe").write_bytes(b"old-cli")
    monkeypatch.setattr(ycode_setup, "_probe_locked_payload_files",
                        lambda _d: [str(install_dir / "ycode.exe")])
    called = []
    bundle = _fake_bundle(tmp_path)
    run_install("cli", str(bundle), InstallOptions(str(install_dir)), _startupinfo(),
                log=lambda m: None,
                ask_occupancy=lambda locked: called.append(locked) or "kill")
    assert called == []
    assert (install_dir / "ycode.exe").read_bytes() == b"cli"


def test_make_way_copy_backup_restores_old_bytes_on_failure(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    core = install_dir / "_internal" / "core.dll"
    core.write_bytes(b"OLD-CORE-BYTES")
    (install_dir / "Y-code.exe").mkdir()

    def _boom_replace(*args, **kwargs):
        raise OSError("rename blocked (no FILE_SHARE_DELETE)")

    monkeypatch.setattr(ycode_setup.os, "replace", _boom_replace)

    zip_path = tmp_path / "payload.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("_internal/core.dll", "NEW-CORE-BYTES")
        zf.writestr("Y-code.exe", "new-gui")

    with pytest.raises(InstallError):
        ycode_setup._copy_payload(
            "zip", str(zip_path), str(install_dir),
            cli_only=False, log=lambda m: None, progress=lambda p: None,
        )

    assert core.read_bytes() == b"OLD-CORE-BYTES"
    leftovers = [p.name for p in install_dir.rglob("*")
                 if p.name.endswith((".ycode-bak", ".ycode-new"))]
    assert leftovers == []


def test_make_way_backup_failure_keeps_current_behavior(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    target = install_dir / "Y-code.exe"
    target.write_bytes(b"OLD-GUI")

    def _boom_replace(*args, **kwargs):
        raise OSError("rename blocked")

    def _boom_copy(*args, **kwargs):
        raise OSError("copy blocked")

    monkeypatch.setattr(ycode_setup.os, "replace", _boom_replace)
    monkeypatch.setattr(ycode_setup.shutil, "copy2", _boom_copy)

    bundle = _fake_bundle(tmp_path, as_zip=True)
    logs = []
    ycode_setup._copy_payload(
        "zip", str(bundle / "ycode_pkg.zip"), str(install_dir),
        cli_only=False, log=logs.append, progress=lambda p: None,
    )

    assert target.read_bytes() == b"gui"
    assert any("复制备份失败" in m for m in logs)
    assert not (install_dir / "Y-code.exe.ycode-bak").exists()


def test_copy_payload_clears_stale_reboot_tmp(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    core = install_dir / "_internal" / "core.dll"
    core.write_bytes(b"OLD-CORE")
    stale = install_dir / "_internal" / "core.dll.ycode-new"
    stale.write_bytes(b"ANCIENT-CORE")
    gui = install_dir / "Y-code.exe"
    gui.write_bytes(b"OLD-GUI")

    bundle = _fake_bundle(tmp_path, as_zip=True)
    real_open = open
    gui_norm = os.path.normcase(os.path.abspath(str(gui)))

    def _locked_open(path, mode="r", *args, **kwargs):
        if (os.path.normcase(os.path.abspath(str(path))) == gui_norm
                and "w" in str(mode)):
            raise PermissionError(13, "locked by running process")
        return real_open(path, mode, *args, **kwargs)

    def _blocked(*_args, **_kwargs):
        raise OSError("blocked by test")

    monkeypatch.setattr(ycode_setup, "open", _locked_open, raising=False)
    monkeypatch.setattr(ycode_setup, "_rename_aside", lambda *a, **k: None)
    monkeypatch.setattr(ycode_setup.shutil, "copy2", _blocked)
    monkeypatch.setattr(ycode_setup, "_schedule_reboot_replace", lambda *a, **k: None)

    ycode_setup._copy_payload(
        "zip", str(bundle / "ycode_pkg.zip"), str(install_dir),
        cli_only=False, log=lambda m: None, progress=lambda p: None,
    )

    assert not stale.exists()
    assert core.read_bytes() == b"dll"
    assert (install_dir / "Y-code.exe.ycode-new").exists()


def test_copy_payload_keeps_old_file_for_reboot_dest(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    gui = install_dir / "Y-code.exe"
    gui.write_bytes(b"OLD-GUI")

    bundle = _fake_bundle(tmp_path, as_zip=True)
    real_open = open
    gui_norm = os.path.normcase(os.path.abspath(str(gui)))

    def _locked_open(path, mode="r", *args, **kwargs):
        if (os.path.normcase(os.path.abspath(str(path))) == gui_norm
                and "w" in str(mode)):
            raise PermissionError(13, "locked by running process")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(ycode_setup, "open", _locked_open, raising=False)
    monkeypatch.setattr(ycode_setup, "_schedule_reboot_replace", lambda *a, **k: None)

    logs = []
    ycode_setup._copy_payload(
        "zip", str(bundle / "ycode_pkg.zip"), str(install_dir),
        cli_only=False, log=logs.append, progress=lambda p: None,
    )

    assert gui.read_bytes() == b"OLD-GUI"
    assert not (install_dir / "Y-code.exe.ycode-bak").exists()
    assert (install_dir / "Y-code.exe.ycode-new").exists()
    assert any("将在系统重启后更新" in m for m in logs)
    assert (install_dir / "_internal" / "core.dll").read_bytes() == b"dll"


def test_rename_aside_keeps_registered_backup(tmp_path):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    dest = install_dir / "core.dll"
    dest.write_bytes(b"OLD")
    bak = install_dir / "core.dll.ycode-bak"
    bak.write_bytes(b"OLD")
    registered = [(str(bak), str(dest))]

    assert ycode_setup._rename_aside(str(dest), lambda m: None, registered) is None
    assert bak.read_bytes() == b"OLD"
    assert dest.read_bytes() == b"OLD"

    other = install_dir / "gui.exe"
    other.write_bytes(b"OLD2")
    stale = install_dir / "gui.exe.ycode-bak"
    stale.write_bytes(b"STALE")
    assert ycode_setup._rename_aside(str(other), lambda m: None, []) == str(stale)
    assert stale.read_bytes() == b"OLD2"


def test_copy_payload_does_not_report_unmodified_file_as_unrestored(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    core = install_dir / "_internal" / "core.dll"
    core.write_bytes(b"OLD-CORE")

    bundle = _fake_bundle(tmp_path, as_zip=True)
    real_open = open
    real_replace = os.replace
    core_norm = os.path.normcase(os.path.abspath(str(core)))

    def _locked_open(path, mode="r", *args, **kwargs):
        if (os.path.normcase(os.path.abspath(str(path))) == core_norm
                and "w" in str(mode)):
            raise PermissionError(13, "locked by running process")
        return real_open(path, mode, *args, **kwargs)

    def _replace(src, dst, *args, **kwargs):
        if os.path.normcase(os.path.abspath(str(src))) == core_norm:
            raise PermissionError(32, "sharing violation")
        return real_replace(src, dst, *args, **kwargs)

    def _blocked(*_args, **_kwargs):
        raise OSError("无法安排重启替换")

    monkeypatch.setattr(ycode_setup, "open", _locked_open, raising=False)
    monkeypatch.setattr(ycode_setup.os, "replace", _replace)
    monkeypatch.setattr(ycode_setup, "_schedule_reboot_replace", _blocked)

    with pytest.raises(InstallError) as excinfo:
        ycode_setup._copy_payload(
            "zip", str(bundle / "ycode_pkg.zip"), str(install_dir),
            cli_only=False, log=lambda m: None, progress=lambda p: None,
        )

    message = str(excinfo.value)
    assert core.read_bytes() == b"OLD-CORE"
    assert "未能恢复" not in message
    leftovers = [p.name for p in install_dir.rglob("*")
                 if p.name.endswith((".ycode-bak", ".ycode-new"))]
    assert leftovers == []


def test_iter_own_process_pids_filters_by_install_dir(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    inside = install_dir / "Y-code.exe"
    outside = tmp_path / "portable" / "Y-code.exe"
    outside.parent.mkdir()
    monkeypatch.setattr(
        ycode_setup, "_iter_process_images",
        lambda name: [(11, str(inside)), (22, str(outside)), (33, "")])

    assert ycode_setup._iter_own_process_pids("Y-code.exe", str(install_dir)) == [11]


def test_taskkill_uses_pid_and_falls_back_to_image_name(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    monkeypatch.setattr(ycode_setup, "_iter_own_process_pids", lambda name, d: [777])
    calls = []
    monkeypatch.setattr(
        ycode_setup.subprocess, "run",
        lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0))

    assert ycode_setup._taskkill("Y-code.exe", lambda m: None,
                                 install_dir=str(install_dir)) is True
    assert calls == [["taskkill", "/F", "/PID", "777"]]

    calls.clear()
    ycode_setup._taskkill("ycode.exe", lambda m: None)
    assert calls == [["taskkill", "/F", "/IM", "ycode.exe"]]


def test_is_file_occupied_distinguishes_permission_from_sharing(tmp_path, monkeypatch):
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    target = install_dir / "core.dll"
    target.write_bytes(b"x")
    real_open = os.open

    def _denied(path, flags, *args, **kwargs):
        if flags & os.O_RDWR:
            raise PermissionError(13, "拒绝访问。", None, 5)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ycode_setup.os, "open", _denied)
    assert ycode_setup._is_file_occupied(str(target)) is False

    def _sharing(path, flags, *args, **kwargs):
        raise PermissionError(13, "另一个程序正在使用此文件。", None, 32)

    monkeypatch.setattr(ycode_setup.os, "open", _sharing)
    assert ycode_setup._is_file_occupied(str(target)) is True


def test_probe_locked_payload_files_ignores_readonly_file(tmp_path):
    if os.name != "nt":
        pytest.skip("只读属性语义仅 Windows 相关")
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    target = install_dir / "ro.pyd"
    target.write_bytes(b"pyd")
    os.chmod(target, 0o444)
    try:
        assert ycode_setup._probe_locked_payload_files(str(install_dir)) == []
    finally:
        os.chmod(target, 0o666)


def _wizard_stub(**attrs):
    import queue as _queue
    wizard = ycode_setup.SetupWizard.__new__(ycode_setup.SetupWizard)
    wizard._ui_queue = _queue.Queue()
    for key, value in attrs.items():
        setattr(wizard, key, value)
    return wizard


def test_drain_ui_queue_survives_handler_exception(monkeypatch):
    wizard = _wizard_stub()
    handled = []

    def _append_log(msg):
        if msg == "boom":
            raise RuntimeError("widget 已销毁")
        handled.append(msg)

    wizard._append_log = _append_log
    after_calls = []
    wizard.after = lambda ms, fn: after_calls.append((ms, fn))

    wizard._ui_queue.put(("log", "boom"))
    wizard._ui_queue.put(("log", "ok"))
    wizard._drain_ui_queue()

    assert handled == ["ok"]
    assert after_calls


def test_ask_occupancy_times_out_when_ui_never_answers(monkeypatch):
    monkeypatch.setattr(ycode_setup, "_OCCUPANCY_ANSWER_TIMEOUT_SECONDS", 0.05)
    wizard = _wizard_stub()
    assert wizard._ask_occupancy(["C:\\x\\Y-code.exe"]) == "continue"


def test_on_window_close_refuses_while_installing(monkeypatch):
    wizard = _wizard_stub(_current_page="installing")
    events = []
    wizard._maybe_schedule_installer_cleanup = lambda: events.append("cleanup")
    wizard.destroy = lambda: events.append("destroy")
    monkeypatch.setattr(ycode_setup.messagebox, "showinfo",
                        lambda *a, **k: events.append("info"))

    wizard._on_window_close()
    assert events == ["info"]

    events.clear()
    wizard._current_page = "finish"
    wizard._on_window_close()
    assert events == ["cleanup", "destroy"]
