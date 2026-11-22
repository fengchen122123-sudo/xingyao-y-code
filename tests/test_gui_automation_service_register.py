import queue
import threading

import pytest

from core.automation import service as automation_service
from core.automation.storage import AutomationStorage
from core.memory import MemoryManager
from gui_bridge import ApiBridge


@pytest.fixture(autouse=True)
def _clean_service_singleton():
    automation_service.unregister()
    yield
    automation_service.unregister()


@pytest.fixture()
def manager(tmp_path):
    return MemoryManager(str(tmp_path / "ycode.db"))


class _StubEngine:

    def __init__(self):
        self.notify_changed_calls = 0
        self.stop_calls = 0

    def notify_changed(self):
        self.notify_changed_calls += 1

    def stop(self):
        self.stop_calls += 1


def _task_dict(**overrides) -> dict:
    base = {
        "name": "每日摘要",
        "trigger_type": "daily",
        "trigger_config": {"time": "09:30"},
        "target_session": "自动化任务",
        "prompt": "汇总昨日进展",
        "permission_mode": "confirm",
        "missed_run_policy": "skip",
        "enabled": 1,
    }
    base.update(overrides)
    return base


def _make_bridge(manager):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._memory = manager
    bridge._automation_storage = AutomationStorage(manager)
    bridge._automation_turn_labels = {}
    bridge._automation_engine = _StubEngine()
    bridge._emit_automation_tasks_changed = lambda: None
    return bridge


def test_register_binds_bridge_callbacks(manager):
    bridge = _make_bridge(manager)
    assert automation_service.get() is None

    bridge._register_automation_service()

    svc = automation_service.get()
    assert svc is not None
    assert svc._list_tasks == bridge.get_automation_tasks
    assert svc._get_task == bridge._automation_storage.get_task
    assert svc._save_task == bridge.save_automation_task
    assert svc._delete_task == bridge.delete_automation_task
    assert svc._set_enabled == bridge.set_automation_task_enabled
    assert svc._run_now == bridge.run_automation_task_now
    assert svc._get_runs == bridge.get_automation_runs


def test_register_is_idempotent_and_covers_full_surface(manager):
    bridge = _make_bridge(manager)

    bridge._register_automation_service()
    first = automation_service.get()
    bridge._register_automation_service()
    second = automation_service.get()

    assert first is not None and second is not None and second is not first
    assert second._save_task == bridge.save_automation_task
    assert bridge._automation_engine.stop_calls == 0
    assert bridge._automation_engine.notify_changed_calls == 0


def test_service_callbacks_reuse_bridge_validations(manager):
    bridge = _make_bridge(manager)
    bridged_calls = []
    bridge._emit_automation_tasks_changed = lambda: bridged_calls.append(1)
    bridge._register_automation_service()
    svc = automation_service.get()

    assert svc.list_tasks() == {"ok": True, "tasks": []}

    saved = svc.save_task(_task_dict())
    assert saved["ok"] is True
    assert svc.save_task(_task_dict(prompt=""))["ok"] is False

    task_id = saved["id"]
    assert svc.get_task(task_id)["name"] == "每日摘要"
    assert svc.set_enabled(task_id, False)["ok"] is True
    assert svc.list_tasks()["tasks"][0]["enabled"] is False
    assert svc.get_runs(task_id, 50) == {"ok": True, "runs": []}
    assert svc.delete_task(task_id)["ok"] is True
    assert bridged_calls
    assert bridge._automation_engine.notify_changed_calls >= 1


def _make_shutdown_bridge(manager):
    bridge = _make_bridge(manager)
    bridge._register_automation_service()
    bridge._shutdown_lock = threading.Lock()
    bridge._cancel_events = {"自动化任务": threading.Event()}
    bridge._bg_summary_lock = threading.Lock()
    bridge._bg_summary_pending_sessions = set()
    bridge._perm_lock = threading.Lock()
    bridge._perm_timers = {}
    bridge._active_turns = {}
    bridge._lan_server = None
    bridge._mcp = None
    bridge._js_queue = queue.Queue()
    return bridge


def test_shutdown_unregisters_service_after_engine_stop(manager):
    bridge = _make_shutdown_bridge(manager)
    assert automation_service.get() is not None

    bridge.shutdown()

    assert bridge._automation_engine.stop_calls == 1
    assert automation_service.get() is None
    assert bridge._cancel_events["自动化任务"].is_set()


def test_shutdown_unregister_failure_does_not_break_chain(manager, monkeypatch):
    bridge = _make_shutdown_bridge(manager)

    def _boom():
        raise RuntimeError("注销失败")

    monkeypatch.setattr(automation_service, "unregister", _boom)

    bridge.shutdown()

    assert bridge._automation_engine.stop_calls == 1
    assert bridge._cancel_events["自动化任务"].is_set()


def test_unregister_is_idempotent(manager):
    automation_service.unregister()
    automation_service.unregister()
    assert automation_service.get() is None
