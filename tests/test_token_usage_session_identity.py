"""同名会话的 token 流水隔离回归测试。

背景：token_usage 按 session_name 记账，而会话删除后名字会被释放，新会话
可以复用同一个名字。若按名字聚合时不叠加「会话创建时间下界」，新会话就会
继承同名旧会话的历史流水——2026-09-29 状态行显示 3200 万输入即此因
（8-23 一个同名会话已删除，残留 227 行 3226 万 prompt 被今天的新会话算入）。

同时验证会话删除会连带清理流水，避免继续产生孤儿行。
"""

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
    """会话创建前写入的同名流水不得计入新会话累计。"""
    manager = MemoryManager(":memory:")
    _add_usage(manager, ts=int(time.time()) - 3600, scope="main")
    _add_usage(manager, ts=int(time.time()) - 1800, scope="keepalive")

    manager.create_session_explicit(SESSION_NAME)

    totals = manager.get_token_usage_by_session(SESSION_NAME)
    assert totals["prompt"] == 0
    assert totals["requests"] == 0
    # 保活单列同样不能被旧同名流水污染
    assert totals["keepalive_prompt"] == 0
    assert totals["keepalive_requests"] == 0


def test_new_session_counts_only_its_own_rows():
    """创建时间之后写入的流水必须正常计入。"""
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
    """最近一轮用量基线不得取到同名旧会话的残留行。"""
    manager = MemoryManager(":memory:")
    _add_usage(manager, ts=int(time.time()) - 3600, prompt=88888)

    manager.create_session_explicit(SESSION_NAME)
    assert manager.get_last_token_usage(SESSION_NAME) is None

    _add_usage(manager, ts=int(time.time()), prompt=123, completion=4)
    assert manager.get_last_token_usage(SESSION_NAME)["prompt_tokens"] == 123


def test_list_token_usage_by_sessions_ignores_earlier_residue():
    """会话弹层列表同样只统计本会话创建之后的流水。"""
    manager = MemoryManager(":memory:")
    _add_usage(manager, ts=int(time.time()) - 3600, prompt=77777)
    manager.create_session_explicit(SESSION_NAME)

    # 全部流水都被下界过滤时，该会话不应带着旧流水出现在列表里
    sessions = manager.list_token_usage_by_sessions(limit=20)
    residue = next(
        (item for item in sessions if item["session_id"] == SESSION_NAME), None
    )
    assert residue is None or residue["prompt"] == 0

    # 本会话自己的流水进列表后，量只算自己的
    _add_usage(manager, ts=int(time.time()), prompt=250, completion=3)
    row = next(
        item
        for item in manager.list_token_usage_by_sessions(limit=20)
        if item["session_id"] == SESSION_NAME
    )
    assert row["prompt"] == 250
    assert row["requests"] == 1


def test_delete_session_removes_token_usage_rows():
    """删除会话必须连带清理按名字记账的流水，避免留下孤儿行。"""
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


def test_delete_sessions_by_workdir_removes_token_usage_rows(tmp_path):
    """按工作目录批量删除会话时同样清理流水。"""
    workdir = str(tmp_path)
    manager = MemoryManager(":memory:")
    names = ["workspace-token-a", "workspace-token-b"]
    for name in names:
        manager.create_session_explicit(name, workdir=workdir)
        _add_usage(manager, ts=int(time.time()), session_id=name)

    manager.delete_sessions_by_workdir(workdir)

    cursor = manager.conn.execute("SELECT COUNT(*) FROM token_usage")
    assert cursor.fetchone()[0] == 0


def test_session_without_created_at_keeps_legacy_behavior():
    """历史会话 created_at 缺失时不过滤，保持既有统计口径不变。"""
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
    """下界换算必须与写入侧 int(time.time()) 的本地 epoch 口径一致。

    created_at 由 Python 本地时间写入，SQLite 的 'utc' 修饰符必须按系统
    本地时区把它换算成 epoch；若某个环境的 SQLite 拿不到本地时区，这条
    断言会立刻暴露（否则下界会整体偏移一个时区，静默多算或少算流水）。
    """
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
