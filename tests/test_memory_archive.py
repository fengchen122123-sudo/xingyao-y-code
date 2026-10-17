"""对话归档（conversation archive）数据层回归测试。

对应设计文档 docs/2026-10-11-conversation-archive-design.md 第 4/5/8/10 节：
- archive_session / unarchive_session 的正常、幂等、会话不存在路径；
- get_all_sessions 三种模式（默认不含归档 / include_archived / archived_only）；
- 归档只写 sessions.archived_at，不触碰 messages / session_summary_generations /
  token_usage（真实 SQLite 断言，不用 mock）；
- 旧库迁移幂等：ALTER ADD COLUMN 而非 DROP 重建，原有数据不丢；
- search_sessions_and_messages 的归档过滤开关。
"""

import sqlite3
from datetime import datetime

import pytest

from core.memory import MemoryManager


@pytest.fixture
def manager(tmp_path):
    mm = MemoryManager(str(tmp_path / "archive.db"))
    yield mm
    mm.conn.close()


def _make_session(mm, name, workdir="GLOBAL", source="GUI", text="hello"):
    """建一个带一条消息的会话，避免被 include_empty 过滤掉。"""
    mm.ensure_session(name, workdir=workdir, source=source)
    mm.add_message(name, "user", text)
    return name


def _archived_at_of(mm, name):
    row = mm.conn.execute(
        "SELECT archived_at FROM sessions WHERE session_name = ?", (name,)
    ).fetchone()
    return row[0] if row else "NO_SUCH_SESSION"


def _count(mm, sql, params=()):
    return mm.conn.execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------- 归档 / 取消归档

def test_archive_session_sets_non_empty_iso8601_archived_at(manager):
    _make_session(manager, "chat")

    assert manager.archive_session("chat") is True

    archived_at = _archived_at_of(manager, "chat")
    assert isinstance(archived_at, str) and archived_at.strip()
    # 必须是可解析的 ISO8601 时间（对齐 created_at / updated_at 的 TEXT 风格）
    assert datetime.fromisoformat(archived_at) is not None


def test_archive_session_is_idempotent_and_keeps_original_timestamp(manager):
    _make_session(manager, "chat")

    assert manager.archive_session("chat", archived_at="2020-01-01T00:00:00") is True
    assert _archived_at_of(manager, "chat") == "2020-01-01T00:00:00"

    # 再次归档：幂等成功，且不覆盖原归档时刻
    assert manager.archive_session("chat") is True
    assert _archived_at_of(manager, "chat") == "2020-01-01T00:00:00"


def test_unarchive_session_restores_and_is_idempotent(manager):
    _make_session(manager, "chat")
    manager.archive_session("chat")

    assert manager.unarchive_session("chat") is True
    assert _archived_at_of(manager, "chat") is None
    assert {s["name"] for s in manager.get_all_sessions(include_empty=True)} == {"chat"}

    # 对未归档会话再次取消归档：同样安全返回 True
    assert manager.unarchive_session("chat") is True
    assert _archived_at_of(manager, "chat") is None


def test_archive_and_unarchive_missing_session_return_false(manager):
    assert manager.archive_session("ghost") is False
    assert manager.unarchive_session("ghost") is False
    # 不存在的会话不会被误建记录
    assert not any(
        s["name"] == "ghost"
        for s in manager.get_all_sessions(include_empty=True, include_archived=True)
    )


# ---------------------------------------------------------------- 列表过滤

def test_get_all_sessions_archive_modes(manager):
    _make_session(manager, "active")
    _make_session(manager, "done")
    assert manager.archive_session("done") is True

    # 默认：不显示归档会话（既有 10+ 处调用点行为不变）
    default_names = {s["name"] for s in manager.get_all_sessions()}
    assert default_names == {"active"}

    # include_archived=True：返回全部
    all_names = {s["name"] for s in manager.get_all_sessions(include_archived=True)}
    assert all_names == {"active", "done"}

    # archived_only=True：只返回归档
    archived_names = {s["name"] for s in manager.get_all_sessions(archived_only=True)}
    assert archived_names == {"done"}


def test_get_all_sessions_exposes_archived_at_key(manager):
    _make_session(manager, "active")
    _make_session(manager, "done")
    manager.archive_session("done")

    rows = {s["name"]: s for s in manager.get_all_sessions(include_archived=True)}
    assert rows["active"]["archived_at"] is None
    assert rows["done"]["archived_at"] is not None


# ---------------------------------------------------------------- 零副作用不变量

def test_archive_only_writes_archived_at_column(manager):
    name = _make_session(manager, "chat", text="需要保留的历史")
    manager.archive_summary_generation(name, "第一代摘要", topics="t", until_msg_id=1)
    with manager.acquire_lock():
        manager.conn.execute(
            "INSERT INTO token_usage (ts, date, provider, model, session_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "2026-10-11", "p", "m", name),
        )
        manager.conn.commit()

    before = {
        "messages": _count(manager, "SELECT COUNT(*) FROM messages"),
        "summaries": _count(
            manager, "SELECT COUNT(*) FROM session_summary_generations"
        ),
        "token_usage": _count(manager, "SELECT COUNT(*) FROM token_usage"),
        "session_row": manager.conn.execute(
            "SELECT memory_summary, summarized_until_msg_id, updated_at, created_at "
            "FROM sessions WHERE session_name = ?",
            (name,),
        ).fetchone(),
    }

    assert manager.archive_session(name) is True

    assert _count(manager, "SELECT COUNT(*) FROM messages") == before["messages"]
    assert (
        _count(manager, "SELECT COUNT(*) FROM session_summary_generations")
        == before["summaries"]
    )
    assert _count(manager, "SELECT COUNT(*) FROM token_usage") == before["token_usage"]
    assert (
        manager.conn.execute(
            "SELECT memory_summary, summarized_until_msg_id, updated_at, created_at "
            "FROM sessions WHERE session_name = ?",
            (name,),
        ).fetchone()
        == before["session_row"]
    )

    # 归档 ≠ 删除：历史仍可完整读取
    assert [m["content"] for m in manager.get_history(name)] == ["需要保留的历史"]


# ---------------------------------------------------------------- 搜索过滤

def test_search_excludes_archived_by_default(manager):
    _make_session(manager, "keyword_active", text="共享关键词 alpha")
    _make_session(manager, "keyword_done", text="共享关键词 alpha")
    manager.archive_session("keyword_done")

    default_hits = manager.search_sessions_and_messages("keyword", scope="all")
    assert {r["session_name"] for r in default_hits} == {"keyword_active"}

    all_hits = manager.search_sessions_and_messages(
        "keyword", scope="all", include_archived=True
    )
    assert {r["session_name"] for r in all_hits} == {"keyword_active", "keyword_done"}

    # 内容检索（scope=content）同样默认排除归档会话
    content_hits = manager.search_sessions_and_messages("alpha", scope="content")
    assert {r["session_name"] for r in content_hits} == {"keyword_active"}
    content_hits_all = manager.search_sessions_and_messages(
        "alpha", scope="content", include_archived=True
    )
    assert {r["session_name"] for r in content_hits_all} == {
        "keyword_active",
        "keyword_done",
    }


# ---------------------------------------------------------------- 迁移幂等

def _make_legacy_db(db_path):
    """构造一个不含 archived_at 列的旧库（sessions + messages 各有数据）。"""
    conn = sqlite3.connect(db_path)
    conn.execute('''
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_name TEXT UNIQUE,
            memory_summary TEXT,
            summarized_until_msg_id INTEGER
        )
    ''')
    conn.execute('''
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            role TEXT,
            content TEXT
        )
    ''')
    conn.execute(
        "INSERT INTO sessions (session_name, memory_summary, summarized_until_msg_id) "
        "VALUES (?, ?, ?)",
        ("legacy_chat", "旧摘要", 7),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content) VALUES (1, 'user', '旧消息')"
    )
    conn.commit()
    conn.close()


def test_migration_adds_archived_at_without_rebuild(tmp_path):
    db_path = tmp_path / "legacy_archive.db"
    _make_legacy_db(db_path)

    manager = MemoryManager(str(db_path))
    try:
        columns = {
            row[1]
            for row in manager.conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        assert "archived_at" in columns

        # 走的是 ALTER 而非 DROP 重建：原有会话与消息数据完整保留
        row = manager.conn.execute(
            "SELECT session_name, memory_summary, summarized_until_msg_id "
            "FROM sessions WHERE id = 1"
        ).fetchone()
        assert row == ("legacy_chat", "旧摘要", 7)
        assert _count(manager, "SELECT COUNT(*) FROM messages") == 1

        # 迁移后归档能力可用
        assert manager.archive_session("legacy_chat") is True
        assert _archived_at_of(manager, "legacy_chat") is not None

        # 迁移幂等：再次建表不报错、不丢数据、不重置归档态
        archived_at = _archived_at_of(manager, "legacy_chat")
        manager.create_tables()
        assert _archived_at_of(manager, "legacy_chat") == archived_at
        assert _count(manager, "SELECT COUNT(*) FROM messages") == 1
    finally:
        manager.conn.close()
