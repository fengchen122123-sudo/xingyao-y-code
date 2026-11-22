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

    def _boom():
        raise RuntimeError("controller unavailable")

    monkeypatch.setattr(computer_controller, "get_computer_control_service", _boom)
    assert gui_bridge._gui_may_raise_window() is True
