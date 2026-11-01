"""编辑已发送消息的存储层支撑：会话截断（rewind_to_message）。

编辑一条历史用户消息意味着「从这条重新分叉」：该条及其之后的全部记录都要
消失；被压缩的摘要与多代归档也随之失真，必须同事务作废，否则模型会在新回合
里继续看到被作废的旧结论。
"""

import pytest

from core.memory import MemoryManager


@pytest.fixture
def manager():
    m = MemoryManager(":memory:")
    m.create_session_explicit("edit-session", workdir="GLOBAL", source="GUI")
    return m


def _seed(manager, session="edit-session"):
    """写入 4 条消息，返回按写入顺序排列的 (id, role, content)。"""
    rows = [
        ("user", "第一条提问"),
        ("assistant", "第一条回答"),
        ("user", "第二条提问"),
        ("assistant", "第二条回答"),
    ]
    for role, text in rows:
        manager.add_message(session, role, text)
    return [
        (m["_msg_id"], m["role"], m["content"])
        for m in manager.get_history_with_ids(session)
    ]


def test_rewind_deletes_target_and_later_messages(manager):
    rows = _seed(manager)

    result = manager.rewind_to_message("edit-session", rows[2][0])

    assert result == {"role": "user", "removed": 2}
    left = manager.get_history_with_ids("edit-session")
    assert [m["content"] for m in left] == ["第一条提问", "第一条回答"]


def test_rewind_keeps_messages_before_target(manager):
    rows = _seed(manager)

    manager.rewind_to_message("edit-session", rows[1][0])

    left = manager.get_history_with_ids("edit-session")
    assert [m["content"] for m in left] == ["第一条提问"]


def test_rewind_unknown_message_returns_none(manager):
    _seed(manager)

    assert manager.rewind_to_message("edit-session", 999999) is None
    assert len(manager.get_history_with_ids("edit-session")) == 4


def test_rewind_does_not_touch_other_sessions(manager):
    other = "other-session"
    manager.create_session_explicit(other, workdir="GLOBAL", source="GUI")
    rows = _seed(manager)
    manager.add_message(other, "user", "别的会话")

    assert manager.rewind_to_message(other, rows[1][0]) is None
    assert len(manager.get_history_with_ids("edit-session")) == 4
    assert len(manager.get_history_with_ids(other)) == 1


def test_rewind_clears_summary_and_archive(manager):
    rows = _seed(manager)
    boundary = rows[1][0]
    cursor = manager.conn.cursor()
    cursor.execute(
        "UPDATE sessions SET memory_summary = ?, summarized_until_msg_id = ? "
        "WHERE session_name = ?",
        ("旧摘要", boundary, "edit-session"),
    )
    manager.conn.commit()
    manager.archive_summary_generation("edit-session", "旧摘要", "旧主题", boundary)

    manager.rewind_to_message("edit-session", rows[2][0])

    cursor.execute(
        "SELECT memory_summary, summarized_until_msg_id FROM sessions "
        "WHERE session_name = ?",
        ("edit-session",),
    )
    summary, until = cursor.fetchone()
    assert summary is None
    assert until == 0
    cursor.execute(
        "SELECT COUNT(*) FROM session_summary_generations WHERE session_name = ?",
        ("edit-session",),
    )
    assert cursor.fetchone()[0] == 0


def test_last_message_id_tracks_newest_row(manager):
    """编辑入口靠它把数据库 id 带回前端（无消息时返回 0）。"""
    rows = _seed(manager)

    assert manager.get_last_message_id("edit-session") == rows[-1][0]
    assert manager.get_last_message_id("no-such-session") == 0
