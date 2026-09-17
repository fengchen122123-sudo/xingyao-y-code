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
        "target_session": "星轨自动化任务",
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
    assert args[1] == "星轨自动化任务"
    assert kwargs["internal_kind"] == "automation"
    assert kwargs["display_text"] == "星轨自动化任务「每日摘要」"
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
    entry = bridge._message_queues["星轨自动化任务"][0]
    assert entry["text"] == "汇总昨日进展"
    assert entry["display_text"] == "星轨自动化任务「每日摘要」"
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
    bridge._message_queues["星轨自动化任务"] = [{
        "id": "q_abc",
        "text": "汇总昨日进展",
        "display_text": "星轨自动化任务「每日摘要」",
        "attachments": [],
        "turn_source": "local",
        "automation_run_id": run["id"],
        "automation_task_name": "每日摘要",
        "automation_permission_mode": "accept_edits",
    }]

    bridge._drain_message_queue("星轨自动化任务")

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
    bridge._message_queues["星轨自动化任务"] = [{
        "id": "q_user", "text": "你好", "display_text": "你好",
        "attachments": [], "turn_source": "local",
    }]
    bridge._drain_message_queue("星轨自动化任务")
    args2, kwargs2 = spawned[1]
    assert kwargs2.get("internal_kind", "") == ""
    assert kwargs2.get("automation_context") is None


# ---------------------------------------------------------------------------
# 任务工作目录（workdir）：新会话懒创建归属
# ---------------------------------------------------------------------------

def test_dispatch_new_session_carries_task_workdir(manager, tmp_path):
    """任务指定 workdir 且目标会话未落库：spawn 带 pending_workdir 供懒创建绑定。"""
    bridge, spawned, _ = _make_bridge(manager, tmp_path)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    task, run = _create_task_with_run(bridge, workdir=str(project_dir))

    bridge._dispatch_automation_run(task, run["id"], "scheduled")

    assert len(spawned) == 1
    assert spawned[0][1]["pending_workdir"] == str(project_dir)


def test_dispatch_global_workdir_for_new_session(manager, tmp_path):
    """GLOBAL：新会话挂自由对话分组，原样透传给 worker。"""
    bridge, spawned, _ = _make_bridge(manager, tmp_path)
    task, run = _create_task_with_run(bridge, workdir="GLOBAL")

    bridge._dispatch_automation_run(task, run["id"], "scheduled")

    assert spawned[0][1]["pending_workdir"] == "GLOBAL"


def test_dispatch_existing_session_keeps_own_workdir(manager, tmp_path):
    """目标会话已存在：会话自身归属是权威，任务 workdir 不改写。"""
    bridge, spawned, _ = _make_bridge(manager, tmp_path)
    manager.ensure_session("星轨自动化任务", workdir=str(tmp_path / "other"), source="GUI")
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    task, run = _create_task_with_run(bridge, workdir=str(project_dir))

    bridge._dispatch_automation_run(task, run["id"], "scheduled")

    assert spawned[0][1].get("pending_workdir", "") == ""


def test_dispatch_no_workdir_keeps_legacy_behavior(manager, tmp_path):
    """旧任务（无 workdir）：不传 pending_workdir，行为与引入前一致。"""
    bridge, spawned, _ = _make_bridge(manager, tmp_path)
    task, run = _create_task_with_run(bridge)

    bridge._dispatch_automation_run(task, run["id"], "scheduled")

    assert spawned[0][1].get("pending_workdir", "") == ""


def test_dispatch_busy_queues_workdir_and_drain_rechecks(manager, tmp_path):
    """忙时入队携带 pending_workdir；补发时会话已被创建则放弃改绑。"""
    bridge, spawned, _ = _make_bridge(manager, tmp_path, is_turn_active=True)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    task, run = _create_task_with_run(bridge, workdir=str(project_dir))

    bridge._dispatch_automation_run(task, run["id"], "scheduled")
    entry = bridge._message_queues["星轨自动化任务"][0]
    assert entry["pending_workdir"] == str(project_dir)

    # 会话在排队期间被其他回合创建 → 补发时以会话自身归属为准
    manager.ensure_session("星轨自动化任务", workdir=str(tmp_path / "other"), source="GUI")
    bridge._is_turn_active = lambda s: False
    bridge._drain_message_queue("星轨自动化任务")
    assert len(spawned) == 1
    assert spawned[0][1].get("pending_workdir", "") == ""


def test_save_automation_task_validates_workdir(manager, tmp_path):
    """桥层保存校验：目录不存在拒绝，GLOBAL 与真实目录放行。"""
    bridge, _, _ = _make_bridge(manager, tmp_path)

    bad = bridge.save_automation_task(_task_dict(workdir=str(tmp_path / "no-such-dir")))
    assert bad["ok"] is False
    assert any("工作目录" in e for e in bad["errors"])

    assert bridge.save_automation_task(_task_dict(workdir="GLOBAL"))["ok"] is True
    real_dir = tmp_path / "proj"
    real_dir.mkdir()
    ok = bridge.save_automation_task(_task_dict(name="t2", workdir=str(real_dir)))
    assert ok["ok"] is True


# ---------------------------------------------------------------------------
# 任务指定模型（model_id）：回合用该模型执行
# ---------------------------------------------------------------------------

def test_dispatch_passes_task_model(manager, tmp_path):
    """任务指定模型：直发 spawn 带 pending_model_id。"""
    bridge, spawned, _ = _make_bridge(manager, tmp_path)
    task, run = _create_task_with_run(bridge, model_id="m-task")

    bridge._dispatch_automation_run(task, run["id"], "scheduled")

    assert len(spawned) == 1
    assert spawned[0][1]["pending_model_id"] == "m-task"


def test_dispatch_no_model_keeps_legacy_behavior(manager, tmp_path):
    """旧任务（无 model_id）：不传 pending_model_id。"""
    bridge, spawned, _ = _make_bridge(manager, tmp_path)
    task, run = _create_task_with_run(bridge)

    bridge._dispatch_automation_run(task, run["id"], "scheduled")

    assert spawned[0][1].get("pending_model_id", "") == ""


def test_dispatch_busy_queues_model_and_drain_forwards(manager, tmp_path):
    """忙时入队携带任务模型；补发回合同样透传给 worker。"""
    bridge, spawned, _ = _make_bridge(manager, tmp_path, is_turn_active=True)
    task, run = _create_task_with_run(bridge, model_id="m-task")

    bridge._dispatch_automation_run(task, run["id"], "scheduled")
    entry = bridge._message_queues["星轨自动化任务"][0]
    assert entry["automation_model_id"] == "m-task"

    bridge._is_turn_active = lambda s: False
    bridge._drain_message_queue("星轨自动化任务")
    assert len(spawned) == 1
    assert spawned[0][1]["pending_model_id"] == "m-task"


def test_worker_binds_pending_model_for_new_session(manager, tmp_path):
    """pending_model_id：worker 落库会话并绑定任务模型（ GLOBAL 兜底归属）。"""
    bridge, _, _ = _make_bridge(manager, tmp_path)
    task_obj, run = _create_task_with_run(bridge)
    bridge._automation_storage.mark_run_running(run["id"])

    with _WorkerHarness(bridge, manager, tmp_path, consume_return=False):
        bridge._run_turn_worker(
            "汇总昨日进展", "星轨自动化任务",
            internal_kind="automation",
            automation_context={"run_id": run["id"], "task_name": "每日摘要",
                                "permission_mode": "confirm"},
            pending_model_id="m-task",
        )

    assert manager.get_session_model_id("星轨自动化任务") == "m-task"
    # 会话已按 GLOBAL 兜底落库（任务未指定 workdir 的旧数据场景）
    names = [s.get("name") for s in manager.get_all_sessions(workdir=None, include_empty=True)]
    assert "星轨自动化任务" in names


def test_worker_without_pending_model_keeps_binding_untouched(manager, tmp_path):
    """未指定模型：worker 不改写会话既有模型绑定。"""
    bridge, _, _ = _make_bridge(manager, tmp_path)
    manager.ensure_session("星轨自动化任务", workdir="GLOBAL", source="GUI")
    manager.set_session_model_id("星轨自动化任务", "m-original")

    with _WorkerHarness(bridge, manager, tmp_path, consume_return=False):
        bridge._run_turn_worker("你好", "星轨自动化任务")

    assert manager.get_session_model_id("星轨自动化任务") == "m-original"


def test_save_automation_task_validates_model_id(manager, tmp_path, monkeypatch):
    """桥层保存校验：模型必须存在于配置；保存后回读一致。"""
    import gui_bridge as gb

    monkeypatch.setattr(
        "gui_bridge.load_config",
        lambda: {"providers": [{"models": [{"id": "m-ok"}]}]},
    )
    bridge, _, _ = _make_bridge(manager, tmp_path)

    bad = bridge.save_automation_task(_task_dict(model_id="m-gone"))
    assert bad["ok"] is False
    assert any("模型" in e for e in bad["errors"])

    ok = bridge.save_automation_task(_task_dict(model_id="m-ok"))
    assert ok["ok"] is True
    assert bridge.get_automation_tasks()["tasks"][0]["model_id"] == "m-ok"


# ---------------------------------------------------------------------------
# _run_turn_worker 终态回写（真实 worker + 桩模型入口）
# ---------------------------------------------------------------------------

class _WorkerHarness:
    """为 _run_turn_worker 桩掉模型入口与事件出口，保留真实存储/turn_undo。"""

    def __init__(self, bridge, manager, tmp_path, consume_return=False, model_error=False,
                 terminal_error=None):
        self.bridge = bridge
        self.manager = manager
        self.tmp_path = tmp_path
        manager.add_messages("星轨自动化任务", [("user", "预热会话")])
        bridge._session_workdir = lambda s: str(tmp_path)
        bridge._sync_active_model_context = lambda *a, **k: None
        bridge._recalc_token_usage = lambda *a, **k: None
        bridge._emit_token_usage = lambda *a, **k: None
        bridge._maybe_auto_rename = lambda s: s
        bridge._subagent_progress_poller = lambda *a, **k: None
        bridge._emit_message_final = lambda *a, **k: None
        bridge._clear_session_last_error = lambda *a, **k: None
        bridge._emit_owned_error = lambda *a, **k: None
        self.terminal_error = terminal_error
        self.consume_return = consume_return

        def _fake_consume(history, model_name, api_key, api_base, session_name, turn_id,
                          model_id=None, error_state=None, turn_source="local"):
            # 模拟 core/llm.py 内层生成器把模型失败转为 turn_error 事件的真实路径：
            # 经 _emit_owned_error(terminal=True) 落入 error_state，worker 据此感知
            if self.terminal_error and error_state is not None:
                error_state["terminal_message"] = self.terminal_error
            return self.consume_return

        bridge._consume_turn_events = _fake_consume
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
            "汇总昨日进展", "星轨自动化任务",
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
            WHERE session_id = (SELECT id FROM sessions WHERE session_name = '星轨自动化任务')
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
            "汇总昨日进展", "星轨自动化任务",
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
            "汇总昨日进展", "星轨自动化任务",
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
        bridge._run_turn_worker("你好", "星轨自动化任务")
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
    assert bridge._message_queues["星轨自动化任务"]


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


# ---------------------------------------------------------------------------
# 评审修复回归：P1-1 turn_error 终态 / P2-1 原子单飞 / P2-2 事件 / P2-6 自愈 / P2-7 DTO
# ---------------------------------------------------------------------------

def test_worker_turn_error_marks_run_error(manager, tmp_path):
    """P1-1：模型侧 turn_error（terminal_message）必须记 error，不得误记 completed。"""
    bridge, _, events = _make_bridge(manager, tmp_path)
    task_obj, run = _create_task_with_run(bridge)
    bridge._automation_storage.mark_run_running(run["id"])

    with _WorkerHarness(bridge, manager, tmp_path, consume_return=False,
                        terminal_error="API 调用失败：重试 5 次后仍超时"):
        bridge._run_turn_worker(
            "汇总昨日进展", "星轨自动化任务",
            internal_kind="automation",
            automation_context={"run_id": run["id"], "task_name": "每日摘要",
                                 "permission_mode": "confirm", "notify_on_success": ""},
        )

    run_row = bridge._automation_storage.list_runs(task_obj["id"])[0]
    assert run_row["status"] == "error"
    assert "API 调用失败" in run_row["error"]
    notices = [e for e in events if e[0] == "notice"]
    assert any("运行失败" in e[1]["message"] for e in notices)
    # 终态通知必须带归属会话：前端按 session_name 路由，缺省会串到当前查看的会话
    assert all(e[1]["session_name"] == "星轨自动化任务" for e in notices)


def test_worker_finish_completed_notice_carries_session(manager, tmp_path):
    """notify_on_success 开启时完成提示必须带 session_name（按归属会话路由）。"""
    bridge, _, events = _make_bridge(manager, tmp_path)
    task_obj, run = _create_task_with_run(bridge)
    bridge._automation_storage.mark_run_running(run["id"])

    with _WorkerHarness(bridge, manager, tmp_path, consume_return=False):
        bridge._run_turn_worker(
            "汇总昨日进展", "星轨自动化任务",
            internal_kind="automation",
            automation_context={"run_id": run["id"], "task_name": "每日摘要",
                                 "permission_mode": "confirm", "notify_on_success": "1"},
        )

    notices = [e for e in events if e[0] == "notice"]
    assert any("运行完成" in e[1]["message"] for e in notices)
    assert all(e[1]["session_name"] == "星轨自动化任务" for e in notices)


def test_dispatch_spawn_failure_marks_error_run(manager, tmp_path):
    """P2-6：spawn 抛异常时运行行立即置 error，不留 stuck 行阻塞单飞。"""
    bridge, _, _ = _make_bridge(manager, tmp_path)
    task_obj, run = _create_task_with_run(bridge)

    def _boom(*a, **k):
        raise RuntimeError("线程创建失败")

    bridge._spawn_turn_worker = _boom
    bridge._dispatch_automation_run(task_obj, run["id"], "scheduled")

    run_row = bridge._automation_storage.list_runs(task_obj["id"])[0]
    assert run_row["status"] == "error"
    assert "线程创建失败" in run_row["error"]


def test_run_row_transitions_push_events(manager, tmp_path):
    """P2-2/P2-3：queued→running→completed 全程推 automation_run_updated，
    认领（queued）时同步推任务快照。"""
    bridge, _, events = _make_bridge(manager, tmp_path)
    bridge._automation_storage.on_run_updated = bridge._on_automation_run_updated
    task_obj, run = _create_task_with_run(bridge)  # create_run 即触发 queued

    bridge._dispatch_automation_run(task_obj, run["id"], "scheduled")  # → running
    bridge._automation_storage.finish_run(run["id"], "completed")

    statuses = [e[1]["run"]["status"] for e in events if e[0] == "automation_run_updated"]
    assert statuses == ["queued", "running", "completed"]
    task_events = [e for e in events if e[0] == "automation_tasks_changed"]
    assert len(task_events) == 1  # 仅 queued（认领）时刷新快照


def test_manual_run_if_idle_atomic(manager, tmp_path):
    """P2-1：锁内 check+insert，活动运行存在时返回 None。"""
    task_obj, run = _create_task_with_run(bridge := _make_bridge(manager, tmp_path)[0])

    bridge._automation_storage.mark_run_running(run["id"])
    assert bridge._automation_storage.create_manual_run_if_idle(task_obj) is None
    bridge._automation_storage.finish_run(run["id"], "completed")
    second = bridge._automation_storage.create_manual_run_if_idle(task_obj)
    assert second is not None and second["status"] == "queued"


def test_task_dto_bools_normalized(manager, tmp_path):
    """P2-7：enabled/notify_on_success 归一为布尔，与类型声明一致。"""
    bridge, _, _ = _make_bridge(manager, tmp_path)
    bridge._automation_storage.save_task(_task_dict(notify_on_success=1))
    dto = bridge.get_automation_tasks()["tasks"][0]
    assert dto["enabled"] is True
    assert dto["notify_on_success"] is True


def test_run_now_single_flight_after_fix(manager, tmp_path):
    """P2-1 桥层语义保持：原子单飞拒绝第二个并发手动触发。"""
    bridge, spawned, _ = _make_bridge(manager, tmp_path, is_turn_active=True)
    task_id = bridge.save_automation_task(_task_dict())["id"]

    first = bridge.run_automation_task_now(task_id)
    second = bridge.run_automation_task_now(task_id)
    assert first["ok"] is True
    assert second["ok"] is False and "尚未结束" in second["msg"]
    assert len(bridge._message_queues["星轨自动化任务"]) == 1
