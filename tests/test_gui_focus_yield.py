"""通用化重构 Task F'：GUI 焦点礼让守卫（gui_bridge._gui_may_raise_window）单测。

电脑操控 run 活动（running/paused/waiting_for_user）期间主窗口不得抢前台；
其余状态与 controller 不可用（fail-open）时允许提升。纯逻辑单测，
绝不触碰真实窗口/键鼠/截屏。
"""

from __future__ import annotations

import pytest

import gui_bridge
from core.computer import controller as computer_controller
from core.computer.models import ComputerRunSnapshot


class _FakeService:
    def __init__(self, status):
        self._status = status

    def snapshot(self):
        return ComputerRunSnapshot(status=self._status)


@pytest.mark.parametrize("status", ["running", "paused", "waiting_for_user"])
def test_active_computer_run_blocks_window_raise(monkeypatch, status):
    monkeypatch.setattr(
        computer_controller,
        "get_computer_control_service",
        lambda: _FakeService(status),
    )
    assert gui_bridge._gui_may_raise_window() is False


@pytest.mark.parametrize(
    "status",
    [
        "idle",
        "starting",
        "completing",
        "completed",
        "stopping",
        "stopped",
        "failed",
        "provider_safety_required",
    ],
)
def test_inactive_computer_run_allows_window_raise(monkeypatch, status):
    monkeypatch.setattr(
        computer_controller,
        "get_computer_control_service",
        lambda: _FakeService(status),
    )
    assert gui_bridge._gui_may_raise_window() is True


def test_controller_failure_fails_open(monkeypatch):
    """controller 不可用：按可提升处理，操控关闭时 GUI 行为完全不变。"""

    def _boom():
        raise RuntimeError("controller unavailable")

    monkeypatch.setattr(computer_controller, "get_computer_control_service", _boom)
    assert gui_bridge._gui_may_raise_window() is True
