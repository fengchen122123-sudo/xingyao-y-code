import time

from core.memory import MemoryManager

SESSION_NAME = "拉取一下最新的代码"


def _add_usage(manager, *, ts, prompt=1000, completion=10, cached=800,
               session_id=SESSION_NAME, scope="main"):
    manager.add_token_usage(
        provider="openai",
        model="deepseek-v4-pro-0813",
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        total_tokens=prompt + completion,
        session_id=session_id,
        ts=ts,
        cache_scope=scope,
    )


def test_new_session_does_not_inherit_same_name_residue():
    manager = MemoryManager(":memory:")
    _add_usage(manager, ts=int(time.time()) - 3600, scope="main")
    _add_usage(manager, ts=int(time.time()) - 1800, scope="keepalive")

    manager.create_session_explicit(SESSION_NAME)

    totals = manager.get_token_usage_by_session(SESSION_NAME)
    assert totals["prompt"] == 0
    assert totals["requests"] == 0
    assert totals["keepalive_prompt"] == 0
    assert totals["keepalive_requests"] == 0


def test_new_session_counts_only_its_own_rows():
    manager = MemoryManager(":memory:")
    _add_usage(manager, ts=int(time.time()) - 3600, prompt=9000)
    manager.create_session_explicit(SESSION_NAME)
    _add_usage(manager, ts=int(time.time()), prompt=500, completion=7, cached=400)

    totals = manager.get_token_usage_by_session(SESSION_NAME)
    assert totals["prompt"] == 500
    assert totals["completion"] == 7
    assert totals["cached"] == 400
    assert totals["requests"] == 1


def test_get_last_token_usage_ignores_earlier_residue():
    manager = MemoryManager(":memory:")
    _add_usage(manager, ts=int(time.time()) - 3600, prompt=88888)

    manager.create_session_explicit(SESSION_NAME)
    assert manager.get_last_token_usage(SESSION_NAME) is None

    _add_usage(manager, ts=int(time.time()), prompt=123, completion=4)
    assert manager.get_last_token_usage(SESSION_NAME)["prompt_tokens"] == 123


def test_list_token_usage_by_sessions_ignores_earlier_residue():
    manager = MemoryManager(":memory:")
    _add_usage(manager, ts=int(time.time()) - 3600, prompt=77777)
    manager.create_session_explicit(SESSION_NAME)

    sessions = manager.list_token_usage_by_sessions(limit=20)
    residue = next(
        (item for item in sessions if item["session_id"] == SESSION_NAME), None
    )
    assert residue is None or residue["prompt"] == 0

    _add_usage(manager, ts=int(time.time()), prompt=250, completion=3)
    row = next(
        item
        for item in manager.list_token_usage_by_sessions(limit=20)
        if item["session_id"] == SESSION_NAME
    )
    assert row["prompt"] == 250
    assert row["requests"] == 1


def test_delete_session_keeps_token_usage_in_statistics():
    manager = MemoryManager(":memory:")
    manager.create_session_explicit(SESSION_NAME)
    _add_usage(manager, ts=int(time.time()), prompt=321, completion=5)

    assert manager.get_token_usage_by_session(SESSION_NAME)["requests"] == 1

    manager.delete_session(SESSION_NAME)

    cursor = manager.conn.execute(
        "SELECT COUNT(*) FROM token_usage WHERE session_id = ?", (SESSION_NAME,)
    )
    assert cursor.fetchone()[0] == 0
    assert manager.get_token_usage_by_session(SESSION_NAME)["prompt"] == 0

    tombstones = manager.conn.execute(
        "SELECT COUNT(*) FROM token_usage WHERE session_id LIKE 'deleted::%'"
    ).fetchone()[0]
    assert tombstones == 1
    assert manager.get_token_usage_totals()["prompt"] == 321

    today = time.strftime("%Y-%m-%d")
    day_rows = manager.query_token_usage_by_day_and_model(today, today)
    assert sum(row["prompt"] for row in day_rows) == 321
    assert sum(row["requests"] for row in day_rows) == 1


def test_deleted_session_rows_not_inherited_by_same_name_new_session():
    manager = MemoryManager(":memory:")
    manager.create_session_explicit(SESSION_NAME)
    _add_usage(manager, ts=int(time.time()), prompt=9000)
    manager.delete_session(SESSION_NAME)

    manager.create_session_explicit(SESSION_NAME)
    _add_usage(manager, ts=int(time.time()), prompt=12, completion=1)

    assert manager.get_token_usage_by_session(SESSION_NAME)["prompt"] == 12
    listed = {item["session_id"] for item in manager.list_token_usage_by_sessions(limit=20)}
    assert SESSION_NAME in listed
    assert all(not name.startswith("deleted::") for name in listed)
    assert manager.get_token_usage_totals()["prompt"] == 9012


def test_delete_sessions_by_workdir_keeps_token_usage_in_statistics(tmp_path):
    workdir = str(tmp_path)
    manager = MemoryManager(":memory:")
    names = ["workspace-token-a", "workspace-token-b"]
    for name in names:
        manager.create_session_explicit(name, workdir=workdir)
        _add_usage(manager, ts=int(time.time()), session_id=name)

    manager.delete_sessions_by_workdir(workdir)

    cursor = manager.conn.execute("SELECT COUNT(*) FROM token_usage")
    assert cursor.fetchone()[0] == 2
    tombstones = manager.conn.execute(
        "SELECT COUNT(*) FROM token_usage WHERE session_id LIKE 'deleted::%'"
    ).fetchone()[0]
    assert tombstones == 2
    assert manager.get_token_usage_totals()["requests"] == 2


def test_session_without_created_at_keeps_legacy_behavior():
    manager = MemoryManager(":memory:")
    _add_usage(manager, ts=int(time.time()) - 3600, prompt=4567)
    manager.create_session_explicit(SESSION_NAME)
    manager.conn.execute(
        "UPDATE sessions SET created_at = NULL WHERE session_name = ?",
        (SESSION_NAME,),
    )

    totals = manager.get_token_usage_by_session(SESSION_NAME)
    assert totals["prompt"] == 4567
    assert totals["requests"] == 1


def test_created_at_bound_matches_local_epoch_convention():
    manager = MemoryManager(":memory:")
    manager.create_session_explicit(SESSION_NAME)
    created_at = manager.conn.execute(
        "SELECT created_at FROM sessions WHERE session_name = ?", (SESSION_NAME,)
    ).fetchone()[0]

    expected = int(time.mktime(time.strptime(created_at, "%Y-%m-%d %H:%M:%S")))
    actual = manager.conn.execute(
        "SELECT CAST(strftime('%s', ?, 'utc') AS INTEGER)", (created_at,)
    ).fetchone()[0]

    assert actual == expected
