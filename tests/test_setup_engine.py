import os
import subprocess
import zipfile
import ctypes
from types import SimpleNamespace

import pytest

import ycode_setup
from ycode_setup import InstallError, InstallOptions, run_install


def _fake_bundle(tmp_path, as_zip=False):
    """构造一个最小化的安装包目录结构。as_zip=True 时载荷为单文件压缩包。"""
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
    # 未选 CLI：ycode.exe 被移除，不写 PATH、不建快捷方式
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
    # CLI 独立安装不携带 GUI 前端资源
    assert not (tmp_path / "installed_cli" / "_internal" / "frontend").exists()
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
    # 即使安装包内置了带占位符厂商的示例配置，播种结果也必须是干净空配置，
    # 不能把「Agnes AI / 深度求索」等假厂商（占位符 key）塞给新用户。
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


# ── 浏览目录自动追加 Y-code 子目录 ─────────────────────────────

def test_normalize_browsed_install_dir_appends_ycode_subdir():
    """浏览选中的目录自动追加 Y-code 子目录：选盘符根目录不再平铺进根目录。"""
    assert ycode_setup.normalize_browsed_install_dir("D:\\") == "D:\\Y-code"
    assert ycode_setup.normalize_browsed_install_dir("D:") == "D:\\Y-code"
    assert ycode_setup.normalize_browsed_install_dir("E:/") == "E:\\Y-code"
    assert ycode_setup.normalize_browsed_install_dir("D:\\MyApps") == "D:\\MyApps\\Y-code"
    assert ycode_setup.normalize_browsed_install_dir("D:\\tools\\") == "D:\\tools\\Y-code"


def test_normalize_browsed_install_dir_keeps_ycode_named_dir():
    """所选目录本身已叫 Y-code（大小写不敏感）时不重复追加。"""
    assert ycode_setup.normalize_browsed_install_dir("D:\\Y-code") == "D:\\Y-code"
    assert ycode_setup.normalize_browsed_install_dir("D:\\y-code") == "D:\\y-code"
    assert ycode_setup.normalize_browsed_install_dir("D:\\y-CODE\\") == "D:\\y-CODE"


# ── 审计修复回归：T21 / M46 / M47 ─────────────────────────────

def test_parse_elevated_args_cli_10_is_not_true():
    """T21：--cli=10 不得被误判为 True（旧实现 endswith("1") 的误判）。"""
    opts = ycode_setup._parse_elevated_args(
        ["--elevated", "--dir=D:\\Y-code", "--cli=10", "--shortcut=10"])
    assert opts["cli"] is False
    assert opts["shortcut"] is False
    opts = ycode_setup._parse_elevated_args(
        ["--elevated", "--dir=D:\\Y-code", "--cli=1", "--shortcut=1"])
    assert opts["cli"] is True
    assert opts["shortcut"] is True


def test_copy_payload_rejects_symlink_target_file(tmp_path, monkeypatch):
    """M46：目标文件自身是符号链接/挂载点时拒绝写入（目录链检查覆盖不到）。"""
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
    """M46：安装中途失败时回滚本次新创建的文件，不留半成品。"""
    bundle = _fake_bundle(tmp_path, as_zip=True)
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    # 让 Y-code.exe 的写入失败：预先创建同名目录，open(..., "wb") 会抛 IsADirectoryError
    (install_dir / "Y-code.exe").mkdir()

    with pytest.raises(Exception):
        ycode_setup._copy_payload(
            "zip", str(bundle / "ycode_pkg.zip"), str(install_dir),
            cli_only=False, log=lambda m: None, progress=lambda p: None,
        )

    # ycode.exe 是本次新写入的文件，应被回滚删除；预置目录不动
    assert not (install_dir / "ycode.exe").exists()
    assert (install_dir / "Y-code.exe").is_dir()


def test_overwrite_install_prunes_files_removed_in_new_version(tmp_path, patched_env):
    """M46：覆盖安装时按上次清单清理新版已移除的旧文件。"""
    bundle = _fake_bundle(tmp_path)
    install_dir = str(tmp_path / "installed")
    run_install("full", str(bundle),
                InstallOptions(install_dir, install_cli=False, create_desktop_shortcut=False),
                _startupinfo(), log=lambda m: None)
    assert (tmp_path / "installed" / "_internal" / "core.dll").exists()

    # 第二版载荷移除了 _internal/core.dll
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
    """M47：非空且无 Y-code 痕迹的目录才需要警告。"""
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
