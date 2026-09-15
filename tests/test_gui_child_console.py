"""core.gui_lifecycle.hide_child_console_windows 的单元测试。

沙盒约束：测试不真正启动任何子进程——把 subprocess.Popen.__init__ 换成只记录
kwargs 的假实现，验证包装后 creationflags 是否被补上 CREATE_NO_WINDOW。
所有改动经 monkeypatch 恢复，不污染其它测试。
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.gui_lifecycle as gui_lifecycle


@pytest.fixture
def record_popen_init(monkeypatch):
    """把 subprocess.Popen.__init__ 换成记录 kwargs 的假实现，返回记录列表。

    假实现不启动进程；monkeypatch 在用例结束后自动还原真实实现。
    """
    calls = []

    def fake_init(self, *args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", fake_init)
    return fake_init, calls


@pytest.fixture
def no_console(monkeypatch):
    """模拟"本进程没有控制台窗口"（GetConsoleWindow() 返回 0）。"""
    monkeypatch.setattr(gui_lifecycle, "_console_window_handle", lambda: 0, raising=True)


def test_no_console_wraps_popen_with_create_no_window(record_popen_init, no_console):
    """无控制台时生效：Popen 的 creationflags 被补上 CREATE_NO_WINDOW。"""
    fake_init, calls = record_popen_init

    assert gui_lifecycle.hide_child_console_windows() is True
    assert subprocess.Popen.__init__ is not fake_init

    subprocess.Popen(["cmd"])

    assert len(calls) == 1
    assert calls[0]["creationflags"] & subprocess.CREATE_NO_WINDOW


def test_explicit_creationflags_are_or_ed(record_popen_init, no_console):
    """调用方已显式传 creationflags 时按位或叠加，原标志不丢失。"""
    fake_init, calls = record_popen_init

    assert gui_lifecycle.hide_child_console_windows() is True

    subprocess.Popen(["cmd"], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

    flags = calls[0]["creationflags"]
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert flags & subprocess.CREATE_NO_WINDOW


def test_has_console_leaves_popen_untouched(record_popen_init, monkeypatch):
    """有控制台时（源码调试/CLI 版）不包装：Popen.__init__ 保持原对象。"""
    fake_init, _calls = record_popen_init
    monkeypatch.setattr(gui_lifecycle, "_console_window_handle", lambda: 12345, raising=True)

    assert gui_lifecycle.hide_child_console_windows() is False
    assert subprocess.Popen.__init__ is fake_init
    assert not getattr(fake_init, "_ycode_hides_console", False)


def test_unknown_console_state_leaves_popen_untouched(record_popen_init, monkeypatch):
    """判定失败（拿不到句柄）时保守不生效，不改变既有行为。"""
    fake_init, _calls = record_popen_init
    monkeypatch.setattr(gui_lifecycle, "_console_window_handle", lambda: None, raising=True)

    assert gui_lifecycle.hide_child_console_windows() is False
    assert subprocess.Popen.__init__ is fake_init


def test_repeated_calls_do_not_double_wrap(record_popen_init, no_console):
    """幂等：连续调用两次不会双层包装（第二次直接返回 False）。"""
    fake_init, calls = record_popen_init

    assert gui_lifecycle.hide_child_console_windows() is True
    first_wrapper = subprocess.Popen.__init__
    assert getattr(first_wrapper, "_ycode_hides_console", False) is True

    assert gui_lifecycle.hide_child_console_windows() is False
    assert subprocess.Popen.__init__ is first_wrapper
    # 单层包装：包装函数的 __wrapped__ 直达假实现，而非再套一层包装
    assert getattr(first_wrapper, "__wrapped__", None) is fake_init

    subprocess.Popen(["cmd"])
    assert len(calls) == 1
    assert calls[0]["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_non_windows_is_noop(record_popen_init, monkeypatch):
    """非 Windows 平台直接返回 False，不做任何包装。"""
    import types

    fake_init, _calls = record_popen_init
    # 只替换被调用模块里的 os 引用，不动全局 os（避免影响 pytest 自身）。
    monkeypatch.setattr(
        gui_lifecycle, "os", types.SimpleNamespace(name="posix"), raising=True
    )

    assert gui_lifecycle.hide_child_console_windows() is False
    assert subprocess.Popen.__init__ is fake_init
