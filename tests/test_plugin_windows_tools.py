# -*- coding: utf-8 -*-
"""plugins/windows_tools 平台门控与进程/剪贴板能力用例（对齐 windows-mcp 对齐计划 Task 5）。

覆盖：
- plugin.json 元信息（name / version / entry / enabled / 中文描述）
- 模块顶层不导入 psutil / win32api / winreg / comtypes（非 Windows 可安全 exec）
- 非 Windows、YCODE_TOOL_PROFILE=cli 时注册为空
- Windows 下注册顺序（windows_process、windows_clipboard 在前，便于 Task 6 追加）
- 受保护进程拒绝结束（全部用假 psutil.process_iter，绝不真的结束系统进程）
"""

import ast
import importlib.util
import json
import os
import sys
import types

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_DIR = os.path.join(_PROJECT_ROOT, "plugins", "windows_tools")

# 本任务只注册这两个；后续任务追加的工具只要排在它们之后即可，故断言写成「包含且在前」。
_REQUIRED_TOOLS = ["windows_process", "windows_clipboard"]

_FORBIDDEN_TOP_LEVEL_IMPORTS = {"psutil", "win32api", "win32gui", "winreg", "comtypes", "ctypes", "wmi"}


def _load_plugin_module():
    """按路径加载插件薄壳，与 plugin_loader 的加载方式一致。"""
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


@pytest.fixture
def win_env(monkeypatch):
    """模拟 Windows + 非 CLI 档位的注册环境。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("YCODE_TOOL_PROFILE", raising=False)
    return _load_plugin_module()


# ── 元信息与导入安全 ─────────────────────────────────────────


def test_plugin_json_metadata():
    with open(os.path.join(_PLUGIN_DIR, "plugin.json"), encoding="utf-8") as handle:
        meta = json.load(handle)

    assert meta["name"] == "windows_tools"
    assert meta["version"] == "0.1.0"
    assert meta["entry"] == "plugin.py"
    assert meta["enabled"] is True
    assert meta["description"].strip()
    # 中文描述：至少含一个中文字符
    assert any("\u4e00" <= char <= "\u9fff" for char in meta["description"])


def test_module_top_level_imports_are_platform_safe():
    """psutil / win32api / winreg / comtypes 只能在函数内惰性导入。"""
    source = open(os.path.join(_PLUGIN_DIR, "plugin.py"), encoding="utf-8").read()
    tree = ast.parse(source)

    top_level = set()
    for node in tree.body:  # 只看模块顶层语句，函数体内的 import 不计
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                top_level.add(node.module.split(".")[0])

    assert top_level <= {"os", "sys", "core", "__future__"}, top_level
    assert not (top_level & _FORBIDDEN_TOP_LEVEL_IMPORTS)


# ── 平台门控 ────────────────────────────────────────────────


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

    assert names[: len(_REQUIRED_TOOLS)] == _REQUIRED_TOOLS
    assert set(_REQUIRED_TOOLS) <= set(names)


def test_tool_specs_declare_system_side_effects(win_env):
    registry = _register(win_env)
    for name in _REQUIRED_TOOLS:
        spec = registry.get(name)
        assert spec.read_only is False
        assert spec.requires_permission is True
        assert spec.side_effect_scope == "system"
        assert spec.activity_description.strip()
        assert any("\u4e00" <= char <= "\u9fff" for char in spec.activity_description)
        assert spec.input_schema.get("required") == ["mode"]


# ── 进程能力 ────────────────────────────────────────────────


class _FakeProcess:
    """替身进程：只记录结束调用，绝不碰真实进程。"""

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

    result = win_env._process_tool(mode="kill", pid=fake_args[0])

    assert result.is_error is True
    assert result.content.startswith("windows_process_protected")
    assert "受保护" in result.content
    assert fakes[0].terminate_calls == 0
    assert fakes[0].kill_calls == 0


def test_kill_refuses_protected_process_by_name(win_env, fake_processes):
    fakes = fake_processes((1234, "csrss.exe"))

    result = win_env._process_tool(mode="kill", name="csrss.exe", force=True)

    assert result.is_error is True
    assert result.content.startswith("windows_process_protected")
    assert fakes[0].kill_calls == 0


def test_kill_refuses_current_process(win_env, fake_processes):
    fakes = fake_processes((os.getpid(), "python.exe"))

    result = win_env._process_tool(mode="kill", pid=os.getpid())

    assert result.is_error is True
    assert "当前进程" in result.content
    assert fakes[0].terminate_calls == 0


def test_kill_missing_pid_reports_clear_error(win_env, fake_processes):
    fake_processes((2468, "notepad.exe"))

    result = win_env._process_tool(mode="kill", pid=999999)

    assert result.is_error is True
    assert result.content.startswith("windows_process_not_found")
    assert "未找到" in result.content
    assert "Traceback" not in result.content


def test_kill_without_pid_or_name_is_rejected(win_env, fake_processes):
    fake_processes((2468, "notepad.exe"))

    result = win_env._process_tool(mode="kill")

    assert result.is_error is True
    assert "pid 或 name" in result.content


def test_kill_target_process_uses_terminate_by_default(win_env, fake_processes):
    fakes = fake_processes((2468, "notepad.exe"))

    result = win_env._process_tool(mode="kill", name="notepad")  # 省略 .exe 也能命中

    assert result.is_error is False
    assert "已结束 1 个进程" in result.content
    assert fakes[0].terminate_calls == 1
    assert fakes[0].kill_calls == 0


def test_kill_force_uses_kill(win_env, fake_processes):
    fakes = fake_processes((2468, "notepad.exe"))

    result = win_env._process_tool(mode="kill", pid=2468, force=True)

    assert result.is_error is False
    assert "kill" in result.content
    assert fakes[0].kill_calls == 1
    assert fakes[0].terminate_calls == 0


def test_list_processes_renders_table_and_filter(win_env, fake_processes):
    fakes = fake_processes((100, "chrome.exe", 300.0), (200, "notepad.exe", 20.0))

    result = win_env._process_tool(mode="list")

    assert result.is_error is False
    assert "PID" in result.content and "内存(MB)" in result.content and "名称" in result.content
    # 默认按内存降序：chrome 在前
    assert result.content.index("chrome.exe") < result.content.index("notepad.exe")

    filtered = win_env._process_tool(mode="list", filter="notepad")
    assert "notepad.exe" in filtered.content
    assert "chrome.exe" not in filtered.content
    assert all(fake.kill_calls == 0 for fake in fakes)


def test_list_processes_sorts_by_cpu_and_name(win_env, fake_processes):
    fake_processes((100, "chrome.exe", 300.0, 0.5), (200, "aaa.exe", 1.0, 9.0))

    by_cpu = win_env._process_tool(mode="list", sort_by="cpu")
    assert by_cpu.content.index("aaa.exe") < by_cpu.content.index("chrome.exe")

    by_name = win_env._process_tool(mode="list", sort_by="name")
    assert by_name.content.index("aaa.exe") < by_name.content.index("chrome.exe")


def test_unknown_mode_is_rejected(win_env):
    result = win_env._process_tool(mode="suspend")

    assert result.is_error is True
    assert result.content.startswith("windows_invalid_mode")


# ── 剪贴板能力（只验证参数校验路径，真实读写依赖运行桌面会话） ──


def test_clipboard_set_with_empty_text_is_rejected(win_env):
    result = win_env._clipboard_tool(mode="set", text="")

    assert result.is_error is True
    assert "为空" in result.content
    assert result.content.startswith("windows_invalid_arguments")


def test_clipboard_unknown_mode_is_rejected(win_env):
    result = win_env._clipboard_tool(mode="clear")

    assert result.is_error is True
    assert result.content.startswith("windows_invalid_mode")