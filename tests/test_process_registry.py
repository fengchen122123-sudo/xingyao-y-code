"""Workspace Process Registry 测试（规格 §9.2）。"""

from core.git import process_registry as reg
from core.git.process_registry import (
    ProcessRecord,
    active_processes,
    clear_registry,
    register_process,
    risk_class_for_command,
    unregister_process,
)


def setup_function():
    clear_registry()


def test_register_and_query_by_workspace():
    register_process("E:/proj", handle="h1", kind="execute_command", risk_class="git")
    register_process("e:/PROJ", handle="h2", kind="background_task", foreground=False, long_running=True)
    records = active_processes("E:/proj")
    assert {r.handle for r in records} == {"h1", "h2"}  # 路径规范化（大小写/分隔符）后同一工作区


def test_unregister_removes_record():
    register_process("E:/proj", handle="h1")
    assert unregister_process("h1") is True
    assert unregister_process("h1") is False  # 幂等
    assert active_processes("E:/proj") == ()


def test_active_other_workspace_isolated():
    register_process("E:/proj", handle="h1")
    assert active_processes("E:/other") == ()


def test_risk_class_mapping():
    assert risk_class_for_command("git status --porcelain") == "git"
    assert risk_class_for_command('"git.exe" commit -m x') == "git"
    assert risk_class_for_command("dir /b") == "readonly"
    assert risk_class_for_command("npm run dev") == "unknown"
    assert risk_class_for_command("") == "unknown"


def test_record_defaults_and_freeze():
    rec = register_process("E:/proj", handle="h9")
    assert isinstance(rec, ProcessRecord)
    assert rec.foreground is True and rec.long_running is False and rec.risk_class == "unknown"
    try:
        rec.handle = "x"
        raise AssertionError("应不可变")
    except Exception:
        pass
