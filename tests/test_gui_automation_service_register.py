"""星轨自动化：桥侧能力注册为 core 层服务句柄（b2）。

沿用 tests/test_gui_automation_dispatch.py 的裸实例夹具风格：ApiBridge.__new__
手工灌入最小内部状态，只验证注册/注销接缝本身，不启动真实调度线程。

注：AutomationService 的公共方法（如 svc.save_task）是转发用绑定方法，不是注入的
回调本体，因此「回调绑定正确」在私有属性 _save_task 等上做相等断言（绑定方法按
__func__/__self__ 比较相等），公共方法的行为由第二个用例端到端验证。
"""
import queue
import threading

import pytest

from core.automation import service as automation_service
from core.automation.storage import AutomationStorage
from core.memory import MemoryManager
from gui_bridge import ApiBridge


@pytest.fixture(autouse=True)
def _clean_service_singleton():
    """每个用例前后清理模块级单例，避免用例互相污染。"""
    automation_service.unregister()
    yield
    automation_service.unregister()


@pytest.fixture()
def manager(tmp_path):
    return MemoryManager(str(tmp_path / "ycode.db"))


class _StubEngine:
    """鸭子类型调度引擎：只记录 notify_changed/stop，注册用例不需要真实线程。"""

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
    """构造仅含注册/注销接缝所需内部状态的裸桥实例。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._memory = manager
    bridge._automation_storage = AutomationStorage(manager)
    bridge._automation_turn_labels = {}
    bridge._automation_engine = _StubEngine()
    # 桥侧既有实现的旁路出口：事件推送桩掉，校验/存储/上限逻辑保留真实实现
    bridge._emit_automation_tasks_changed = lambda: None
    return bridge


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def test_register_binds_bridge_callbacks(manager):
    """注册后句柄可取得，且 7 个回调均指向桥侧既有实现。"""
    bridge = _make_bridge(manager)
    assert automation_service.get() is None

    bridge._register_automation_service()

    svc = automation_service.get()
    assert svc is not None
    # 回调绑定：绑定方法按相等比较（每次属性访问都是新对象，不能用 is）
    assert svc._list_tasks == bridge.get_automation_tasks
    assert svc._get_task == bridge._automation_storage.get_task
    assert svc._save_task == bridge.save_automation_task
    assert svc._delete_task == bridge.delete_automation_task
    assert svc._set_enabled == bridge.set_automation_task_enabled
    assert svc._run_now == bridge.run_automation_task_now
    assert svc._get_runs == bridge.get_automation_runs


def test_register_is_idempotent_and_covers_full_surface(manager):
    """重复注册只替换句柄，不产生副作用（引擎不启动、不触达调度）。"""
    bridge = _make_bridge(manager)

    bridge._register_automation_service()
    first = automation_service.get()
    bridge._register_automation_service()
    second = automation_service.get()

    assert first is not None and second is not None and second is not first
    assert second._save_task == bridge.save_automation_task
    # 注册不启动引擎、不推送事件
    assert bridge._automation_engine.stop_calls == 0
    assert bridge._automation_engine.notify_changed_calls == 0


def test_service_callbacks_reuse_bridge_validations(manager):
    """端到端：经句柄调用复用桥侧既有校验/上限/事件推送，不重复实现。"""
    bridge = _make_bridge(manager)
    bridged_calls = []
    bridge._emit_automation_tasks_changed = lambda: bridged_calls.append(1)
    bridge._register_automation_service()
    svc = automation_service.get()

    assert svc.list_tasks() == {"ok": True, "tasks": []}

    saved = svc.save_task(_task_dict())
    assert saved["ok"] is True
    # 桥侧校验生效（空提示词被拒）
    assert svc.save_task(_task_dict(prompt=""))["ok"] is False

    task_id = saved["id"]
    assert svc.get_task(task_id)["name"] == "每日摘要"
    assert svc.set_enabled(task_id, False)["ok"] is True
    assert svc.list_tasks()["tasks"][0]["enabled"] is False
    assert svc.get_runs(task_id, 50) == {"ok": True, "runs": []}
    assert svc.delete_task(task_id)["ok"] is True
    # 桥侧事件推送与引擎通知均被复用
    assert bridged_calls
    assert bridge._automation_engine.notify_changed_calls >= 1


# ---------------------------------------------------------------------------
# 注销
# ---------------------------------------------------------------------------

def _make_shutdown_bridge(manager):
    """构造可跑完整 shutdown() 的裸桥实例（收尾链各步全部桩掉/清空）。"""
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
    """shutdown 先停引擎、再注销句柄，收尾链继续推进到取消事件。"""
    bridge = _make_shutdown_bridge(manager)
    assert automation_service.get() is not None

    bridge.shutdown()

    assert bridge._automation_engine.stop_calls == 1
    assert automation_service.get() is None
    # 注销之后的收尾链没有被中断（取消事件已 set）
    assert bridge._cancel_events["自动化任务"].is_set()


def test_shutdown_unregister_failure_does_not_break_chain(manager, monkeypatch):
    """注销抛异常时被吞掉，后续收尾链（取消事件）照常执行。"""
    bridge = _make_shutdown_bridge(manager)

    def _boom():
        raise RuntimeError("注销失败")

    monkeypatch.setattr(automation_service, "unregister", _boom)

    bridge.shutdown()  # 不得抛出

    assert bridge._automation_engine.stop_calls == 1
    assert bridge._cancel_events["自动化任务"].is_set()


def test_unregister_is_idempotent(manager):
    """未注册时直接注销不抛异常，get() 仍为 None。"""
    automation_service.unregister()
    automation_service.unregister()
    assert automation_service.get() is None
