import threading
import time
from datetime import datetime

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


def test_workdir_roundtrip(storage):
    task_id = storage.save_task(_task())
    assert storage.get_task(task_id)["workdir"] == ""
    global_id = storage.save_task(_task(name="g", workdir="GLOBAL"))
    assert storage.get_task(global_id)["workdir"] == "GLOBAL"
    path_id = storage.save_task(_task(name="p", workdir="E:\\proj"))
    assert storage.get_task(path_id)["workdir"] == "E:\\proj"
    storage.save_task({**storage.get_task(path_id), "workdir": ""})
    assert storage.get_task(path_id)["workdir"] == ""


def test_model_id_roundtrip(storage):
    task_id = storage.save_task(_task())
    assert storage.get_task(task_id)["model_id"] == ""
    mid = storage.save_task(_task(name="m", model_id="m-task"))
    assert storage.get_task(mid)["model_id"] == "m-task"
    storage.save_task({**storage.get_task(mid), "model_id": ""})
    assert storage.get_task(mid)["model_id"] == ""


def test_session_mode_defaults_to_per_run(storage):
    task_id = storage.save_task(_task())
    assert storage.get_task(task_id)["session_mode"] == "per_run"
    fixed_id = storage.save_task(_task(name="固定会话", session_mode="fixed"))
    assert storage.get_task(fixed_id)["session_mode"] == "fixed"
    blank_id = storage.save_task(_task(name="空白", session_mode="   "))
    assert storage.get_task(blank_id)["session_mode"] == "per_run"
    storage.save_task({**storage.get_task(fixed_id), "session_mode": "per_run"})
    assert storage.get_task(fixed_id)["session_mode"] == "per_run"


def test_missed_run_policy_defaults_to_run_once(storage):
    task = _task()
    task.pop("missed_run_policy")
    task_id = storage.save_task(task)
    assert storage.get_task(task_id)["missed_run_policy"] == "run_once"


def test_validate_task_rejects_non_string_model_id(storage):
    from core.automation.models import validate_task

    errors = validate_task(_task(model_id=123))
    assert any("模型" in e for e in errors)
    assert validate_task(_task(model_id="m-task")) == []


def test_validate_task_accepts_plan_permission_mode(storage):
    from core.automation.models import validate_task

    assert validate_task(_task(permission_mode="plan")) == []
    assert validate_task(_task(permission_mode="bogus")) != []


def test_migration_adds_workdir_column_to_legacy_table(manager):
    with manager._lock:
        cursor = manager.conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS automation_tasks")
        cursor.execute("""
            CREATE TABLE automation_tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                trigger_type TEXT NOT NULL,
                trigger_config TEXT NOT NULL DEFAULT '{}',
                target_session TEXT NOT NULL,
                prompt TEXT NOT NULL,
                permission_mode TEXT NOT NULL DEFAULT 'confirm',
                missed_run_policy TEXT NOT NULL DEFAULT 'skip',
                notify_on_success INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_run_at TEXT,
                next_run_at REAL
            )
        """)
        cursor.execute(
            """
            INSERT INTO automation_tasks (
                id, name, trigger_type, trigger_config, target_session, prompt,
                created_at, updated_at
            ) VALUES (
                't1', '旧任务', 'daily', '{"time": "09:30"}', '自动化任务', 'p',
                '2026-01-01 00:00:00', '2026-01-01 00:00:00'
            )
            """
        )
        manager.conn.commit()
    storage = AutomationStorage(manager)
    loaded = storage.get_task("t1")
    assert loaded is not None and loaded["workdir"] == "" and loaded["model_id"] == ""
    storage.save_task({**loaded, "workdir": "GLOBAL", "model_id": "m1"})
    reloaded = storage.get_task("t1")
    assert reloaded["workdir"] == "GLOBAL"
    assert reloaded["model_id"] == "m1"


def test_migration_adds_session_mode_column_to_legacy_table(manager):
    with manager._lock:
        cursor = manager.conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS automation_tasks")
        cursor.execute("""
            CREATE TABLE automation_tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                trigger_type TEXT NOT NULL,
                trigger_config TEXT NOT NULL DEFAULT '{}',
                target_session TEXT NOT NULL,
                prompt TEXT NOT NULL,
                permission_mode TEXT NOT NULL DEFAULT 'confirm',
                missed_run_policy TEXT NOT NULL DEFAULT 'skip',
                notify_on_success INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_run_at TEXT,
                next_run_at REAL,
                workdir TEXT NOT NULL DEFAULT '',
                model_id TEXT NOT NULL DEFAULT '',
                max_runs INTEGER,
                scheduled_run_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        cursor.execute(
            """
            INSERT INTO automation_tasks (
                id, name, trigger_type, trigger_config, target_session, prompt,
                created_at, updated_at
            ) VALUES (
                't1', '旧任务', 'daily', '{"time": "09:30"}', '自动化任务', 'p',
                '2026-01-01 00:00:00', '2026-01-01 00:00:00'
            )
            """
        )
        manager.conn.commit()
    storage = AutomationStorage(manager)
    loaded = storage.get_task("t1")
    assert loaded is not None and loaded["session_mode"] == "per_run"
    assert loaded["missed_run_policy"] == "skip"
    storage.save_task({**loaded, "session_mode": "fixed"})
    assert storage.get_task("t1")["session_mode"] == "fixed"
    AutomationStorage(manager)
    assert storage.get_task("t1")["session_mode"] == "fixed"


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
    task_id = storage.save_task(_task(enabled=0))
    assert storage.get_task(task_id)["next_run_at"] is not None


def test_set_enabled_recomputes_next_run_on_enable(storage):
    task_id = storage.save_task(_task(trigger_type="daily", trigger_config={"time": "09:30"}))
    storage.set_next_run_raw(task_id, 1000.0)
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

    assert storage.finish_run(run_id, "error", "不应生效") is False
    assert storage.list_runs(task_id)[0]["status"] == "completed"


def test_set_run_session_rewrites_session_name(storage):
    task_id = storage.save_task(_task())
    run_id = new_automation_id("run")
    storage._insert_run_raw(run_id, task_id, "自动化任务", "queued")
    assert storage.set_run_session(run_id, "2026-11-11 每日站会摘要") is True
    assert storage.list_runs(task_id)[0]["session_name"] == "2026-11-11 每日站会摘要"
    assert storage.list_runs(task_id)[0]["status"] == "queued"
    assert storage.set_run_session("run-不存在", "x") is False


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
        time.sleep(0.001)
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
    assert storage.get_task(task["id"])["next_run_at"] > before


def test_claim_startup_only_on_first_iteration(storage):
    task_id = storage.save_task(_task(trigger_type="startup", trigger_config={}))
    first = storage.claim_due_tasks(time.time(), _compute(None), include_startup=True, catchup_enabled=False)
    assert len(first) == 1 and first[0][2] == "startup"
    assert first[0][0]["id"] == task_id
    second = storage.claim_due_tasks(time.time() + 1000, _compute(None), include_startup=True, catchup_enabled=False)
    assert second == []


def test_claim_startup_no_repeat_within_same_process(storage):
    task_id = storage.save_task(_task(trigger_type="startup", trigger_config={}))
    boot = time.time()
    first = storage.claim_due_tasks(
        boot, _compute(None), include_startup=True, catchup_enabled=False, startup_since=boot
    )
    assert len(first) == 1 and first[0][2] == "startup"
    assert first[0][0]["id"] == task_id
    storage.finish_run(first[0][1], "completed")
    again = storage.claim_due_tasks(
        boot + 1800, _compute(None), include_startup=True, catchup_enabled=False, startup_since=boot
    )
    assert again == []


def test_claim_startup_retriggers_on_next_process(storage):
    task_id = storage.save_task(_task(trigger_type="startup", trigger_config={}))
    boot1 = time.time()
    first = storage.claim_due_tasks(
        boot1, _compute(None), include_startup=True, catchup_enabled=False, startup_since=boot1
    )
    assert len(first) == 1 and first[0][2] == "startup"
    storage.finish_run(first[0][1], "completed")
    boot2 = boot1 + 86400
    second = storage.claim_due_tasks(
        boot2, _compute(None), include_startup=True, catchup_enabled=False, startup_since=boot2
    )
    assert len(second) == 1
    assert second[0][0]["id"] == task_id
    assert second[0][2] == "startup"


def test_catchup_skip_policy_records_and_reschedules(storage):
    task = _insert_due_task(storage, next_run_at=time.time() - 3600, missed_run_policy="skip")
    now = time.time()
    claimed = storage.claim_due_tasks(now, _compute(None), include_startup=True, catchup_enabled=True)
    assert claimed == []
    runs = storage.list_runs(task["id"])
    assert any(r["status"] == "skipped" and "过期" in r["error"] for r in runs)
    assert storage.get_task(task["id"])["next_run_at"] > now


def test_catchup_run_once_dispatches_once(storage):
    task = _insert_due_task(storage, next_run_at=time.time() - 3600, missed_run_policy="run_once")
    now = time.time()
    claimed = storage.claim_due_tasks(now, _compute(None), include_startup=True, catchup_enabled=True)
    assert len(claimed) == 1 and claimed[0][2] == "catchup"
    assert storage.get_task(task["id"])["next_run_at"] > now
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
        return now_ts

    claimed = storage.claim_due_tasks(now, bad_compute, include_startup=False, catchup_enabled=False)
    assert len(claimed) == 1
    assert storage.get_task(claimed[0][0]["id"])["next_run_at"] > now


def test_earliest_next_run(storage):
    assert storage.earliest_next_run() is None
    storage.save_task(_task(name="a", trigger_type="daily", trigger_config={"time": "23:59"}))
    storage.save_task(_task(name="b", trigger_type="daily", trigger_config={"time": "09:00"}))
    earliest = storage.earliest_next_run()
    assert earliest is not None and earliest > time.time()
    storage.save_task(_task(name="c", enabled=0, trigger_type="daily", trigger_config={"time": "00:00"}))
    assert storage.earliest_next_run() == pytest.approx(earliest)


def test_iso_now_and_new_id():
    value = iso_now()
    assert len(value) >= 19
    assert new_automation_id("run").startswith("run-")


def _once_task(**overrides) -> dict:
    base = {
        "trigger_type": "once",
        "trigger_config": {"delay_minutes": 8},
    }
    base.update(overrides)
    return _task(**base)


def test_save_once_normalizes_delay_minutes_to_run_at(storage):
    before = time.time()
    task_id = storage.save_task(_once_task())
    loaded = storage.get_task(task_id)

    config = loaded["trigger_config"]
    assert set(config) == {"run_at"}
    fire_ts = scheduling.parse_run_at(config["run_at"])
    assert before + 6 * 60 < fire_ts <= before + 8 * 60 + 1
    assert loaded["next_run_at"] == pytest.approx(fire_ts)


def test_resave_once_keeps_original_run_at(storage):
    task_id = storage.save_task(_once_task())
    first_next = storage.get_task(task_id)["next_run_at"]

    storage.save_task({**storage.get_task(task_id), "name": "改名"})
    assert storage.get_task(task_id)["next_run_at"] == first_next


def test_save_once_rejects_stale_run_at(storage):
    with pytest.raises(ValueError) as excinfo:
        storage.save_task(
            _once_task(trigger_config={"run_at": "2000-01-01 00:00"})
        )
    assert "一次性触发时刻已过" in str(excinfo.value)
    assert "delay_minutes" in str(excinfo.value)
    assert storage.count_tasks() == 0


def test_save_once_accepts_future_run_at(storage):
    future = datetime.fromtimestamp(time.time() + 3600).strftime("%Y-%m-%d %H:%M")
    task_id = storage.save_task(_once_task(trigger_config={"run_at": future}))
    assert storage.get_task(task_id)["next_run_at"] == pytest.approx(
        scheduling.parse_run_at(future)
    )


def test_claim_once_clears_next_run_and_does_not_refire(storage):
    now = time.time()
    future_run_at = datetime.fromtimestamp(now + 3600).strftime("%Y-%m-%d %H:%M")
    task = _insert_due_task(
        storage,
        next_run_at=now - 10,
        trigger_type="once",
        trigger_config={"run_at": future_run_at},
    )

    claimed = storage.claim_due_tasks(now, _compute(None), include_startup=False, catchup_enabled=False)
    assert len(claimed) == 1 and claimed[0][2] == "scheduled"
    assert storage.get_task(task["id"])["next_run_at"] is None
    assert storage.claim_due_tasks(now + 3600, _compute(None), include_startup=False, catchup_enabled=False) == []


def test_catchup_once_clears_next_run(storage):
    now = time.time()
    future_run_at = datetime.fromtimestamp(now + 3600).strftime("%Y-%m-%d %H:%M")
    task = _insert_due_task(
        storage,
        next_run_at=now - 3600,
        trigger_type="once",
        trigger_config={"run_at": future_run_at},
        missed_run_policy="run_once",
    )

    claimed = storage.claim_due_tasks(now, _compute(None), include_startup=True, catchup_enabled=True)
    assert len(claimed) == 1 and claimed[0][2] == "catchup"
    assert storage.get_task(task["id"])["next_run_at"] is None


_LEGACY_DDL = """
    CREATE TABLE automation_tasks (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        trigger_type TEXT NOT NULL,
        trigger_config TEXT NOT NULL DEFAULT '{}',
        target_session TEXT NOT NULL,
        prompt TEXT NOT NULL,
        permission_mode TEXT NOT NULL DEFAULT 'confirm',
        missed_run_policy TEXT NOT NULL DEFAULT 'skip',
        notify_on_success INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_run_at TEXT,
        next_run_at REAL,
        workdir TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT ''
    )
"""


def test_migration_adds_run_limit_columns_to_legacy_table(manager):
    with manager._lock:
        cursor = manager.conn.cursor()
        cursor.execute("DROP TABLE IF EXISTS automation_tasks")
        cursor.execute(_LEGACY_DDL)
        cursor.execute(
            """
            INSERT INTO automation_tasks (
                id, name, trigger_type, trigger_config, target_session, prompt,
                created_at, updated_at, next_run_at
            ) VALUES (
                'legacy-1', '旧任务', 'daily', '{"time": "09:30"}', '自动化任务', 'p',
                '2026-01-01 00:00:00', '2026-01-01 00:00:00', 1000.0
            )
            """
        )
        manager.conn.commit()

    storage = AutomationStorage(manager)
    with manager._lock:
        cursor = manager.conn.cursor()
        cols = {
            row[1]
            for row in cursor.execute("PRAGMA table_info(automation_tasks)").fetchall()
        }
    assert {"max_runs", "scheduled_run_count", "status"} <= cols

    loaded = storage.get_task("legacy-1")
    assert loaded is not None
    assert loaded["max_runs"] is None
    assert loaded["scheduled_run_count"] == 0
    assert loaded["status"] == "active"
    storage.save_task({**loaded, "name": "改名"})
    reloaded = storage.get_task("legacy-1")
    assert reloaded["name"] == "改名"
    assert reloaded["scheduled_run_count"] == 0
    assert reloaded["status"] == "active"


def _limited_task(storage, max_runs, **overrides) -> dict:
    task = _task(max_runs=max_runs, **overrides)
    task["id"] = storage.save_task(task)
    storage.set_next_run_raw(task["id"], time.time() - 10)
    return storage.get_task(task["id"])


def test_save_task_persists_max_runs(storage):
    task_id = storage.save_task(_task(max_runs=5))
    loaded = storage.get_task(task_id)
    assert loaded["max_runs"] == 5
    assert loaded["scheduled_run_count"] == 0
    assert loaded["status"] == "active"


def test_save_task_defaults_to_unlimited(storage):
    task_id = storage.save_task(_task())
    assert storage.get_task(task_id)["max_runs"] is None


@pytest.mark.parametrize("bad", [0, -3, 1.5, True, "3"])
def test_save_task_rejects_bad_max_runs(storage, bad):
    with pytest.raises(ValueError) as excinfo:
        storage.save_task(_task(max_runs=bad))
    assert "最多执行次数" in str(excinfo.value)
    assert storage.count_tasks() == 0


def test_claim_increments_scheduled_run_count(storage):
    task = _limited_task(storage, 3)
    claimed = storage.claim_due_tasks(
        time.time(), _compute(None), include_startup=False, catchup_enabled=False
    )
    assert len(claimed) == 1
    reloaded = storage.get_task(task["id"])
    assert reloaded["scheduled_run_count"] == 1
    assert reloaded["status"] == "active"


def test_edit_does_not_reset_run_count_or_status(storage):
    task = _limited_task(storage, 3)
    storage.claim_due_tasks(
        time.time(), _compute(None), include_startup=False, catchup_enabled=False
    )
    storage.save_task({**storage.get_task(task["id"]), "name": "改名", "max_runs": 10})
    reloaded = storage.get_task(task["id"])
    assert reloaded["name"] == "改名"
    assert reloaded["max_runs"] == 10
    assert reloaded["scheduled_run_count"] == 1
    assert reloaded["status"] == "active"


def _finish_queued_runs(storage, task_id) -> None:
    for run in storage.list_runs(task_id):
        if run["status"] == "queued":
            storage.finish_run(run["id"], "completed")


def test_max_runs_exhausted_marks_completed(storage):
    task = _limited_task(storage, 2)
    compute = _compute(None)
    for _ in range(2):
        storage.set_next_run_raw(task["id"], time.time() - 10)
        claimed = storage.claim_due_tasks(
            time.time(), compute, include_startup=False, catchup_enabled=False
        )
        assert len(claimed) == 1
        _finish_queued_runs(storage, task["id"])

    reloaded = storage.get_task(task["id"])
    assert reloaded["scheduled_run_count"] == 2
    assert reloaded["status"] == "completed"
    assert reloaded["next_run_at"] is None
    storage.set_next_run_raw(task["id"], time.time() - 10)
    assert storage.claim_due_tasks(
        time.time(), compute, include_startup=False, catchup_enabled=False
    ) == []


def test_completed_task_rejects_save(storage):
    task = _limited_task(storage, 1)
    storage.claim_due_tasks(
        time.time(), _compute(None), include_startup=False, catchup_enabled=False
    )
    assert storage.get_task(task["id"])["status"] == "completed"
    with pytest.raises(ValueError) as excinfo:
        storage.save_task({**storage.get_task(task["id"]), "name": "改名"})
    assert "任务已完成" in str(excinfo.value)
    assert storage.get_task(task["id"])["name"] == task["name"]


def test_completed_task_cannot_be_reenabled(storage):
    task = _limited_task(storage, 1)
    storage.claim_due_tasks(
        time.time(), _compute(None), include_startup=False, catchup_enabled=False
    )
    assert storage.set_enabled(task["id"], True) is False
    assert storage.set_enabled(task["id"], False) is False
    reloaded = storage.get_task(task["id"])
    assert reloaded["status"] == "completed"
    assert reloaded["next_run_at"] is None


def test_completed_task_refuses_manual_run(storage):
    task = _limited_task(storage, 1)
    claimed = storage.claim_due_tasks(
        time.time(), _compute(None), include_startup=False, catchup_enabled=False
    )
    storage.finish_run(claimed[0][1], "completed")
    assert storage.create_manual_run_if_idle(storage.get_task(task["id"])) is None


def test_earliest_next_run_skips_completed_task(storage):
    task = _limited_task(storage, 1)
    storage.claim_due_tasks(
        time.time(), _compute(None), include_startup=False, catchup_enabled=False
    )
    storage.set_next_run_raw(task["id"], time.time() - 10)
    assert storage.earliest_next_run() is None


def test_claim_once_marks_completed(storage):
    now = time.time()
    future_run_at = datetime.fromtimestamp(now + 3600).strftime("%Y-%m-%d %H:%M")
    task = _insert_due_task(
        storage,
        next_run_at=now - 10,
        trigger_type="once",
        trigger_config={"run_at": future_run_at},
    )
    claimed = storage.claim_due_tasks(
        now, _compute(None), include_startup=False, catchup_enabled=False
    )
    assert len(claimed) == 1
    reloaded = storage.get_task(task["id"])
    assert reloaded["status"] == "completed"
    assert reloaded["scheduled_run_count"] == 1
    assert reloaded["next_run_at"] is None


def test_catchup_once_skip_marks_completed(storage):
    now = time.time()
    future_run_at = datetime.fromtimestamp(now + 3600).strftime("%Y-%m-%d %H:%M")
    task = _insert_due_task(
        storage,
        next_run_at=now - 3600,
        trigger_type="once",
        trigger_config={"run_at": future_run_at},
        missed_run_policy="skip",
    )
    claimed = storage.claim_due_tasks(
        now, _compute(None), include_startup=True, catchup_enabled=True
    )
    assert claimed == []
    runs = storage.list_runs(task["id"])
    assert any(r["status"] == "skipped" and "过期" in r["error"] for r in runs)
    reloaded = storage.get_task(task["id"])
    assert reloaded["status"] == "completed"
    assert reloaded["next_run_at"] is None
