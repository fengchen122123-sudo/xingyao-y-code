"""秘书派活台账存储层（DispatchStore）单元测试。

覆盖：建表由 MemoryManager 负责 / create 返回正整数 id 与默认字段 /
mark_status 终态写摘要与时间戳 / failed 写错误 / list 过滤与 limit 夹紧 /
get 不存在返回 None / depends_on JSON 往返 / 连接异常时 create 返回 None 不抛。
"""

import os
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.memory import MemoryManager
from core.secretary_dispatches import (
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    DispatchStore,
)


@pytest.fixture
def manager(tmp_path):
    m = MemoryManager(str(tmp_path / "dispatches.db"))
    try:
        yield m
    finally:
        try:
            m.conn.close()
        except Exception:
            pass


@pytest.fixture
def store(manager):
    return DispatchStore(manager)


def _create(store, **overrides):
    params = dict(
        secretary_session="sec_1",
        target_session="proj_a",
        target_display="项目甲",
        workdir="D:/proj/a",
        title="补 README",
        prompt="把 README 补全",
        status=STATUS_RUNNING,
    )
    params.update(overrides)
    return store.create(**params)


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------

def test_table_and_indexes_created(manager):
    names = {
        row[0]
        for row in manager.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall()
    }
    assert "secretary_dispatches" in names
    assert "idx_secretary_dispatches_target" in names
    assert "idx_secretary_dispatches_secretary" in names


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_create_returns_positive_id_and_defaults(store):
    dispatch_id = _create(store)
    assert isinstance(dispatch_id, int)
    assert dispatch_id > 0

    row = store.get(dispatch_id)
    assert row is not None
    assert row["secretary_session"] == "sec_1"
    assert row["target_session"] == "proj_a"
    assert row["target_display"] == "项目甲"
    assert row["workdir"] == "D:/proj/a"
    assert row["title"] == "补 README"
    assert row["prompt"] == "把 README 补全"
    assert row["status"] == STATUS_RUNNING
    assert row["depends_on"] == []
    assert row["batch_id"] == ""
    assert row["started_at"], "create 必须自动填 started_at"
    assert row["finished_at"] == ""
    assert row["result_excerpt"] == ""
    assert row["error"] == ""


def test_create_accepts_queued_status_and_depends_on(store):
    dispatch_id = _create(
        store,
        status=STATUS_QUEUED,
        depends_on=[1, 2],
        batch_id="batch_1",
    )
    row = store.get(dispatch_id)
    assert row["status"] == STATUS_QUEUED
    assert row["depends_on"] == [1, 2]
    assert row["batch_id"] == "batch_1"
    assert row["finished_at"] == ""


# ---------------------------------------------------------------------------
# mark_status
# ---------------------------------------------------------------------------

def test_mark_done_writes_excerpt_and_finished_at(store):
    dispatch_id = _create(store)
    store.mark_status(dispatch_id, STATUS_DONE, result_excerpt="已补全 README 并跑通测试")

    row = store.get(dispatch_id)
    assert row["status"] == STATUS_DONE
    assert row["result_excerpt"] == "已补全 README 并跑通测试"
    assert row["finished_at"], "终态必须写 finished_at"


def test_mark_failed_writes_error(store):
    dispatch_id = _create(store)
    store.mark_status(dispatch_id, STATUS_FAILED, error="余额不足")

    row = store.get(dispatch_id)
    assert row["status"] == STATUS_FAILED
    assert row["error"] == "余额不足"
    assert row["finished_at"]


def test_mark_running_keeps_finished_at_empty(store):
    dispatch_id = _create(store)
    store.mark_status(dispatch_id, STATUS_DONE, result_excerpt="first")
    # 回到非终态时 finished_at 应被清空（running/queued 不留完成时刻）
    store.mark_status(dispatch_id, STATUS_RUNNING)

    row = store.get(dispatch_id)
    assert row["status"] == STATUS_RUNNING
    assert row["finished_at"] == ""


def test_mark_status_on_missing_id_does_not_raise(store):
    # 不存在的 id：静默忽略
    store.mark_status(999999, STATUS_DONE)
    assert store.get(999999) is None


# ---------------------------------------------------------------------------
# list 过滤与 limit
# ---------------------------------------------------------------------------

def test_list_filters_by_status_and_workdir(store):
    a1 = _create(store, workdir="D:/proj/a", status=STATUS_RUNNING)
    a2 = _create(store, workdir="D:/proj/a", status=STATUS_DONE)
    b1 = _create(store, workdir="D:/proj/b", status=STATUS_DONE)

    done_rows = store.list(status=STATUS_DONE)
    assert {row["id"] for row in done_rows} == {a2, b1}

    a_rows = store.list(workdir="D:/proj/a")
    assert [row["id"] for row in a_rows] == [a2, a1]  # 按 id 倒序

    both = store.list(status=STATUS_DONE, workdir="D:/proj/b")
    assert [row["id"] for row in both] == [b1]


def test_list_filters_by_target_and_secretary(store):
    _create(store, target_session="proj_a", secretary_session="sec_1")
    _create(store, target_session="proj_b", secretary_session="sec_2")

    assert len(store.list(target_session="proj_b")) == 1
    assert len(store.list(secretary_session="sec_1")) == 1
    assert len(store.list(secretary_session="sec_1", target_session="proj_b")) == 0


def test_list_limit_clamped_to_50(store):
    for _ in range(55):
        _create(store)

    assert len(store.list(limit=999)) == 50
    assert len(store.list(limit=0)) == 1  # 下限夹紧到 1
    assert len(store.list()) == 20  # 默认 20


def test_list_target_like_matches_session_or_display_substring(store):
    """target_like 按会话名 / 显示名子串命中（SQL LIKE，两列任一命中即可）。"""
    alpha = _create(store, target_session="proj_alpha", target_display="项目甲")
    beta = _create(store, target_session="session_beta", target_display="项目乙")
    gamma = _create(store, target_session="other_gamma", target_display="项目丙")

    # 会话名子串
    assert [row["id"] for row in store.list(target_like="alpha")] == [alpha]
    assert [row["id"] for row in store.list(target_like="gamma")] == [gamma]
    # 显示名子串
    assert [row["id"] for row in store.list(target_like="项目乙")] == [beta]
    # 两列共有的子串：三条全中，仍按 id 倒序
    assert [row["id"] for row in store.list(target_like="项目")] == [gamma, beta, alpha]


def test_list_target_like_no_hit_returns_empty(store):
    """不命中的关键词返回空列表（不是全量倾倒）。"""
    _create(store)
    assert store.list(target_like="压根不存在的会话") == []


def test_list_target_like_combines_with_status(store):
    """target_like 与 status 是同一条 SQL 的 AND 条件，叠加生效。"""
    done_id = _create(store, target_session="proj_alpha", status=STATUS_DONE)
    _create(store, target_session="proj_alpha", status=STATUS_RUNNING)
    _create(store, target_session="proj_beta", status=STATUS_DONE)

    assert [
        row["id"] for row in store.list(target_like="alpha", status=STATUS_DONE)
    ] == [done_id]
    assert store.list(target_like="alpha", status=STATUS_FAILED) == []


def test_list_target_like_filters_before_limit(store):
    """过滤先于 LIMIT：台账超过 50 条时，更早的命中项依然取得到。"""
    oldest = _create(store, target_session="proj_alpha_old", target_display="老项目")
    for _ in range(55):
        _create(store, target_session="proj_noise", target_display="噪音项目")

    rows = store.list(target_like="老项目", limit=50)
    assert [row["id"] for row in rows] == [oldest]


def test_list_keeps_exact_target_session_matching(store):
    """新增 target_like 不能动原有的 target_session 精确匹配。"""
    exact = _create(store, target_session="proj_a", target_display="项目甲")
    _create(store, target_session="proj_a_2", target_display="项目甲二")

    assert [row["id"] for row in store.list(target_session="proj_a")] == [exact]
    # 精确匹配不吃子串：proj_a 不应命中 proj_a_2
    assert len(store.list(target_session="proj_a")) == 1


# ---------------------------------------------------------------------------
# depends_on / 依赖查询
# ---------------------------------------------------------------------------

def test_depends_on_roundtrip(store):
    dispatch_id = _create(store, depends_on=[1, 2])
    row = store.get(dispatch_id)
    assert row["depends_on"] == [1, 2]


def test_dependents_of_and_unlocked_dependents(store):
    upstream_a = _create(store, title="上游 A", status=STATUS_DONE)
    upstream_b = _create(store, title="上游 B", status=STATUS_RUNNING)
    child = _create(store, title="下游", depends_on=[upstream_a, upstream_b])

    dependents = store.dependents_of(upstream_a)
    assert [row["id"] for row in dependents] == [child]

    # 上游 B 还没 done → 下游不应解锁
    assert store.unlocked_dependents(upstream_a) == []

    store.mark_status(upstream_b, STATUS_DONE)
    unlocked = store.unlocked_dependents(upstream_a)
    assert [row["id"] for row in unlocked] == [child]


# ---------------------------------------------------------------------------
# 异常兜底：台账故障不得外抛
# ---------------------------------------------------------------------------

class _BrokenConn:
    """任何访问都抛异常的假连接。"""

    def cursor(self):
        raise sqlite3.ProgrammingError("database is closed")

    def execute(self, *args, **kwargs):
        raise sqlite3.ProgrammingError("database is closed")

    def commit(self):
        raise sqlite3.ProgrammingError("database is closed")


class _BrokenManager:
    def __init__(self):
        self.conn = _BrokenConn()
        self._lock = threading.RLock()

    def acquire_lock(self):
        return self._lock


def test_create_returns_none_when_conn_raises():
    store = DispatchStore(_BrokenManager())
    # 不抛异常，只返回 None
    assert store.create(
        secretary_session="sec",
        target_session="proj",
        target_display="项目甲",
        workdir="D:/proj/a",
        title="t",
        prompt="p",
        status=STATUS_RUNNING,
    ) is None


def test_other_methods_swallow_errors():
    store = DispatchStore(_BrokenManager())
    assert store.get(1) is None
    assert store.list() == []
    assert store.dependents_of(1) == []
    assert store.unlocked_dependents(1) == []
    store.mark_status(1, STATUS_DONE)  # 不抛即通过


def test_manager_without_table_auto_creates_it(tmp_path):
    """台账表缺失时（进程早于升级启动、连接里还没有新表）首次访问会自动补建。"""
    manager = MemoryManager(str(tmp_path / "bare.db"))
    try:
        manager.conn.execute("DROP TABLE IF EXISTS secretary_dispatches")
        manager.conn.commit()
        store = DispatchStore(manager)
        # 补建前的读操作：不抛、返回空值
        assert store.list() == []
        assert store.get(1) is None
        # 写操作触发补建，而且真的写得进去（不再静默失效）
        new_id = store.create(
            secretary_session="sec",
            target_session="proj",
            target_display="",
            workdir="",
            title="t",
            prompt="p",
            status=STATUS_CANCELLED,
        )
        assert isinstance(new_id, int) and new_id > 0
        tables = {
            row[0]
            for row in manager.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "secretary_dispatches" in tables
        row = store.get(new_id)
        assert row is not None and row["status"] == STATUS_CANCELLED
    finally:
        manager.conn.close()


def test_constants_exposed():
    assert STATUS_QUEUED == "queued"
    assert STATUS_RUNNING == "running"
    assert STATUS_DONE == "done"
    assert STATUS_FAILED == "failed"
    assert STATUS_CANCELLED == "cancelled"
    assert STATUS_BLOCKED == "blocked"
