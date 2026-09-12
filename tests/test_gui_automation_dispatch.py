"""星轨自动化任务：GUI 桥集成（分发、忙时排队透传、运行终态回写、js_api）。

全部用裸实例 + 桩函数构造（与 tests/test_gui_message_queue.py 同模式），
worker 终态用例走真实 _run_turn_worker + 真实 MemoryManager/turn_undo，
只桩掉模型入口与事件出口。
"""
import threading

import pytest

from core.automation.storage import AutomationStorage
from core.memory import MemoryManager
from gui_bridge import ApiBridge


@pytest.fixture()
def manager(tmp_path):
    return MemoryManager(str(tmp_path / "ycode.db"))


def _make_bridge(manager, tmp_path, *, is_turn_active=False):
    """构造仅含自动化所需内部状态的裸桥实例。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._current_session = "s1"
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._cancel_events = {}
    bridge._is_turn_active = (lambda s: True) if is_turn_active else (lambda s: False)
    # worker 终态用例会触达的身份/快照结构
    bridge._active_turn_ids = {}
    bridge._active_turn_lock = threading.Lock()
    bridge._active_turn_snapshots = {}
    bridge._turn_owner_lock = threading.Lock()
    bridge._turn_owner_sessions = {}
    bridge._bg_summary_lock = threading.Lock()
    bridge._bg_summary_triggered = set()
    bridge._bg_summary_pending_sessions = set()
    bridge._user_cancelled_sessions = set()
    bridge._memory = manager
    bridge._automation_storage = AutomationStorage(manager)
    bridge._automation_turn_labels = {}
    events = []
    bridge.emit_event = lambda etype, payload: events.append((etype, payload))
    spawned = []

    def _spawn(*args, **kwargs):
        spawned.append((args, kwargs))

    bridge._spawn_turn_worker = _spawn
    bridge._automation_engine = type(
        "E", (), {"notify_changed": lambda self: None}
    )()
    # worker/权限路径的重量级出口全部桩掉
    bridge._undo_turn_impact_for_event = lambda request: None
    bridge._get_cancel_event = lambda s: threading.Event()
    bridge._finalize_session_deltas = lambda s: None
    return bridge, spawned, events


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


def _create_task_with_run(bridge, *, trigger_kind="scheduled", **overrides):
    """入库任务并创建一条运行记录，返回 (task_dict, run_dict)。"""
    task_id = bridge._automation_storage.save_task(_task_dict(**overrides))
    task = bridge._automation_storage.get_task(task_id)
    run = bridge._automation_storage.create_run(task, trigger_kind=trigger_kind)
    return task, run


# ---------------------------------------------------------------------------
# _dispatch_automation_run
# ---------------------------------------------------------------------------

def test_dispatch_idle_spawns_internal_turn(manager, tmp_path):
    bridge, spawned, events = _make_bridge(manager, tmp_path)
    task, run = _create_task_with_run(bridge)

    bridge._dispatch_automation_run(task, run["id"], "scheduled")

    assert len(spawned) == 1
    args, kwargs = spawned[0]
    assert args[0] == "汇总昨日进展"
    assert args[1] == "自动化任务"
    assert kwargs["internal_kind"] == "automation"
    assert kwargs["display_text"] == "自动化任务「每日摘要」"
    assert kwargs["automation_context"]["run_id"] == run["id"]
    assert kwargs["automation_context"]["permission_mode"] == "confirm"
    # 运行记录 queued → running
    run_row = bridge._automation_storage.list_runs(task["id"])[0]
    assert run_row["status"] == "running"


def test_dispatch_busy_enqueues_with_automation_metadata(manager, tmp_path):
    bridge, spawned, events = _make_bridge(manager, tmp_path, is_turn_active=True)
    task, run = _create_task_with_run(bridge)

    bridge._dispatch_automation_run(task, run["id"], "scheduled")

    assert spawned == []  # 忙时不打断当前回合
    entry = bridge._message_queues["自动化任务"][0]
    assert entry["text"] == "汇总昨日进展"
    assert entry["display_text"] == "自动化任务「每日摘要」"
    assert entry["automation_run_id"] == run["id"]
    assert entry["automation_permission_mode"] == "confirm"
    assert entry["turn_source"] == "local"
    assert events[-1][0] == "message_queue"
    # 运行记录保持 queued
    run_row = bridge._automation_storage.list_runs(task["id"])[0]
    assert run_row["status"] == "queued"


def test_drain_message_queue_passes_automation_context(manager, tmp_path):
    bridge, spawned, events = _make_bridge(manager, tmp_path)
    task, run = _create_task_with_run(bridge)
    bridge._message_queues["自动化任务"] = [{
        "id": "q_abc",
        "text": "汇总昨日进展",
        "display_text": "自动化任务「每日摘要」",
        "attachments": [],
        "turn_source": "local",
        "automation_run_id": run["id"],
        "automation_task_name": "每日摘要",
        "automation_permission_mode": "accept_edits",
    }]

    bridge._drain_message_queue("自动化任务")

    assert len(spawned) == 1
    args, kwargs = spawned[0]
    assert kwargs["internal_kind"] == "automation"
    assert kwargs["automation_context"] == {
        "run_id": run["id"],
        "task_name": "每日摘要",
        "permission_mode": "accept_edits",
        "notify_on_success": "",
    }
    run_row = bridge._automation_storage.list_runs(task["id"])[0]
    assert run_row["status"] == "running"

    # 旧式用户消息条目（无自动化键）补发不受影响
    bridge._message_queues["自动化任务"] = [{
        "id": "q_user", "text": "你好", "display_text": "你好",
        "attachments": [], "turn_source": "local",
    }]
    bridge._drain_message_queue("自动化任务")
    args2, kwargs2 = spawned[1]
    assert kwargs2.get("internal_kind", "") == ""
    assert kwargs2.get("automation_context") is None


# ---------------------------------------------------------------------------
# _run_turn_worker 终态回写（真实 worker + 桩模型入口）
# ---------------------------------------------------------------------------

class _WorkerHarness:
    """为 _run_turn_worker 桩掉模型入口与事件出口，保留真实存储/turn_undo。"""

    def __init__(self, bridge, manager, tmp_path, consume_return=False, model_error=False):
        self.bridge = bridge
        self.manager = manager
        self.tmp_path = tmp_path
        manager.add_messages("自动化任务", [("user", "预热会话")])
        bridge._session_workdir = lambda s: str(tmp_path)
        bridge._sync_active_model_context = lambda *a, **k: None
        bridge._recalc_token_usage = lambda *a, **k: None
        bridge._emit_token_usage = lambda *a, **k: None
        bridge._maybe_auto_rename = lambda s: s
        bridge._subagent_progress_poller = lambda *a, **k: None
        bridge._emit_message_final = lambda *a, **k: None
        bridge._clear_session_last_error = lambda *a, **k: None
        bridge._emit_owned_error = lambda *a, **k: None
        bridge._consume_turn_events = lambda *a, **k: consume_return
        if model_error:
            self.model = None
        else:
            self.model = {
                "litellm_model": "openai/model-one",
                "api_key": "",
                "base_url": "",
                "context_window": 128000,
                "id": "m1",
            }
        self.model_error = model_error

    def __enter__(self):
        import gui_bridge as gb
        from core.turn_undo import service as tu_service

        # 配置 turn_undo 默认存储（真实 GUI 由 ApiBridge.__init__ 完成），
        # 退出时还原全局，避免污染后续测试
        self._prev_manager = tu_service._default_manager
        self._prev_storage = tu_service._default_storage
        tu_service.configure_default_manager(self.manager, blob_root=self.tmp_path / "blobs")

        self._orig_get_active_model = gb.get_active_model

        def _fake_get_active_model(session_name):
            if self.model_error:
                return None
            return self.model

        gb.get_active_model = _fake_get_active_model
        return self

    def __exit__(self, *exc):
        import gui_bridge as gb
        from core.turn_undo import service as tu_service

        gb.get_active_model = self._orig_get_active_model
        tu_service._default_manager = self._prev_manager
        tu_service._default_storage = self._prev_storage
        return False


def test_worker_finish_writes_completed_run(manager, tmp_path):
    bridge, _, _ = _make_bridge(manager, tmp_path)
    task_obj, run = _create_task_with_run(bridge)
    bridge._automation_storage.mark_run_running(run["id"])

    with _WorkerHarness(bridge, manager, tmp_path, consume_return=False):
        bridge._run_turn_worker(
            "汇总昨日进展", "自动化任务",
            internal_kind="automation",
            automation_context={"run_id": run["id"], "task_name": "每日摘要", "permission_mode": "confirm"},
        )

    run_row = bridge._automation_storage.list_runs(task_obj["id"])[0]
    assert run_row["status"] == "completed"
    assert run_row["finished_at"]
    assert run_row["turn_id"]  # turn_id 已回填
    # 提示词逐字落库为 user 消息（运行痕迹，messages 表唯一事实源）
    with manager._lock:
        last = manager.conn.execute(
            """
            SELECT role, content FROM messages
            WHERE session_id = (SELECT id FROM sessions WHERE session_name = '自动化任务')
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    assert last is not None and last[0] == "user"
    assert "汇总昨日进展" in str(last[1])
    # turn_id 已从自动化活跃映射清除
    assert bridge._automation_turn_labels == {}


def test_worker_finish_writes_cancelled_run(manager, tmp_path):
    bridge, _, _ = _make_bridge(manager, tmp_path)
    task_obj, run = _create_task_with_run(bridge, trigger_kind="manual")
    bridge._automation_storage.mark_run_running(run["id"])

    with _WorkerHarness(bridge, manager, tmp_path, consume_return=True):
        bridge._run_turn_worker(
            "汇总昨日进展", "自动化任务",
            internal_kind="automation",
            automation_context={"run_id": run["id"], "task_name": "每日摘要", "permission_mode": "confirm"},
        )

    run_row = bridge._automation_storage.list_runs(task_obj["id"])[0]
    assert run_row["status"] == "cancelled"


def test_worker_finish_writes_error_run_on_model_missing(manager, tmp_path):
    bridge, _, _ = _make_bridge(manager, tmp_path)
    task_obj, run = _create_task_with_run(bridge)
    bridge._automation_storage.mark_run_running(run["id"])

    with _WorkerHarness(bridge, manager, tmp_path, model_error=True):
        bridge._run_turn_worker(
            "汇总昨日进展", "自动化任务",
            internal_kind="automation",
            automation_context={"run_id": run["id"], "task_name": "每日摘要", "permission_mode": "confirm"},
        )

    run_row = bridge._automation_storage.list_runs(task_obj["id"])[0]
    assert run_row["status"] == "error"
    assert "模型" in run_row["error"]


def test_worker_without_automation_context_writes_nothing(manager, tmp_path):
    """人工/内部回合零影响：不写 automation_runs、不登记 turn_labels。"""
    bridge, _, _ = _make_bridge(manager, tmp_path)
    with _WorkerHarness(bridge, manager, tmp_path, consume_return=False):
        bridge._run_turn_worker("你好", "自动化任务")
    assert bridge._automation_storage.list_runs(None) == []
    assert bridge._automation_turn_labels == {}


# ---------------------------------------------------------------------------
# js_api 方法
# ---------------------------------------------------------------------------

def test_save_automation_task_validation_and_events(manager, tmp_path):
    bridge, _, events = _make_bridge(manager, tmp_path)
    notified = []
    bridge._automation_engine = type("E", (), {"notify_changed": lambda self: notified.append(1)})()

    bad = bridge.save_automation_task(_task_dict(prompt=""))
    assert bad["ok"] is False and bad["errors"]

    ok = bridge.save_automation_task(_task_dict())
    assert ok["ok"] is True and ok["id"]
    assert any(e[0] == "automation_tasks_changed" for e in events)
    assert notified == [1]
    assert bridge.get_automation_tasks()["tasks"][0]["trigger_summary"] == "每天 09:30"


def test_save_automation_task_rejects_65th(manager, tmp_path):
    from core.automation.models import MAX_TASKS

    bridge, _, _ = _make_bridge(manager, tmp_path)
    for i in range(MAX_TASKS):
        assert bridge.save_automation_task(_task_dict(name=f"t{i}"))["ok"] is True
    overflow = bridge.save_automation_task(_task_dict(name="超限"))
    assert overflow["ok"] is False
    assert "最多" in overflow["errors"][0]
    # 编辑已有任务不受上限限制
    first = bridge.get_automation_tasks()["tasks"][0]
    edit = bridge.save_automation_task({**first, "name": "改名"})
    assert edit["ok"] is True


def test_run_now_and_single_flight(manager, tmp_path):
    bridge, spawned, _ = _make_bridge(manager, tmp_path)
    task_id = bridge.save_automation_task(_task_dict())["id"]

    res = bridge.run_automation_task_now(task_id)
    assert res["ok"] is True
    assert len(spawned) == 1

    # 上一次运行还在 running：单飞拒绝
    res2 = bridge.run_automation_task_now(task_id)
    assert res2["ok"] is False and "尚未结束" in res2["msg"]
    assert len(spawned) == 1


def test_run_now_busy_enqueues(manager, tmp_path):
    bridge, spawned, events = _make_bridge(manager, tmp_path, is_turn_active=True)
    task_id = bridge.save_automation_task(_task_dict())["id"]

    res = bridge.run_automation_task_now(task_id)
    assert res["ok"] is True
    assert spawned == []
    assert bridge._message_queues["自动化任务"]


def test_enabled_toggle_recomputes_next_run(manager, tmp_path):
    import time as _time

    bridge, _, _ = _make_bridge(manager, tmp_path)
    task_id = bridge.save_automation_task(_task_dict())["id"]
    bridge._automation_storage.set_next_run_raw(task_id, 1000.0)

    assert bridge.set_automation_task_enabled(task_id, False)["ok"] is True
    assert bridge.get_automation_tasks()["tasks"][0]["enabled"] == 0
    bridge.set_automation_task_enabled(task_id, True)
    assert bridge.get_automation_tasks()["tasks"][0]["next_run_at"] > _time.time()


def test_delete_and_runs_api(manager, tmp_path):
    bridge, _, _ = _make_bridge(manager, tmp_path)
    task_id = bridge.save_automation_task(_task_dict())["id"]
    task_obj = bridge._automation_storage.get_task(task_id)
    bridge._automation_storage.create_run(task_obj, trigger_kind="manual")

    runs = bridge.get_automation_runs(task_id)
    assert runs["ok"] is True and len(runs["runs"]) == 1

    assert bridge.clear_automation_runs(task_id)["ok"] is True
    assert bridge.get_automation_runs(task_id)["runs"] == []

    assert bridge.delete_automation_task(task_id)["ok"] is True
    assert bridge.get_automation_tasks()["tasks"] == []


def test_dispatch_skipped_run_emits_notice(manager, tmp_path):
    """storage.on_run_updated 回调链：skipped 行推送事件与 notice。"""
    bridge, _, events = _make_bridge(manager, tmp_path)
    bridge._automation_storage.on_run_updated = bridge._on_automation_run_updated
    task = _insert_due_with_active_run(bridge)
    from core.automation import scheduling

    bridge._automation_storage.claim_due_tasks(
        __import__("time").time(), scheduling.compute_next_run,
        include_startup=False, catchup_enabled=False,
    )
    kinds = [e[0] for e in events]
    assert "automation_run_updated" in kinds
    assert "notice" in kinds


def _insert_due_with_active_run(bridge):
    task_id = bridge._automation_storage.save_task(_task_dict(
        trigger_type="interval", trigger_config={"interval_minutes": 5},
    ))
    import time as _time

    bridge._automation_storage.set_next_run_raw(task_id, _time.time() - 10)
    task = bridge._automation_storage.get_task(task_id)
    bridge._automation_storage.create_run(task, trigger_kind="scheduled")
    bridge._automation_storage.mark_run_running(
        bridge._automation_storage.list_runs(task_id)[0]["id"]
    )
    return task
