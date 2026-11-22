import pytest

from core.memory import MemoryManager
from gui_bridge import ApiBridge


@pytest.fixture()
def manager(tmp_path):
    return MemoryManager(str(tmp_path / "ycode.db"))


def _make_bridge(manager) -> ApiBridge:
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._current_session = "s1"
    bridge._current_permission_mode = "confirm"
    bridge._memory = manager
    return bridge


def test_session_binding_overrides_global_only_for_that_session(manager):
    bridge = _make_bridge(manager)
    manager.ensure_session("s1", workdir="GLOBAL", source="GUI")
    manager.ensure_session("s2", workdir="GLOBAL", source="GUI")

    assert bridge._effective_permission_mode("s1") == "confirm"
    res = bridge.set_permission_mode("full", "s2")
    assert res["ok"] is True and res["bound"] is True
    assert manager.get_session_permission_mode("s2") == "full"
    assert bridge._effective_permission_mode("s2") == "full"
    assert bridge._effective_permission_mode("s1") == "confirm"
    assert bridge._current_permission_mode == "confirm"


def test_unbound_session_falls_back_to_global(manager):
    bridge = _make_bridge(manager)
    manager.ensure_session("s1", workdir="GLOBAL", source="GUI")

    res = bridge.set_permission_mode("plan", "session_ab12cd")
    assert res == {"ok": True, "permission_mode": "plan", "bound": False}
    assert bridge._effective_permission_mode("session_ab12cd") == "confirm"

    manager.set_session_permission_mode("s1", "plan")
    assert bridge._effective_permission_mode("s1") == "plan"
    manager.set_session_permission_mode("s1", None)
    assert bridge._effective_permission_mode("s1") == "confirm"


def test_global_mode_update_without_session_name(manager):
    bridge = _make_bridge(manager)
    res = bridge.set_permission_mode("plan")
    assert res["ok"] is True and res["permission_mode"] == "plan"
    assert bridge._current_permission_mode == "plan"
    assert bridge._effective_permission_mode("s1") == "plan"

    bad = bridge.set_permission_mode("bogus", "s1")
    assert bad["ok"] is False


def test_worker_binds_pending_permission_mode(manager, tmp_path):
    from tests.test_gui_automation_dispatch import (
        _WorkerHarness,
        _make_bridge as _make_worker_bridge,
    )

    bridge = _make_worker_bridge(manager, tmp_path)[0]
    with _WorkerHarness(bridge, manager, tmp_path, consume_return=False):
        bridge._run_turn_worker("你好", "自动化任务", pending_permission_mode="plan")

    assert manager.get_session_permission_mode("自动化任务") == "plan"


def test_worker_without_pending_mode_keeps_binding_untouched(manager, tmp_path):
    from tests.test_gui_automation_dispatch import (
        _WorkerHarness,
        _make_bridge as _make_worker_bridge,
    )

    bridge = _make_worker_bridge(manager, tmp_path)[0]
    manager.ensure_session("自动化任务", workdir="GLOBAL", source="GUI")
    manager.set_session_permission_mode("自动化任务", "accept_edits")

    with _WorkerHarness(bridge, manager, tmp_path, consume_return=False):
        bridge._run_turn_worker("你好", "自动化任务")

    assert manager.get_session_permission_mode("自动化任务") == "accept_edits"


def test_worker_binds_session_mode_into_auth_path(manager, tmp_path):
    from core import app_context
    from tests.test_gui_automation_dispatch import (
        _WorkerHarness,
        _make_bridge as _make_worker_bridge,
    )

    bridge = _make_worker_bridge(manager, tmp_path)[0]
    manager.ensure_session("自动化任务", workdir="GLOBAL", source="GUI")
    manager.set_session_permission_mode("自动化任务", "full")
    bridge._current_permission_mode = "confirm"
    app_context.set_permission_mode("confirm")

    seen = []
    harness = _WorkerHarness(bridge, manager, tmp_path, consume_return=False)
    harness.__enter__()
    real_consume = bridge._consume_turn_events

    def _capture(*args, **kwargs):
        seen.append(app_context.get_permission_mode())
        return real_consume(*args, **kwargs)

    bridge._consume_turn_events = _capture
    try:
        bridge._run_turn_worker("你好", "自动化任务")
    finally:
        harness.__exit__(None, None, None)

    assert seen == ["full"]
    assert app_context.get_permission_mode() == "confirm"


def test_send_user_message_validates_and_forwards_permission_mode(manager, tmp_path):
    bridge = _make_bridge(manager)
    bridge._turn_start_lock = __import__("threading").Lock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._is_turn_active = lambda s: False
    spawned = []
    bridge._spawn_turn_worker = lambda *a, **k: spawned.append((a, k))

    bad = bridge.send_user_message("你好", "s1", permission_mode="bogus")
    assert bad["ok"] is False and spawned == []

    ok = bridge.send_user_message("你好", "s1", permission_mode="plan")
    assert ok["ok"] is True
    assert len(spawned) == 1
    assert spawned[0][1]["pending_permission_mode"] == "plan"

    ok2 = bridge.send_user_message("在吗", "s1")
    assert ok2["ok"] is True
    assert spawned[1][1].get("pending_permission_mode", "") == ""
