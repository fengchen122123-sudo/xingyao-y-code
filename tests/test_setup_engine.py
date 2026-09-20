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


def test_desktop_shortcut_uses_new_brand_name_and_removes_legacy(tmp_path, patched_env):
    """桌面快捷方式必须命名为 星瑶code.lnk，且升级时清掉旧版遗留的 Y-code.lnk。"""
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
    # 快捷方式落点必须是新品牌名（create_shortcut 第 1 个位置参数是 lnk 路径）
    assert len(patched_env["shortcut"]) == 1
    lnk_path = patched_env["shortcut"][0][0]
    assert lnk_path == os.path.join(str(desktop), "星瑶code.lnk")
    assert lnk_path.endswith("星瑶code.lnk")
    # 旧版遗留的 Y-code.lnk 已被清理
    assert not legacy.exists()


def test_desktop_shortcut_legacy_cleanup_failure_does_not_abort(tmp_path, patched_env,
                                                               monkeypatch):
    """清理旧 Y-code.lnk 失败（锁定/占用）时只记日志，安装仍要继续完成。"""
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

    # 未因清理失败而中断：仍创建了新品牌快捷方式
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


def test_launch_gui_app_direct_when_not_admin(tmp_path, monkeypatch):
    """普通权限安装完成后直接启动 Y-code.exe。"""
    (tmp_path / "Y-code.exe").write_text("gui", encoding="utf-8")
    calls = []
    monkeypatch.setattr(ycode_setup, "is_user_an_admin", lambda: False)
    monkeypatch.setattr(ycode_setup.subprocess, "Popen",
                        lambda args, **kw: calls.append((args, kw)))
    assert ycode_setup.launch_gui_app(str(tmp_path), _startupinfo()) is True
    assert calls[0][0] == [str(tmp_path / "Y-code.exe")]


def test_launch_gui_app_de_elevates_via_explorer_when_admin(tmp_path, monkeypatch):
    """管理员身份（为所有用户安装场景）时经 explorer.exe 中转降权启动。"""
    (tmp_path / "Y-code.exe").write_text("gui", encoding="utf-8")
    calls = []
    monkeypatch.setattr(ycode_setup, "is_user_an_admin", lambda: True)
    monkeypatch.setattr(ycode_setup.subprocess, "Popen",
                        lambda args, **kw: calls.append((args, kw)))
    assert ycode_setup.launch_gui_app(str(tmp_path), _startupinfo()) is True
    assert calls[0][0][0] == "explorer.exe"
    assert calls[0][0][1] == str(tmp_path / "Y-code.exe")


def test_launch_gui_app_missing_exe_returns_false(tmp_path):
    """安装目录下没有 Y-code.exe 时不启动，返回 False。"""
    assert ycode_setup.launch_gui_app(str(tmp_path), _startupinfo()) is False


def test_launch_gui_app_popen_failure_returns_false(tmp_path, monkeypatch):
    """启动被拦截（OSError）时不抛异常，静默返回 False。"""
    (tmp_path / "Y-code.exe").write_text("gui", encoding="utf-8")
    monkeypatch.setattr(ycode_setup, "is_user_an_admin", lambda: False)

    def _boom(*a, **k):
        raise OSError("blocked")

    monkeypatch.setattr(ycode_setup.subprocess, "Popen", _boom)
    assert ycode_setup.launch_gui_app(str(tmp_path), _startupinfo()) is False


# ── 载荷释放的"失败不破坏已有安装"与"被占用文件改名让位" ────────────

def test_copy_payload_failure_preserves_existing_install(tmp_path):
    """复制中途失败：不破坏已有安装——旧文件字节不变、无 .ycode-bak 残留、
    本次新建的文件被回滚、旧版独有文件（本应被 prune 掉的）仍然存在。"""
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    (install_dir / "_internal" / "core.dll").write_text("old-core", encoding="utf-8")
    (install_dir / "_internal" / "legacy.dll").write_text("old-only", encoding="utf-8")
    # 旧清单记录了 legacy.dll，而新版载荷已移除它 → 只有在复制全部成功后才会被清理
    (install_dir / "_internal" / "ycode_install_manifest.txt").write_text(
        "ycode.exe\nY-code.exe\n_internal/core.dll\n_internal/legacy.dll\n",
        encoding="utf-8")
    # 让 ycode.exe 的写入失败：预置同名目录，写入抛 PermissionError
    (install_dir / "ycode.exe").mkdir()

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    zip_path = bundle / "ycode_pkg.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        # 已存在的 core.dll 必须放在失败点【之前】：先让位后被新版覆盖，失败时
        # 必须按字节回滚成旧内容——这是本轮修复的核心，旧实现会留下半新半旧。
        zf.writestr("_internal/core.dll", "new-core")
        # 再写入一个本次新建的文件，用于验证新建文件回滚
        zf.writestr("_internal/frontend/index.html", "new-html")
        zf.writestr("ycode.exe", "new-cli")
        zf.writestr("Y-code.exe", "new-gui")

    with pytest.raises(InstallError, match="ycode.exe"):
        ycode_setup._copy_payload(
            "zip", str(zip_path), str(install_dir),
            cli_only=False, log=lambda m: None, progress=lambda p: None,
        )

    # 旧文件字节不变（未被半途覆盖）
    assert (install_dir / "_internal" / "core.dll").read_text(encoding="utf-8") == "old-core"
    # 旧版独有文件仍在（prune 只在复制全部成功后才执行）
    assert (install_dir / "_internal" / "legacy.dll").read_text(encoding="utf-8") == "old-only"
    # 预置目录不动，本次新建的文件被回滚
    assert (install_dir / "ycode.exe").is_dir()
    assert not (install_dir / "_internal" / "frontend" / "index.html").exists()
    # 没有让位备份 / 重启替换临时文件残留
    leftovers = [p.name for p in install_dir.rglob("*")
                 if p.name.endswith((".ycode-bak", ".ycode-new"))]
    assert leftovers == []


def test_copy_payload_renames_aside_locked_file(tmp_path, monkeypatch):
    """已存在文件原地写入抛 PermissionError 时：改名让位后新内容正确就位、
    .ycode-bak 被清理，且不抛异常（无需管理员权限）。"""
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    target = install_dir / "Y-code.exe"
    target.write_text("old-gui", encoding="utf-8")

    bundle = _fake_bundle(tmp_path, as_zip=True)
    target_norm = os.path.normcase(os.path.abspath(str(target)))
    real_open = open

    def _locked_open(path, mode="r", *args, **kwargs):
        # 模拟"正在运行的 exe 被占用"：只要旧文件还在原地，原地覆盖写就抛
        # PermissionError；改名让位后路径不存在，写入即可成功。
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
    """prune 时机：复制失败时旧版独有文件不动；复制全部成功后才清理。"""
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    legacy = install_dir / "_internal" / "legacy.dll"
    legacy.write_text("old-only", encoding="utf-8")
    manifest = install_dir / "_internal" / "ycode_install_manifest.txt"
    manifest.write_text("ycode.exe\n_internal/legacy.dll\n", encoding="utf-8")

    # 阶段一：载荷写入失败（ycode.exe 被同名目录占位）→ 旧版独有文件必须保留
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

    # 阶段二：排除失败点后复制成功 → 旧版独有文件被清理，清单同步更新
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
    """先让位、后写入：已存在文件成功让位并被新版覆盖后，若后续文件写入失败，
    必须按字节恢复旧内容，且不留 .ycode-bak / .ycode-new 残留。

    这是本轮修复的核心回归：旧实现下 core.dll 已被新版覆盖且不在任何回滚集合里，
    失败后目录半新半旧（前端与 assets 哈希不配套即白屏）。"""
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    core = install_dir / "_internal" / "core.dll"
    core.write_bytes(b"OLD-CORE-BYTES")
    # 失败点：Y-code.exe 被同名目录占位，写入抛 PermissionError
    (install_dir / "Y-code.exe").mkdir()

    zip_path = tmp_path / "payload.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("_internal/core.dll", "NEW-CORE-BYTES")  # 先成功让位并覆盖
        zf.writestr("Y-code.exe", "new-gui")                 # 再触发失败

    with pytest.raises(InstallError):
        ycode_setup._copy_payload(
            "zip", str(zip_path), str(install_dir),
            cli_only=False, log=lambda m: None, progress=lambda p: None,
        )

    # 旧文件字节被完整恢复（未停留在被新版覆盖的半新半旧状态）
    assert core.read_bytes() == b"OLD-CORE-BYTES"
    # 无让位备份 / 重启替换临时文件残留
    leftovers = [p.name for p in install_dir.rglob("*")
                 if p.name.endswith((".ycode-bak", ".ycode-new"))]
    assert leftovers == []


def test_copy_payload_restores_orphan_bak_before_writing(tmp_path):
    """中断自愈：遗留 bak 且目标缺失（上次改名到一半被强杀/断电）时，再次调用
    _copy_payload 必须先把 bak 改回原名恢复出旧文件——那是唯一的一份副本。"""
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    orphan = install_dir / "_internal" / "core.dll.ycode-bak"
    orphan.write_bytes(b"OLD-CORE")
    assert not (install_dir / "_internal" / "core.dll").exists()

    zip_path = tmp_path / "payload.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("ycode.exe", "cli")  # 载荷不含 core.dll，便于观察自愈结果

    ycode_setup._copy_payload(
        "zip", str(zip_path), str(install_dir),
        cli_only=False, log=lambda m: None, progress=lambda p: None,
    )

    assert (install_dir / "_internal" / "core.dll").read_bytes() == b"OLD-CORE"
    assert not orphan.exists()


def test_copy_payload_removes_redundant_bak_when_target_present(tmp_path):
    """目标与 bak 同时存在（上次写完新文件但没删掉备份）→ bak 是冗余备份，
    被尽力清理，且绝不反过来覆盖已就位的目标。"""
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
    """让位失败（os.replace 抛 OSError，模拟被以无 FILE_SHARE_DELETE 方式打开的
    文件）时，回退到三级兜底（直接写）仍能成功写入，不因单个文件让位失败而中止。"""
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

    # 让位失败已回退到兜底路径：新内容正确就位，无备份残留
    assert target.read_bytes() == b"gui"
    assert not (install_dir / "Y-code.exe.ycode-bak").exists()
    assert any("改名让位失败" in m for m in logs)


# ── 所有用户安装：Program Files 判定的唯一真值（向导即时校验与引擎共用） ──

def _patch_program_files(monkeypatch, pf):
    monkeypatch.setenv("ProgramFiles", str(pf))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)


def test_is_under_program_files_accepts_subdir(tmp_path, monkeypatch):
    pf = tmp_path / "Program Files"
    _patch_program_files(monkeypatch, pf)
    assert ycode_setup._is_under_program_files(str(pf / "Y-code")) is True


def test_is_under_program_files_rejects_root_and_trailing_slash(tmp_path, monkeypatch):
    """Program Files 根目录本身不是合法子目录（尾随反斜杠归一化后同样拒绝）。"""
    pf = tmp_path / "Program Files"
    _patch_program_files(monkeypatch, pf)
    assert ycode_setup._is_under_program_files(str(pf)) is False
    assert ycode_setup._is_under_program_files(str(pf) + "\\") is False


def test_is_under_program_files_case_insensitive(tmp_path, monkeypatch):
    """大小写不敏感：realpath + normcase 归一化后仍判为子目录。"""
    pf = tmp_path / "Program Files"
    _patch_program_files(monkeypatch, pf)
    assert ycode_setup._is_under_program_files(str(pf / "Y-CODE").upper()) is True


def test_is_under_program_files_rejects_unrelated_drive(tmp_path, monkeypatch):
    pf = tmp_path / "Program Files"
    _patch_program_files(monkeypatch, pf)
    assert ycode_setup._is_under_program_files(str(tmp_path / "other" / "Y-code")) is False


def test_is_under_program_files_matches_engine_validation(tmp_path, monkeypatch):
    """同一组输入下，向导共用的 _is_under_program_files 与引擎 _validate_install_target
    必须给出相同结论——两处判定漂移正是"界面说合法、安装却失败"的根因。"""
    pf = tmp_path / "Program Files"
    _patch_program_files(monkeypatch, pf)
    cases = [
        str(pf / "Y-code"),            # 合法子目录
        str(pf / "Y-CODE") + "\\",     # 大小写 + 尾随反斜杠
        str(pf),                       # Program Files 根目录本身
        str(tmp_path / "elsewhere"),   # 完全不相干
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
    """不依赖 Tk 的字符串变量替身，用于在无窗口环境下测试 _on_scope_change。"""

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
    wizard._dir_hint = None       # 让 _refresh_dir_validation 直接返回，避免触碰 Tk 控件
    wizard._dir_invalid = False
    wizard._dir_note = ""
    wizard.var_scope = _FakeVar(scope)
    wizard.var_dir = _FakeVar(cur_dir)
    return wizard


def test_on_scope_change_corrects_manual_path_to_machine_default(tmp_path, monkeypatch):
    """用户手改过路径后再切"所有用户"：当场纠正为 machine 默认路径并提示已自动改，
    而不是等到安装阶段才由引擎抛"安装失败"。"""
    wizard = _scope_wizard(tmp_path, monkeypatch, "machine",
                           str(tmp_path / "custom" / "Y-code"))
    wizard._on_scope_change()
    assert wizard.var_dir.get() == ycode_setup.default_install_dir("machine")
    assert "已自动改为" in wizard._dir_note


def test_on_scope_change_keeps_valid_machine_path(tmp_path, monkeypatch):
    """切"所有用户"时路径已在 Program Files 下：保持不变，无纠正提示。"""
    valid = str(tmp_path / "Program Files" / "Y-code")
    wizard = _scope_wizard(tmp_path, monkeypatch, "machine", valid)
    wizard._on_scope_change()
    assert wizard.var_dir.get() == valid
    assert wizard._dir_note == ""


def test_on_scope_change_back_to_user_restores_user_default(tmp_path, monkeypatch):
    """切回"仅为我安装"时路径仍等于 machine 默认值：改回 user 默认路径。"""
    wizard = _scope_wizard(tmp_path, monkeypatch, "user", "")
    # 在环境隔离生效后再取 machine 默认路径（否则会读到真实 ProgramFiles）
    wizard.var_dir.set(ycode_setup.default_install_dir("machine"))
    wizard._on_scope_change()
    assert wizard.var_dir.get() == ycode_setup.default_install_dir("user")


def test_on_scope_change_locked_install_keeps_path(tmp_path, monkeypatch):
    """重装锁定路径时行为保持现状：范围切换不改写安装路径。"""
    old = str(tmp_path / "old-install")
    wizard = _scope_wizard(tmp_path, monkeypatch, "machine", old, previous=old)
    wizard._on_scope_change()
    assert wizard.var_dir.get() == old


# ── 安装前占用预检 + 可自动结束进程 ─────────────────────────────────

def test_probe_locked_payload_files_detects_locked_exe(tmp_path, monkeypatch):
    """os.open(O_RDWR) 抛 PermissionError 即视为被占用，只返回 exe/dll/pyd。"""
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
    """检测无副作用：不改变文件内容与 mtime。"""
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    exe = install_dir / "Y-code.exe"
    exe.write_bytes(b"gui")
    before = exe.stat().st_mtime_ns
    assert ycode_setup._probe_locked_payload_files(str(install_dir)) == []
    assert exe.read_bytes() == b"gui"
    assert exe.stat().st_mtime_ns == before


def test_resolve_occupied_files_retries_after_kill(tmp_path, monkeypatch):
    """选①结束进程后重新检测：占用消失则继续；taskkill 用列表参数（非 shell 字符串）。"""
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    target = install_dir / "Y-code.exe"
    target.write_bytes(b"gui")

    probes = {"n": 0}

    def _fake_probe(_dir):
        probes["n"] += 1
        # 第一次报告被占用；结束进程后（第二次起）视为已解除
        return [str(target)] if probes["n"] == 1 else []

    monkeypatch.setattr(ycode_setup, "_probe_locked_payload_files", _fake_probe)
    # 结束进程走"按 PID"这条路：筛进程要读真实进程表，测试里直接给出结果
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
    assert probes["n"] >= 2  # 结束后确实重新检测
    args_seen = [c[0] for c in calls]
    assert ["taskkill", "/F", "/PID", "4242"] in args_seen
    # 不能按镜像名结束（会误杀安装目录之外的实例）
    assert all("/IM" not in a for a in args_seen)
    # 必须是列表参数，不能是 shell 字符串
    assert all(isinstance(a, list) for a in args_seen)


def test_resolve_occupied_files_continue_choice_keeps_locked(tmp_path, monkeypatch):
    """选③仍要继续：直接返回仍被占用的列表，不调用 taskkill。"""
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
    """非交互（ask=None）绝不弹窗/阻塞：只记日志并继续，保持无人值守安装可用。"""
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
    """cli 模式默认非交互：即便传入 ask 回调也不弹窗，安装照常完成。"""
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


# ── 让位失败时的复制备份兜底（避免半新半旧） ───────────────────────

def test_make_way_copy_backup_restores_old_bytes_on_failure(tmp_path, monkeypatch):
    """让位（os.replace）失败但文件可读：复制备份后，若后续文件写入失败，
    该文件仍能按字节恢复旧内容，不留半新半旧目录。"""
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    core = install_dir / "_internal" / "core.dll"
    core.write_bytes(b"OLD-CORE-BYTES")
    (install_dir / "Y-code.exe").mkdir()  # 失败点：同名目录使写入抛错

    def _boom_replace(*args, **kwargs):
        raise OSError("rename blocked (no FILE_SHARE_DELETE)")

    monkeypatch.setattr(ycode_setup.os, "replace", _boom_replace)

    zip_path = tmp_path / "payload.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("_internal/core.dll", "NEW-CORE-BYTES")  # 先就地覆盖（有复制备份）
        zf.writestr("Y-code.exe", "new-gui")                 # 再触发失败

    with pytest.raises(InstallError):
        ycode_setup._copy_payload(
            "zip", str(zip_path), str(install_dir),
            cli_only=False, log=lambda m: None, progress=lambda p: None,
        )

    # 旧文件字节被完整恢复（未停留在被新版覆盖的半新半旧状态）
    assert core.read_bytes() == b"OLD-CORE-BYTES"
    leftovers = [p.name for p in install_dir.rglob("*")
                 if p.name.endswith((".ycode-bak", ".ycode-new"))]
    assert leftovers == []


def test_make_way_backup_failure_keeps_current_behavior(tmp_path, monkeypatch):
    """让位与复制备份都失败时退回现状：不中止安装，就地覆盖，只记日志。"""
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

    # 退回现状：新内容就地就位，安装未中止，无备份残留
    assert target.read_bytes() == b"gui"
    assert any("复制备份失败" in m for m in logs)
    assert not (install_dir / "Y-code.exe.ycode-bak").exists()


def test_copy_payload_clears_stale_reboot_tmp(tmp_path, monkeypatch):
    """上次遗留的待重启替换文件（*.ycode-new）必须在本次成功写入目标后清理。

    否则下次重启会用它（旧内容）覆盖掉本次写入的新版本——命中的若是前端资源，
    表现就是"更新后打开白屏"。同时绝不能误删本次自己产生的 .ycode-new。
    """
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    core = install_dir / "_internal" / "core.dll"
    core.write_bytes(b"OLD-CORE")
    # 上次安装登记过重启替换、用户却没重启，留下的陈旧 tmp（内容是更早的版本）
    stale = install_dir / "_internal" / "core.dll.ycode-new"
    stale.write_bytes(b"ANCIENT-CORE")
    # 本次会走 reboot 分支的文件：它自己产生的 tmp 不能被当成陈旧文件删掉
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
    # 让位与复制备份都失败 → GUI 只能走 reboot 分支；并让"登记重启替换"成功
    # （模拟管理员），这样安装不会中止，成功路径的清理逻辑才会被执行。
    monkeypatch.setattr(ycode_setup, "_rename_aside", lambda *a, **k: None)
    monkeypatch.setattr(ycode_setup.shutil, "copy2", _blocked)
    monkeypatch.setattr(ycode_setup, "_schedule_reboot_replace", lambda *a, **k: None)

    ycode_setup._copy_payload(
        "zip", str(bundle / "ycode_pkg.zip"), str(install_dir),
        cli_only=False, log=lambda m: None, progress=lambda p: None,
    )

    # 陈旧 tmp 被清理，目标已是本次的新版本
    assert not stale.exists()
    assert core.read_bytes() == b"dll"
    # 本次 reboot 分支自己产生的 tmp 保留（正等着重启替换）
    assert (install_dir / "Y-code.exe.ycode-new").exists()


def test_copy_payload_keeps_old_file_for_reboot_dest(tmp_path, monkeypatch):
    """命中 reboot 分支的 dest：成功路径必须把让位备份恢复回原名。

    它的新内容要等重启才生效，此时若删掉备份，目标文件在重启前会整段缺失——
    Y-code.exe 直接消失，用户连旧版都打不开。
    """
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    gui = install_dir / "Y-code.exe"
    gui.write_bytes(b"OLD-GUI")

    bundle = _fake_bundle(tmp_path, as_zip=True)
    real_open = open
    gui_norm = os.path.normcase(os.path.abspath(str(gui)))

    def _locked_open(path, mode="r", *args, **kwargs):
        # 无论目标在不在，对它的写一律拒绝：让位后仍写不进去 → 只能走 reboot 分支
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

    # 旧版本仍在原位（重启前可用），备份已归位，新内容在等重启
    assert gui.read_bytes() == b"OLD-GUI"
    assert not (install_dir / "Y-code.exe.ycode-bak").exists()
    assert (install_dir / "Y-code.exe.ycode-new").exists()
    assert any("将在系统重启后更新" in m for m in logs)
    # 其余文件照常更新
    assert (install_dir / "_internal" / "core.dll").read_bytes() == b"dll"


def test_rename_aside_keeps_registered_backup(tmp_path):
    """已登记进回滚集合的备份不能被让位逻辑删掉。

    删了它，回滚时就恢复不出旧字节，还会把其实从未被修改的文件误报成"未能恢复"。
    """
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    dest = install_dir / "core.dll"
    dest.write_bytes(b"OLD")
    bak = install_dir / "core.dll.ycode-bak"
    bak.write_bytes(b"OLD")
    registered = [(str(bak), str(dest))]

    assert ycode_setup._rename_aside(str(dest), lambda m: None, registered) is None
    # 备份仍在、目标没被搬走（放弃让位，但回滚能力完好）
    assert bak.read_bytes() == b"OLD"
    assert dest.read_bytes() == b"OLD"

    # 对照：未登记的陈旧备份仍按原行为清理，并完成让位
    other = install_dir / "gui.exe"
    other.write_bytes(b"OLD2")
    stale = install_dir / "gui.exe.ycode-bak"
    stale.write_bytes(b"STALE")
    assert ycode_setup._rename_aside(str(other), lambda m: None, []) == str(stale)
    assert stale.read_bytes() == b"OLD2"


def test_copy_payload_does_not_report_unmodified_file_as_unrestored(tmp_path, monkeypatch):
    """让位失败、只剩复制备份时，回滚必须仍能恢复，且不得把它误报成"未能恢复"。"""
    install_dir = tmp_path / "installed"
    (install_dir / "_internal").mkdir(parents=True)
    core = install_dir / "_internal" / "core.dll"
    core.write_bytes(b"OLD-CORE")
    # 注意：失败点必须落在 core.dll 自己身上（载荷顺序为 ycode.exe → Y-code.exe →
    # core.dll → index.html）。若在更早的文件上制造失败，core.dll 根本不会被处理，
    # 这条用例就会退化成"什么都没发生也通过"。

    bundle = _fake_bundle(tmp_path, as_zip=True)
    real_open = open
    real_replace = os.replace
    core_norm = os.path.normcase(os.path.abspath(str(core)))

    def _locked_open(path, mode="r", *args, **kwargs):
        # 只拒绝"写 core.dll"：让位失败后原地覆盖写也失败
        if (os.path.normcase(os.path.abspath(str(path))) == core_norm
                and "w" in str(mode)):
            raise PermissionError(13, "locked by running process")
        return real_open(path, mode, *args, **kwargs)

    def _replace(src, dst, *args, **kwargs):
        # 只让"core.dll → 备份"这一步失败（模拟无 FILE_SHARE_DELETE 的占用），
        # 恢复方向（备份 → core.dll）保持可用。
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
    # 旧字节被复制备份救回来，且没有被误报为"未能恢复"
    assert core.read_bytes() == b"OLD-CORE"
    assert "未能恢复" not in message
    leftovers = [p.name for p in install_dir.rglob("*")
                 if p.name.endswith((".ycode-bak", ".ycode-new"))]
    assert leftovers == []


def test_iter_own_process_pids_filters_by_install_dir(tmp_path, monkeypatch):
    """结束进程前必须按可执行文件路径筛选：安装目录之外的实例不能被列入。"""
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
    """有 install_dir 时按 PID 结束本目录内的进程；没有时保留按镜像名的兼容行为。"""
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

    # 没有 install_dir（旧调用方）时退化为按镜像名结束
    calls.clear()
    ycode_setup._taskkill("ycode.exe", lambda m: None)
    assert calls == [["taskkill", "/F", "/IM", "ycode.exe"]]


def test_is_file_occupied_distinguishes_permission_from_sharing(tmp_path, monkeypatch):
    """共享冲突才算被占用；ACCESS_DENIED 但只读能打开的不算（只读属性不是占用）。"""
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
    """真实只读文件（非 monkeypatch）：只读属性不等于被占用，不能报给用户去结束进程。"""
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
    """构造不触碰 Tk 的向导替身（只填测试需要的那几个属性）。"""
    import queue as _queue
    wizard = ycode_setup.SetupWizard.__new__(ycode_setup.SetupWizard)
    wizard._ui_queue = _queue.Queue()
    for key, value in attrs.items():
        setattr(wizard, key, value)
    return wizard


def test_drain_ui_queue_survives_handler_exception(monkeypatch):
    """单条消息处理失败不能让消息泵停摆：后续消息照常处理，after 仍然重挂。"""
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

    assert handled == ["ok"]           # 失败那条被跳过，后面的仍然处理
    assert after_calls                 # 泵没有死掉


def test_ask_occupancy_times_out_when_ui_never_answers(monkeypatch):
    """主线程不再消费队列时必须超时返回，而不是永久阻塞安装线程。"""
    monkeypatch.setattr(ycode_setup, "_OCCUPANCY_ANSWER_TIMEOUT_SECONDS", 0.05)
    wizard = _wizard_stub()
    # 队列里的询问没人处理（模拟消息泵已停摆 / 窗口被销毁）
    assert wizard._ask_occupancy(["C:\\x\\Y-code.exe"]) == "continue"


def test_on_window_close_refuses_while_installing(monkeypatch):
    """安装进行中拒绝关闭窗口：销毁窗口会把安装线程硬杀，回滚来不及执行。"""
    wizard = _wizard_stub(_current_page="installing")
    events = []
    wizard._maybe_schedule_installer_cleanup = lambda: events.append("cleanup")
    wizard.destroy = lambda: events.append("destroy")
    monkeypatch.setattr(ycode_setup.messagebox, "showinfo",
                        lambda *a, **k: events.append("info"))

    wizard._on_window_close()
    assert events == ["info"]          # 只提示，不销毁

    events.clear()
    wizard._current_page = "finish"
    wizard._on_window_close()
    assert events == ["cleanup", "destroy"]
