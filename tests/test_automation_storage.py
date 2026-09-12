"""星轨自动化任务：SQLite 存储层（CRUD、CAS 认领、单飞、恢复、清理、级联删除）。"""

import threading
import time

import pytest

from core.automation import scheduling
from core.automation.storage import AutomationStorage, iso_now, new_automation_id
from core.memory import MemoryManager


@pytest.fixture()
def manager(tmp_path):
    return MemoryManager(str(tmp_path / "ycode.db"))


@pytest.fixture()
def storage(manager):
    return AutomationStorage(manager)


def _task(**overrides) -> dict:
    base = {
        "name": "每日站会摘要",
        "trigger_type": "daily",
        "trigger_config": {"time": "09:30"},
        "target_session": "自动化任务",
        "prompt": "汇总昨日进展",
        "permission_mode": "confirm",
        "missed_run_policy": "skip",
        "notify_on_success": 0,
        "enabled": 1,
    }
    base.update(overrides)
    return base


def _insert_due_task(storage: AutomationStorage, *, next_run_at=1000.0, **overrides) -> dict:
    task = _task(**overrides)
    task["id"] = storage.save_task(task)
    storage.set_next_run_raw(task["id"], next_run_at)
    return storage.get_task(task["id"])


# ---------------------------------------------------------------------------
# 基础 CRUD
# ---------------------------------------------------------------------------

def test_save_and_get_task_roundtrip(storage):
    task_id = storage.save_task(_task())
    loaded = storage.get_task(task_id)
    assert loaded["name"] == "每日站会摘要"
    assert loaded["trigger_type"] == "daily"
    assert loaded["trigger_config"] == {"time": "09:30"}
    assert loaded["enabled"] == 1
    assert loaded["permission_mode"] == "confirm"
    assert loaded["missed_run_policy"] == "skip"
    assert loaded["created_at"] and loaded["updated_at"]
    assert storage.count_tasks() == 1


def test_save_task_updates_existing(storage):
    task_id = storage.save_task(_task())
    updated = _task(name="改名", id=task_id)
    assert storage.save_task(updated) == task_id
    assert storage.count_tasks() == 1
    assert storage.get_task(task_id)["name"] == "改名"


def test_list_tasks(storage):
    storage.save_task(_task(name="A"))
    storage.save_task(_task(name="B"))
    names = [t["name"] for t in storage.list_tasks()]
    assert names == ["A", "B"]


def test_save_task_computes_next_run_for_timed_triggers(storage):
    now = time.time()
    task_id = storage.save_task(_task())
    next_run = storage.get_task(task_id)["next_run_at"]
    assert next_run is not None and next_run > now


def test_save_task_startup_has_null_next_run(storage):
    task_id = storage.save_task(_task(trigger_type="startup", trigger_config={}))
    assert storage.get_task(task_id)["next_run_at"] is None


def test_save_task_disabled_keeps_schedule_computed(storage):
    # 禁用任务同样维护 next_run_at（引擎按 enabled 过滤），重启用时语义一致
    task_id = storage.save_task(_task(enabled=0))
    assert storage.get_task(task_id)["next_run_at"] is not None


def test_set_enabled_recomputes_next_run_on_enable(storage):
    task_id = storage.save_task(_task(trigger_type="daily", trigger_config={"time": "09:30"}))
    storage.set_next_run_raw(task_id, 1000.0)  # 模拟停用期间的过期时刻
    storage.set_enabled(task_id, False)
    assert storage.get_task(task_id)["enabled"] == 0
    before = time.time()
    storage.set_enabled(task_id, True)
    reloaded = storage.get_task(task_id)
    assert reloaded["enabled"] == 1
    assert reloaded["next_run_at"] > before  # 重新启用后绝不在过去触发


def test_delete_task_cascades_runs(storage):
    task_id = storage.save_task(_task())
    run_id = new_automation_id("run")
    storage._insert_run_raw(run_id, task_id, "自动化任务", "completed")
    assert storage.delete_task(task_id) is True
    assert storage.get_task(task_id) is None
    assert storage.list_runs(task_id) == []
    assert storage.delete_task(task_id) is False


def test_count_tasks(storage):
    for i in range(3):
        storage.save_task(_task(name=f"t{i}"))
    assert storage.count_tasks() == 3


# ---------------------------------------------------------------------------
# 运行记录
# ---------------------------------------------------------------------------

def test_mark_running_and_finish_run(storage):
    task_id = storage.save_task(_task())
    run_id = new_automation_id("run")
    storage._insert_run_raw(run_id, task_id, "自动化任务", "queued")
    storage.mark_run_running(run_id)
    storage.set_run_turn_id(run_id, "turn-123")
    run = storage.list_runs(task_id)[0]
    assert run["status"] == "running"
    assert run["started_at"]
    assert run["turn_id"] == "turn-123"

    assert storage.finish_run(run_id, "completed") is True
    run = storage.list_runs(task_id)[0]
    assert run["status"] == "completed"
    assert run["finished_at"]

    # 终态 exactly-once：二次收尾是 no-op
    assert storage.finish_run(run_id, "error", "不应生效") is False
    assert storage.list_runs(task_id)[0]["status"] == "completed"


def test_finish_run_rejects_unknown_status(storage):
    task_id = storage.save_task(_task())
    run_id = new_automation_id("run")
    storage._insert_run_raw(run_id, task_id, "会话", "running")
    with pytest.raises(ValueError):
        storage.finish_run(run_id, "whatever")


def test_recover_stale_runs(storage):
    task_id = storage.save_task(_task())
    for status in ("queued", "running", "completed", "cancelled"):
        run_id = new_automation_id("run")
        storage._insert_run_raw(run_id, task_id, "会话", status)
    recovered = storage.recover_stale_runs()
    assert recovered == 2
    statuses = sorted(r["status"] for r in storage.list_runs(task_id))
    assert statuses == ["cancelled", "completed", "interrupted", "interrupted"]


def test_run_retention_per_task(storage):
    task_id = storage.save_task(_task())
    for i in range(55):
        run_id = new_automation_id("run")
        storage._insert_run_raw(run_id, task_id, "会话", "completed")
        time.sleep(0.001)  # 保证 queued_at 可排序
    runs = storage.list_runs(task_id)
    assert len(runs) == 50


def test_list_runs_filters_by_task(storage):
    t1 = storage.save_task(_task(name="t1"))
    t2 = storage.save_task(_task(name="t2"))
    storage._insert_run_raw(new_automation_id("run"), t1, "会话", "completed")
    storage._insert_run_raw(new_automation_id("run"), t2, "会话", "error")
    assert len(storage.list_runs(t1)) == 1
    assert len(storage.list_runs(t2)) == 1
    assert len(storage.list_runs(None)) == 2


def test_clear_runs(storage):
    task_id = storage.save_task(_task())
    storage._insert_run_raw(new_automation_id("run"), task_id, "会话", "completed")
    storage.clear_runs(task_id)
    assert storage.list_runs(task_id) == []


def test_has_active_run(storage):
    task_id = storage.save_task(_task())
    run_id = new_automation_id("run")
    storage._insert_run_raw(run_id, task_id, "会话", "queued")
    assert storage.has_active_run(task_id) is True
    storage.finish_run(run_id, "completed")
    assert storage.has_active_run(task_id) is False


# ---------------------------------------------------------------------------
# claim_due_tasks：CAS 认领 / 单飞 / startup / catchup
# ---------------------------------------------------------------------------

def _compute(_ignored):
    return scheduling.compute_next_run


def test_claim_due_task_once(storage):
    now = time.time()
    _insert_due_task(storage, next_run_at=now - 10)
    claimed = storage.claim_due_tasks(now, _compute(None), include_startup=False, catchup_enabled=False)
    assert len(claimed) == 1
    task, run_id, kind = claimed[0]
    assert kind == "scheduled"
    assert storage.get_task(task["id"])["next_run_at"] > now
    # 下一轮不再触发
    assert storage.claim_due_tasks(now, _compute(None), include_startup=False, catchup_enabled=False) == []


def test_claim_creates_queued_run_row(storage):
    now = time.time()
    task = _insert_due_task(storage, next_run_at=now - 10)
    claimed = storage.claim_due_tasks(now, _compute(None), include_startup=False, catchup_enabled=False)
    run = storage.list_runs(task["id"])[0]
    assert run["id"] == claimed[0][1]
    assert run["status"] == "queued"
    assert run["task_name"] == task["name"]
    assert run["trigger_kind"] == "scheduled"


def test_claim_single_flight_writes_skipped(storage):
    now = time.time()
    task = _insert_due_task(storage, next_run_at=now - 10)
    active_run = new_automation_id("run")
    storage._insert_run_raw(active_run, task["id"], "会话", "running")
    before = time.time()
    claimed = storage.claim_due_tasks(now, _compute(None), include_startup=False, catchup_enabled=False)
    assert claimed == []
    runs = storage.list_runs(task["id"])
    skipped = [r for r in runs if r["status"] == "skipped"]
    assert len(skipped) == 1
    assert "上一次运行尚未结束" in skipped[0]["error"]
    assert storage.get_task(task["id"])["next_run_at"] > before  # 周期照常推进


def test_claim_startup_only_on_first_iteration(storage):
    task_id = storage.save_task(_task(trigger_type="startup", trigger_config={}))
    first = storage.claim_due_tasks(time.time(), _compute(None), include_startup=True, catchup_enabled=False)
    assert len(first) == 1 and first[0][2] == "startup"
    assert first[0][0]["id"] == task_id
    second = storage.claim_due_tasks(time.time() + 1000, _compute(None), include_startup=True, catchup_enabled=False)
    assert second == []


def test_catchup_skip_policy_records_and_reschedules(storage):
    task = _insert_due_task(storage, next_run_at=time.time() - 3600, missed_run_policy="skip")
    now = time.time()
    claimed = storage.claim_due_tasks(now, _compute(None), include_startup=True, catchup_enabled=True)
    assert claimed == []  # skip 不派发
    runs = storage.list_runs(task["id"])
    assert any(r["status"] == "skipped" and "过期" in r["error"] for r in runs)
    assert storage.get_task(task["id"])["next_run_at"] > now


def test_catchup_run_once_dispatches_once(storage):
    task = _insert_due_task(storage, next_run_at=time.time() - 3600, missed_run_policy="run_once")
    now = time.time()
    claimed = storage.claim_due_tasks(now, _compute(None), include_startup=True, catchup_enabled=True)
    assert len(claimed) == 1 and claimed[0][2] == "catchup"
    assert storage.get_task(task["id"])["next_run_at"] > now
    # 补跑后不会连环补
    again = storage.claim_due_tasks(now + 1, _compute(None), include_startup=True, catchup_enabled=True)
    assert again == []


def test_cas_prevents_double_claim_across_threads(storage):
    now = time.time()
    _insert_due_task(storage, next_run_at=now - 10)
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        got = storage.claim_due_tasks(now, _compute(None), include_startup=False, catchup_enabled=False)
        with lock:
            results.extend(got)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1  # CAS 保证 16 线程只有 1 个认领成功


def test_claim_guard_against_non_future_next_run(storage):
    # compute_next 返回 <= now 的值时必须被钳制，防止 0 秒死循环
    now = time.time()
    _insert_due_task(storage, next_run_at=now - 10, trigger_type="daily", trigger_config={"time": "09:30"})

    def bad_compute(trigger_type, config, now_ts):
        return now_ts  # 故意返回不严格递增的值

    claimed = storage.claim_due_tasks(now, bad_compute, include_startup=False, catchup_enabled=False)
    assert len(claimed) == 1
    assert storage.get_task(claimed[0][0]["id"])["next_run_at"] > now


def test_earliest_next_run(storage):
    assert storage.earliest_next_run() is None
    storage.save_task(_task(name="a", trigger_type="daily", trigger_config={"time": "23:59"}))
    storage.save_task(_task(name="b", trigger_type="daily", trigger_config={"time": "09:00"}))
    earliest = storage.earliest_next_run()
    assert earliest is not None and earliest > time.time()
    # 禁用任务不计入
    storage.save_task(_task(name="c", enabled=0, trigger_type="daily", trigger_config={"time": "00:00"}))
    assert storage.earliest_next_run() == pytest.approx(earliest)


def test_iso_now_and_new_id():
    value = iso_now()
    assert len(value) >= 19
    assert new_automation_id("run").startswith("run-")
