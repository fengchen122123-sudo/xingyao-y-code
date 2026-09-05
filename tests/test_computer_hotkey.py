"""全局紧急停止热键（core/computer/hotkey.py）测试（计划 Task 5 / 设计 3.3、15.1、17.3）。

所有测试只使用 FakeHotkeyApi，绝不在 pytest 中注册真实系统热键。
"""

import ctypes
import logging
import sys

import pytest

import core.computer.hotkey as hotkey_module
from core.computer.hotkey import (
    EmergencyStopHotkey,
    WindowsHotkeyApi,
    get_emergency_hotkey_snapshot,
)
from core.computer.models import ComputerControlError
from tests.computer_fakes import FakeHotkeyApi


@pytest.fixture(autouse=True)
def _clear_active_hotkey():
    """每个测试结束后清空进程级活动热键注册表，避免跨测试污染。"""
    yield
    hotkey_module._set_active_hotkey(None)


# ---------------- brief 样例（逐字） ----------------


def test_hotkey_registration_failure_is_not_reported_available():
    api = FakeHotkeyApi(register_result=False)
    hotkey = EmergencyStopHotkey(api, on_stop=lambda: None)
    with pytest.raises(ComputerControlError) as raised:
        hotkey.start()
    assert raised.value.code == "computer_hotkey_unavailable"
    assert hotkey.available is False


def test_hotkey_callback_only_requests_controller_stop():
    requested = []
    hotkey = EmergencyStopHotkey(FakeHotkeyApi(), on_stop=lambda: requested.append("stop"))
    hotkey._dispatch_for_test("Ctrl+Alt+Esc")
    assert requested == ["stop"]


# ---------------- 生命周期与快照边界 ----------------


def test_hotkey_start_registers_once_and_reports_available():
    api = FakeHotkeyApi()
    hotkey = EmergencyStopHotkey(api, on_stop=lambda: None)
    hotkey.start()
    hotkey.start()
    assert api.register_calls == 1
    assert hotkey.available is True
    hotkey.close()


def test_registered_callback_reaches_on_stop():
    requested = []
    api = FakeHotkeyApi()
    hotkey = EmergencyStopHotkey(api, on_stop=lambda: requested.append("stop"))
    hotkey.start()
    try:
        api.on_hotkey()
    finally:
        hotkey.close()
    assert requested == ["stop"]


def test_dispatch_ignores_other_hotkey_labels():
    requested = []
    hotkey = EmergencyStopHotkey(FakeHotkeyApi(), on_stop=lambda: requested.append("stop"))
    hotkey._dispatch_for_test("Ctrl+Alt+Delete")
    hotkey._dispatch_for_test("")
    assert requested == []


def test_hotkey_close_is_idempotent_and_marks_unavailable():
    api = FakeHotkeyApi()
    hotkey = EmergencyStopHotkey(api, on_stop=lambda: None)
    hotkey.start()
    hotkey.close()
    hotkey.close()
    assert api.unregister_calls == 1
    assert hotkey.available is False


def test_hotkey_close_without_start_is_noop():
    api = FakeHotkeyApi()
    hotkey = EmergencyStopHotkey(api, on_stop=lambda: None)
    hotkey.close()
    assert api.unregister_calls == 0
    assert hotkey.available is False


def test_failed_start_does_not_unregister_or_stay_active():
    api = FakeHotkeyApi(register_result=False)
    hotkey = EmergencyStopHotkey(api, on_stop=lambda: None)
    with pytest.raises(ComputerControlError):
        hotkey.start()
    assert api.unregister_calls == 0
    assert get_emergency_hotkey_snapshot().available is False


def test_hotkey_snapshot_is_conservative_without_active_instance():
    snapshot = get_emergency_hotkey_snapshot()
    assert snapshot.available is False


def test_hotkey_snapshot_tracks_active_instance():
    hotkey = EmergencyStopHotkey(FakeHotkeyApi(), on_stop=lambda: None)
    hotkey.start()
    assert get_emergency_hotkey_snapshot().available is True
    hotkey.close()
    assert get_emergency_hotkey_snapshot().available is False


def test_default_api_requires_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ComputerControlError) as raised:
        WindowsHotkeyApi()
    assert raised.value.code == "computer_unsupported_os"


# ---------------- 安全留痕：异常绝不静默吞掉 ----------------


@pytest.mark.skipif(sys.platform != "win32", reason="默认热键 API 仅在 Windows 构造")
def test_hotkey_callback_exception_is_logged(caplog):
    api = WindowsHotkeyApi()

    def _boom():
        raise RuntimeError("on_stop exploded")

    api._on_hotkey = _boom
    with caplog.at_level(logging.ERROR, logger="core.computer.hotkey"):
        api._invoke_on_hotkey()
    assert any(
        record.name == "core.computer.hotkey" and record.levelno == logging.ERROR
        for record in caplog.records
    )


@pytest.mark.skipif(sys.platform != "win32", reason="默认热键 API 仅在 Windows 构造")
def test_hotkey_quit_message_failure_is_logged(monkeypatch, caplog):
    api = WindowsHotkeyApi()

    def _raise(*_args):
        raise OSError("post failed")

    monkeypatch.setattr(ctypes.windll.user32, "PostThreadMessageW", _raise)
    with caplog.at_level(logging.ERROR, logger="core.computer.hotkey"):
        api._post_quit_message(12345)
    assert any(
        record.name == "core.computer.hotkey" and record.levelno == logging.ERROR
        for record in caplog.records
    )
