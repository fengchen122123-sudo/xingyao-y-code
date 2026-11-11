"""星轨自动化任务：调度引擎（认领→分发、异常隔离、startup 首轮、stop 幂等）。"""

import threading
import time

import pytest

from core.automation.engine import AutomationEngine
from core.automation.storage import AutomationStorage, new_automation_id
from core.memory import MemoryManager


@pytest.fixture()
def manager(tmp_path):
    return MemoryManager(str(tmp_path / "ycode.db"))


@pytest.fixture()
def storage(manager):
    return AutomationStorage(manager)


class DispatchRecorder:
    """记录 dispatch 调用并可注入异常；用事件同步线程用例。"""

    def __init__(self, fail_task_ids=()):
        self.calls: list[tuple[dict, str, str]] = []
        self.fail_task_ids = set(fail_task_ids)
        self.events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def __call__(self, task: dict, run_id: str, trigger_kind: str):
        with self._lock:
            self.calls.append((dict(task), run_id, trigger_kind))
            event = self.events.setdefault(task["id"], threading.Event())
        if task["id"] in self.fail_task_ids:
            raise RuntimeError(f"分发失败: {task['name']}")
        event.set()

    def wait_for(self, task_id: str, timeout: float = 5.0) -> bool:
        # 必须先在锁内注册事件再等待：若用 get(id, Event()) 等一个临时对象，
        # 引擎线程随后 setdefault 创建的将是另一个 Event，永远等不到 set
        with self._lock:
            event = self.events.get(task_id)
            if event is None:
                event = threading.Event()
                self.events[task_id] = event
        return event.wait(timeout)

    def call_count(self, task_id: str) -> int:
        return sum(1 for task, _, _ in self.calls if task["id"] == task_id)


def _insert_task(storage: AutomationStorage, *, due: bool = True, **overrides) -> dict:
    base = {
        "name": "每分钟任务",
        "trigger_type": "daily",
        "trigger_config": {"time": "09:30"},
        "target_session": "自动化任务",
        "prompt": "做点事",
        "enabled": 1,
    }
    base.update(overrides)
    task_id = storage.save_task(base)
    if due:
        storage.set_next_run_raw(task_id, time.time() - 10)
    return storage.get_task(task_id)


# ---------------------------------------------------------------------------
# _run_iteration：同步确定性测试
# ---------------------------------------------------------------------------

def test_iteration_dispatches_due_task(storage):
    task = _insert_task(storage)
    recorder = DispatchRecorder()
    engine = AutomationEngine(storage, recorder)
    engine._run_iteration()
    assert recorder.call_count(task["id"]) == 1
    assert recorder.calls[0][2] == "scheduled"
    assert recorder.calls[0][0]["prompt"] == task["prompt"]
    # next_run_at 已推进，第二次迭代不再派发
    engine._run_iteration()
    assert recorder.call_count(task["id"]) == 1


def test_iteration_startup_only_on_first(storage):
    _insert_task(storage, trigger_type="startup", trigger_config={}, due=False)
    recorder = DispatchRecorder()
    engine = AutomationEngine(storage, recorder)
    engine._run_iteration()  # first_iteration=True
    assert recorder.calls and recorder.calls[0][2] == "startup"
    engine._run_iteration()  # 已翻面
    assert len(recorder.calls) == 1


def test_iteration_startup_retriggers_on_new_process(storage):
    """回归：应用重启（新引擎实例）时 startup 任务重新触发一次。"""
    task = _insert_task(storage, trigger_type="startup", trigger_config={}, due=False)
    clock = {"now": 1000.0}

    def time_fn():
        return clock["now"]

    first = DispatchRecorder()
    AutomationEngine(storage, first, time_fn=time_fn)._run_iteration()
    assert first.call_count(task["id"]) == 1
    assert first.calls[0][2] == "startup"
    storage.finish_run(first.calls[0][1], "completed")
    # 下一次应用启动：进程启动时刻晚于上次写入的认领标记
    clock["now"] = 1000.0 + 86400
    second = DispatchRecorder()
    AutomationEngine(storage, second, time_fn=time_fn)._run_iteration()
    assert second.call_count(task["id"]) == 1
    assert second.calls[0][2] == "startup"


def test_iteration_single_flight_does_not_dispatch(storage):
    task = _insert_task(storage)
    storage._insert_run_raw(new_automation_id("run"), task["id"], "会话", "running")
    recorder = DispatchRecorder()
    engine = AutomationEngine(storage, recorder)
    engine._run_iteration()
    assert recorder.calls == []
    runs = storage.list_runs(task["id"])
    assert any(r["status"] == "skipped" for r in runs)


def test_iteration_dispatch_error_marks_run_and_continues(storage):
    bad = _insert_task(storage, name="会炸的任务")
    good = _insert_task(storage, name="正常的任务")
    recorder = DispatchRecorder(fail_task_ids={bad["id"]})
    engine = AutomationEngine(storage, recorder)
    engine._run_iteration()
    assert recorder.call_count(good["id"]) == 1  # 单任务异常不停摆
    bad_runs = storage.list_runs(bad["id"])
    error_runs = [r for r in bad_runs if r["status"] == "error"]
    assert len(error_runs) == 1
    assert "分发失败" in error_runs[0]["error"]


def test_iteration_survives_claim_error(storage, monkeypatch):
    task = _insert_task(storage)
    recorder = DispatchRecorder()
    engine = AutomationEngine(storage, recorder)
    original = storage.claim_due_tasks
    state = {"failed": False}

    def flaky(*args, **kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("数据库抖动")
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "claim_due_tasks", flaky)
    engine._run_iteration()  # 认领失败：不派发、不停摆
    assert recorder.calls == []
    engine._run_iteration()
    assert recorder.call_count(task["id"]) == 1


# ---------------------------------------------------------------------------
# 线程生命周期
# ---------------------------------------------------------------------------

def test_engine_thread_dispatches_and_wakes(storage):
    # 先插入未来任务，再启动引擎；通过 notify_changed 让保存的新到期任务立即生效
    recorder = DispatchRecorder()
    engine = AutomationEngine(storage, recorder)
    engine.start()
    try:
        task_id = storage.save_task({
            "name": "唤醒任务",
            "trigger_type": "interval",
            "trigger_config": {"interval_minutes": 5},
            "target_session": "自动化任务",
            "prompt": "唤醒",
            "enabled": 1,
        })
        storage.set_next_run_raw(task_id, time.time() - 1)
        engine.notify_changed()
        assert recorder.wait_for(task_id, timeout=5.0), "notify_changed 应立即触发派发"
    finally:
        engine.stop()
    assert engine._thread is None


def test_engine_stop_is_idempotent(storage):
    recorder = DispatchRecorder()
    engine = AutomationEngine(storage, recorder)
    engine.start()
    engine.stop()
    engine.stop()  # 幂等
    engine.start()  # 可重启
    try:
        assert engine._thread is not None and engine._thread.is_alive()
        assert engine._thread.name == "automation-engine"
        assert engine._thread.daemon is True
    finally:
        engine.stop()
    engine.stop()
    assert engine._thread is None


def test_engine_double_start_keeps_single_thread(storage):
    recorder = DispatchRecorder()
    engine = AutomationEngine(storage, recorder)
    engine.start()
    try:
        first = engine._thread
        engine.start()  # 重复启动是无操作
        assert engine._thread is first
    finally:
        engine.stop()


def test_engine_stopped_thread_no_longer_dispatches(storage):
    task = _insert_task(storage)
    recorder = DispatchRecorder()
    engine = AutomationEngine(storage, recorder)
    engine.start()
    assert recorder.wait_for(task["id"], timeout=5.0)
    engine.stop()
    storage.set_next_run_raw(task["id"], time.time() - 1)
    engine.notify_changed()
    time.sleep(0.2)
    assert recorder.call_count(task["id"]) == 1


# ---------------------------------------------------------------------------
# 有限次执行（max_runs）：达上限即停、手动执行不消耗次数
# ---------------------------------------------------------------------------

def _finish_queued_runs(storage, task_id: str) -> None:
    """收尾该任务全部 queued 运行行：fake dispatch 不跑真实回合，需手动解除单飞。"""
    for run in storage.list_runs(task_id):
        if run["status"] == "queued":
            storage.finish_run(run["id"], "completed")


def test_max_runs_task_stops_after_limit(storage):
    task = _insert_task(storage, name="只跑两次", max_runs=2)
    recorder = DispatchRecorder()
    engine = AutomationEngine(storage, recorder)

    engine._run_iteration()  # 第 1 次认领
    assert recorder.call_count(task["id"]) == 1
    _finish_queued_runs(storage, task["id"])
    storage.set_next_run_raw(task["id"], time.time() - 10)

    engine._run_iteration()  # 第 2 次认领 → 达上限，落终态
    assert recorder.call_count(task["id"]) == 2
    reloaded = storage.get_task(task["id"])
    assert reloaded["scheduled_run_count"] == 2
    assert reloaded["status"] == "completed"
    assert reloaded["next_run_at"] is None

    # 终态之后即便把 next_run_at 拽回过去，引擎也不再认领
    _finish_queued_runs(storage, task["id"])
    storage.set_next_run_raw(task["id"], time.time() - 10)
    engine._run_iteration()
    assert recorder.call_count(task["id"]) == 2


def test_manual_run_does_not_consume_max_runs(storage):
    task = _insert_task(storage, name="手动试跑", max_runs=2)
    manual = storage.create_manual_run_if_idle(task)
    assert manual is not None and manual["status"] == "queued"
    storage.finish_run(manual["id"], "completed")

    reloaded = storage.get_task(task["id"])
    assert reloaded["scheduled_run_count"] == 0  # 手动执行不消耗生命周期
    assert reloaded["status"] == "active"
    assert reloaded["next_run_at"] is not None  # 周期节奏不受手动执行影响
