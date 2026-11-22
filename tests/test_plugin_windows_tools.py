# -*- coding: utf-8 -*-

import ast
import importlib.util
import json
import os
import sys
import types

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_DIR = os.path.join(_PROJECT_ROOT, "plugins", "windows_tools")

_EXPECTED_ORDER = [
    "windows_process_list",
    "windows_process_kill",
    "windows_clipboard_get",
    "windows_clipboard_set",
    "windows_registry",
    "windows_notify",
]

_READ_ONLY_TOOLS = ("windows_process_list", "windows_clipboard_get", "windows_registry")
_WRITE_TOOLS = ("windows_process_kill", "windows_clipboard_set")
_PERMISSIONLESS_TOOLS = tuple(_READ_ONLY_TOOLS) + ("windows_notify",)

_FORBIDDEN_TOP_LEVEL_IMPORTS = {"psutil", "win32api", "win32gui", "winreg", "comtypes", "ctypes", "wmi"}


def _load_plugin_module():
    entry = os.path.join(_PLUGIN_DIR, "plugin.py")
    module_spec = importlib.util.spec_from_file_location("_test_windows_tools_plugin", entry)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def _registry_names(registry):
    return [spec.name for spec in registry.all()]


def _register(module):
    from core.tool_registry import ToolRegistry

    registry = ToolRegistry()
    module.register(registry)
    return registry


def _properties(spec):
    return set(spec.input_schema.get("properties", {}))


@pytest.fixture
def win_env(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("YCODE_TOOL_PROFILE", raising=False)
    return _load_plugin_module()


def test_plugin_json_metadata():
    with open(os.path.join(_PLUGIN_DIR, "plugin.json"), encoding="utf-8") as handle:
        meta = json.load(handle)

    assert meta["name"] == "windows_tools"
    assert meta["version"] == "0.1.0"
    assert meta["entry"] == "plugin.py"
    assert meta["enabled"] is True
    assert meta["description"].strip()
    assert any("\u4e00" <= char <= "\u9fff" for char in meta["description"])


def test_module_top_level_imports_are_platform_safe():
    source = open(os.path.join(_PLUGIN_DIR, "plugin.py"), encoding="utf-8").read()
    tree = ast.parse(source)

    top_level = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                top_level.add(node.module.split(".")[0])

    assert top_level <= {"os", "sys", "core", "__future__"}, top_level
    assert not (top_level & _FORBIDDEN_TOP_LEVEL_IMPORTS)


def test_register_is_noop_on_non_windows(monkeypatch):
    module = _load_plugin_module()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("YCODE_TOOL_PROFILE", raising=False)

    assert _registry_names(_register(module)) == []


def test_register_is_noop_in_cli_profile(monkeypatch):
    module = _load_plugin_module()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("YCODE_TOOL_PROFILE", "cli")

    assert _registry_names(_register(module)) == []


def test_register_order_on_windows(win_env):
    names = _registry_names(_register(win_env))

    assert names == _EXPECTED_ORDER


@pytest.mark.parametrize("name", _READ_ONLY_TOOLS)
def test_read_only_tools_do_not_require_permission(win_env, name):
    spec = _register(win_env).get(name)

    assert spec.read_only is True
    assert spec.requires_permission is False
    assert spec.mutates_files is False
    assert spec.side_effect_scope == "none"


@pytest.mark.parametrize("name", _WRITE_TOOLS)
def test_write_tools_require_permission(win_env, name):
    spec = _register(win_env).get(name)

    assert spec.read_only is False
    assert spec.requires_permission is True
    assert spec.side_effect_scope == "system"


def test_notify_does_not_require_permission_but_declares_blocking(win_env):
    spec = _register(win_env).get("windows_notify")

    assert spec.requires_permission is False
    assert spec.side_effect_scope == "none"
    # 描述必须提前告知「阻塞到点确定」，否则模型会用它播报进度。
    assert "阻塞" in spec.description and "确定" in spec.description


@pytest.mark.parametrize("name", _EXPECTED_ORDER)
def test_every_spec_has_chinese_activity_description(win_env, name):
    spec = _register(win_env).get(name)

    assert spec.activity_description.strip()
    assert any("\u4e00" <= char <= "\u9fff" for char in spec.activity_description)


def test_split_tools_only_declare_their_own_fields(win_env):
    registry = _register(win_env)

    assert _properties(registry.get("windows_process_list")) == {"filter", "sort_by", "limit"}
    assert _properties(registry.get("windows_process_kill")) == {"pid", "name", "force"}
    assert _properties(registry.get("windows_clipboard_get")) == set()
    assert _properties(registry.get("windows_clipboard_set")) == {"text"}
    assert registry.get("windows_clipboard_set").input_schema.get("required") == ["text"]

    for name in (
        "windows_process_list",
        "windows_process_kill",
        "windows_clipboard_get",
        "windows_clipboard_set",
    ):
        assert "mode" not in _properties(registry.get(name)), name
    assert _properties(registry.get("windows_registry")) == {"mode", "path", "name", "view"}


class _FakeProcess:

    def __init__(self, pid, name, memory_mb=10.0, cpu=1.0):
        self.pid = pid
        self.cpu = cpu
        self.info = {
            "pid": pid,
            "name": name,
            "memory_info": types.SimpleNamespace(rss=int(memory_mb * 1024 * 1024)),
        }
        self.terminate_calls = 0
        self.kill_calls = 0

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1

    def cpu_percent(self, interval=None):
        return self.cpu


@pytest.fixture
def fake_processes(monkeypatch):
    psutil = pytest.importorskip("psutil")

    def _install(*items):
        fakes = [
            item if isinstance(item, _FakeProcess) else _FakeProcess(*item)
            for item in items
        ]
        monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: list(fakes))
        return fakes

    return _install


def test_protected_names_cover_critical_system_processes(win_env):
    assert {
        "system",
        "system idle process",
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "services.exe",
        "lsass.exe",
    } <= set(win_env._PROTECTED_NAMES)


@pytest.mark.parametrize(
    "fake_args",
    [
        (1234, "csrss.exe"),
        (4, "System"),
        (0, "System Idle Process"),
        (4004, "lsass.exe"),
    ],
)
def test_kill_refuses_protected_process_by_pid(win_env, fake_processes, fake_args):
    fakes = fake_processes(fake_args)

    result = win_env._process_kill_tool(pid=fake_args[0])

    assert result.is_error is True
    assert result.content.startswith("windows_process_protected")
    assert "受保护" in result.content
    assert fakes[0].terminate_calls == 0
    assert fakes[0].kill_calls == 0


def test_kill_refuses_protected_process_by_name(win_env, fake_processes):
    fakes = fake_processes((1234, "csrss.exe"))

    result = win_env._process_kill_tool(name="csrss.exe", force=True)

    assert result.is_error is True
    assert result.content.startswith("windows_process_protected")
    assert fakes[0].kill_calls == 0


def test_kill_refuses_current_process(win_env, fake_processes):
    fakes = fake_processes((os.getpid(), "python.exe"))

    result = win_env._process_kill_tool(pid=os.getpid())

    assert result.is_error is True
    assert "当前进程" in result.content
    assert fakes[0].terminate_calls == 0


def test_kill_missing_pid_reports_clear_error(win_env, fake_processes):
    fake_processes((2468, "notepad.exe"))

    result = win_env._process_kill_tool(pid=999999)

    assert result.is_error is True
    assert result.content.startswith("windows_process_not_found")
    assert "未找到" in result.content
    assert "Traceback" not in result.content


def test_kill_without_pid_or_name_is_rejected(win_env, fake_processes):
    fake_processes((2468, "notepad.exe"))

    result = win_env._process_kill_tool()

    assert result.is_error is True
    assert "pid 或 name" in result.content


def test_kill_target_process_uses_terminate_by_default(win_env, fake_processes):
    fakes = fake_processes((2468, "notepad.exe"))

    result = win_env._process_kill_tool(name="notepad")

    assert result.is_error is False
    assert "已结束 1 个进程" in result.content
    assert fakes[0].terminate_calls == 1
    assert fakes[0].kill_calls == 0


def test_kill_force_uses_kill(win_env, fake_processes):
    fakes = fake_processes((2468, "notepad.exe"))

    result = win_env._process_kill_tool(pid=2468, force=True)

    assert result.is_error is False
    assert "kill" in result.content
    assert fakes[0].kill_calls == 1
    assert fakes[0].terminate_calls == 0


def test_list_processes_renders_table_and_filter(win_env, fake_processes):
    fakes = fake_processes((100, "chrome.exe", 300.0), (200, "notepad.exe", 20.0))

    result = win_env._process_list_tool()

    assert result.is_error is False
    assert "PID" in result.content and "内存(MB)" in result.content and "名称" in result.content
    assert result.content.index("chrome.exe") < result.content.index("notepad.exe")

    filtered = win_env._process_list_tool(filter="notepad")
    assert "notepad.exe" in filtered.content
    assert "chrome.exe" not in filtered.content
    assert all(fake.kill_calls == 0 for fake in fakes)


def test_list_processes_sorts_by_cpu_and_name(win_env, fake_processes):
    fake_processes((100, "chrome.exe", 300.0, 0.5), (200, "aaa.exe", 1.0, 9.0))

    by_cpu = win_env._process_list_tool(sort_by="cpu")
    assert by_cpu.content.index("aaa.exe") < by_cpu.content.index("chrome.exe")

    by_name = win_env._process_list_tool(sort_by="name")
    assert by_name.content.index("aaa.exe") < by_name.content.index("chrome.exe")


def test_unknown_mode_is_rejected(win_env):
    result = win_env._process_tool(mode="suspend")

    assert result.is_error is True
    assert result.content.startswith("windows_invalid_mode")


def test_clipboard_set_with_empty_text_is_rejected(win_env):
    result = win_env._clipboard_set_tool(text="")

    assert result.is_error is True
    assert "为空" in result.content
    assert result.content.startswith("windows_invalid_arguments")


def test_clipboard_unknown_mode_is_rejected(win_env):
    result = win_env._clipboard_tool(mode="clear")

    assert result.is_error is True
    assert result.content.startswith("windows_invalid_mode")


class _FakeKey:

    def __init__(self, owner):
        self.owner = owner


class _FakeWinreg:

    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CLASSES_ROOT = "HKCR"
    KEY_READ = 0x20019
    KEY_WOW64_64KEY = 0x0100
    KEY_WOW64_32KEY = 0x0200

    def __init__(self, values=None, subkeys=None, error=None):
        self.values = list(values or [])
        self.subkeys = list(subkeys or [])
        self.error = error
        self.open_calls = []
        self.closed = 0

    def OpenKey(self, root, subpath, reserved=0, access=0):
        self.open_calls.append((root, subpath, access))
        if self.error is not None:
            raise self.error
        return _FakeKey(self)

    def QueryValueEx(self, key, name):
        for value_name, value, value_type in self.values:
            if value_name == name:
                return value, value_type
        raise FileNotFoundError(name)

    def EnumValue(self, key, index):
        if index >= len(self.values):
            raise OSError(22, "no more data")
        return self.values[index]

    def EnumKey(self, key, index):
        if index >= len(self.subkeys):
            raise OSError(22, "no more data")
        return self.subkeys[index]

    def CloseKey(self, key):
        self.closed += 1


@pytest.fixture
def fake_winreg(monkeypatch):
    def _install(**kwargs):
        fake = _FakeWinreg(**kwargs)
        monkeypatch.setitem(sys.modules, "winreg", fake)
        return fake

    return _install


def test_registry_set_mode_is_rejected(win_env, fake_winreg):
    fake = fake_winreg()

    result = win_env._registry_tool(
        mode="set", path="HKEY_CURRENT_USER\\Software\\Xingyao", name="Mode"
    )

    assert result.is_error is True
    assert "不提供" in result.content and "写入" in result.content
    # 必须在触达注册表之前就拒绝。
    assert fake.open_calls == []


def test_registry_delete_mode_is_rejected(win_env, fake_winreg):
    fake = fake_winreg()

    result = win_env._registry_tool(mode="delete", path="HKEY_CURRENT_USER\\Software\\Xingyao")

    assert result.is_error is True
    assert "删除" in result.content
    assert fake.open_calls == []


@pytest.mark.parametrize(
    "path",
    [
        "Software\\Microsoft\\Windows",
        "HKLM\\Software",
        "HKEY_USERS\\Software",
    ],
)
def test_registry_relative_path_is_rejected(win_env, fake_winreg, path):
    fake = fake_winreg()

    result = win_env._registry_tool(mode="list", path=path)

    assert result.is_error is True
    assert result.content.startswith("windows_registry_invalid_path")
    assert "HKEY_" in result.content
    assert fake.open_calls == []


@pytest.mark.parametrize(
    "path",
    [
        "HKEY_CURRENT_USER\\Software\\..\\System",
        "HKEY_CURRENT_USER\\Software\\\\.\\Bad",
        "HKEY_CURRENT_USER",
        "HKEY_CURRENT_USER\\",
        "",
    ],
)
def test_registry_path_segments_are_validated(win_env, fake_winreg, path):
    fake = fake_winreg()

    result = win_env._registry_tool(mode="list", path=path)

    assert result.is_error is True
    assert result.content.startswith("windows_registry_invalid_path")
    assert fake.open_calls == []


def test_registry_get_reads_value_with_key_read(win_env, fake_winreg):
    fake = fake_winreg(values=[("InstallPath", "C:\\App", 1)])

    result = win_env._registry_tool(
        mode="get", path="HKEY_CURRENT_USER\\Software\\Xingyao", name="InstallPath"
    )

    assert result.is_error is False
    assert "C:\\App" in result.content
    assert "REG_SZ" in result.content
    root, subpath, access = fake.open_calls[0]
    assert root == _FakeWinreg.HKEY_CURRENT_USER
    assert subpath == "Software\\Xingyao"
    assert access == _FakeWinreg.KEY_READ
    assert fake.closed == 1


def test_registry_list_enumerates_values_and_subkeys(win_env, fake_winreg):
    fake_winreg(values=[("A", 1, 4), ("B", ["x", "y"], 7)], subkeys=["Sub1", "Sub2"])

    result = win_env._registry_tool(mode="list", path="HKEY_LOCAL_MACHINE\\Software\\Xingyao")

    assert result.is_error is False
    assert "REG_DWORD" in result.content and "REG_MULTI_SZ" in result.content
    assert "Sub1" in result.content and "Sub2" in result.content
    assert "值 2 个，子键 2 个" in result.content


def test_registry_view_flags_are_applied(win_env, fake_winreg):
    fake = fake_winreg(values=[("A", 1, 4)])

    win_env._registry_tool(mode="list", path="HKEY_LOCAL_MACHINE\\Software", view="64")
    assert fake.open_calls[0][2] == _FakeWinreg.KEY_READ | _FakeWinreg.KEY_WOW64_64KEY

    bad_view = win_env._registry_tool(mode="list", path="HKEY_LOCAL_MACHINE\\Software", view="128")
    assert bad_view.is_error is True
    assert bad_view.content.startswith("windows_invalid_arguments")


def test_registry_get_requires_value_name(win_env, fake_winreg):
    fake = fake_winreg()

    result = win_env._registry_tool(mode="get", path="HKEY_CURRENT_USER\\Software")

    assert result.is_error is True
    assert result.content.startswith("windows_invalid_arguments")
    assert fake.open_calls == []


def test_registry_read_failure_returns_error_without_traceback(win_env, fake_winreg):
    fake_winreg(error=FileNotFoundError("no such key"))

    result = win_env._registry_tool(mode="list", path="HKEY_CURRENT_USER\\Software\\NoSuchKey")

    assert result.is_error is True
    assert result.content.startswith("windows_registry_path_not_found")
    assert "Traceback" not in result.content


def test_registry_permission_error_is_normalized(win_env, fake_winreg):
    fake_winreg(error=PermissionError("denied"))

    result = win_env._registry_tool(mode="list", path="HKEY_LOCAL_MACHINE\\SECURITY")

    assert result.is_error is True
    assert result.content.startswith("windows_registry_access_denied")
    assert "Traceback" not in result.content


def test_registry_output_is_truncated(win_env, fake_winreg):
    fake_winreg(values=[("Huge", "x" * 9000, 1)])

    result = win_env._registry_tool(mode="list", path="HKEY_CURRENT_USER\\Software\\Huge")

    assert result.is_error is False
    assert "截断" in result.content
    assert len(result.content) <= 4000 + 120


class _FakeWin32Gui:
    def __init__(self, foreground=4242, error=None):
        self.foreground = foreground
        self.error = error
        self.message_boxes = []

    def GetForegroundWindow(self):
        return self.foreground

    def MessageBox(self, hwnd, message, title, flags):
        if self.error is not None:
            raise self.error
        self.message_boxes.append((hwnd, message, title, flags))
        return 1


@pytest.fixture
def fake_win32gui(monkeypatch):
    def _install(**kwargs):
        fake = _FakeWin32Gui(**kwargs)
        monkeypatch.setitem(sys.modules, "win32gui", fake)
        return fake

    return _install


def test_notify_rejects_empty_message(win_env, fake_win32gui):
    fake = fake_win32gui()

    result = win_env._notify_tool(title="提醒", message="   ")

    assert result.is_error is True
    assert result.content.startswith("windows_invalid_arguments")
    assert fake.message_boxes == []


@pytest.mark.parametrize(
    "title,message",
    [
        ("长" * 65, "正文"),
        ("标题", "长" * 257),
    ],
)
def test_notify_rejects_over_long_fields(win_env, fake_win32gui, title, message):
    fake = fake_win32gui()

    result = win_env._notify_tool(title=title, message=message)

    assert result.is_error is True
    assert result.content.startswith("windows_invalid_arguments")
    assert fake.message_boxes == []


def test_notify_shows_message_box(win_env, fake_win32gui):
    fake = fake_win32gui()

    result = win_env._notify_tool(title="构建完成", message="产物已就绪，请查收")

    assert result.is_error is False
    assert fake.message_boxes == [(4242, "产物已就绪，请查收", "构建完成", 0x40)]
    assert "确定" in result.content


def test_notify_failure_returns_error_without_traceback(win_env, fake_win32gui):
    fake_win32gui(error=RuntimeError("boom"))

    result = win_env._notify_tool(title="提醒", message="正文")

    assert result.is_error is True
    assert result.content.startswith("windows_notify_failed")
    assert "Traceback" not in result.content


def test_notify_missing_pywin32_degrades_gracefully(win_env, monkeypatch):
    monkeypatch.setitem(sys.modules, "win32gui", None)

    result = win_env._notify_tool(title="提醒", message="正文")

    assert result.is_error is True
    assert result.content.startswith("windows_notify_unavailable")
    assert "Traceback" not in result.content