import gui_bridge


def test_force_finish_reason_stop_requested(monkeypatch):
    monkeypatch.setattr(gui_bridge, "_STOP_WATCHDOG_SECONDS", 60.0)
    monkeypatch.setattr(gui_bridge, "_STALL_TIMEOUT_SECONDS", 1800.0)

    assert gui_bridge._force_finish_reason(59.0, True) is None
    assert gui_bridge._force_finish_reason(60.0, True) == "stop"


def test_force_finish_reason_stall_without_stop(monkeypatch):
    monkeypatch.setattr(gui_bridge, "_STOP_WATCHDOG_SECONDS", 60.0)
    monkeypatch.setattr(gui_bridge, "_STALL_TIMEOUT_SECONDS", 1800.0)

    assert gui_bridge._force_finish_reason(60.0, False) is None
    assert gui_bridge._force_finish_reason(1799.0, False) is None
    assert gui_bridge._force_finish_reason(1800.0, False) == "stall"


def test_force_finish_reason_stop_takes_priority(monkeypatch):
    monkeypatch.setattr(gui_bridge, "_STOP_WATCHDOG_SECONDS", 60.0)
    monkeypatch.setattr(gui_bridge, "_STALL_TIMEOUT_SECONDS", 1800.0)

    assert gui_bridge._force_finish_reason(2000.0, True) == "stop"