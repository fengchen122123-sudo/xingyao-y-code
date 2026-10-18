"""消息全文检索回归（FTS5 + CJK bigram 归一化，懒同步，片段脱敏，不可用降级）。

覆盖不变量：
1. 中文两字词/三字词/英文都能命中（中文不能依赖 FTS5 内置分词）；
2. 跨会话可检索，session_name= 限定会话；
3. 增量（懒）同步：写入消息后不 rebuild 也能立刻搜到；
4. rebuild_message_index 返回条数 > 0 且重建后仍可命中；
5. 已删除消息不出现（靠 JOIN messages 排除孤儿索引行）；
6. 片段脱敏：历史消息里的 sk- 密钥不得经片段再次外泄；
7. 空查询返回 []（不是 None）；
8. FTS5 不可用时 search_messages 返回 None、search_sessions 给出「不可用」提示。
"""

import pytest

import core.tools as tools
from core.memory import MemoryManager

SECRET = "sk-abcdefghijklmnop123456"


@pytest.fixture
def manager():
    m = MemoryManager(":memory:")
    yield m
    m.close()


def _indexed_max_rowid(manager):
    with manager._lock:
        row = manager.conn.execute("SELECT MAX(rowid) FROM messages_fts").fetchone()
    return int((row[0] if row else 0) or 0)


def _message_count(manager):
    with manager._lock:
        row = manager.conn.execute("SELECT COUNT(*) FROM messages").fetchone()
    return int(row[0] or 0)


@pytest.fixture
def seeded(manager):
    """三个会话的消息，覆盖中文两字/三字词与英文词。"""
    manager.add_message("search-a", "user", "我们讨论一下缓存策略，先看现状。")
    manager.add_message("search-a", "assistant", "缓存命中率提升了 12%，主要靠前缀复用。")
    manager.add_message("search-b", "user", "enable prefix caching for prompts, 别忘了缓存预热")
    manager.add_message("search-b", "assistant", "已切换到 prefix cache 方案。")
    return manager


# ---------------------------------------------------------------------------
# 1. 中文两字词 / 三字词 / 英文
# ---------------------------------------------------------------------------


def test_search_matches_two_char_chinese_word(seeded):
    rows = seeded.search_messages("缓存")

    assert rows is not None
    hits = {r["session_name"] for r in rows}
    assert hits == {"search-a", "search-b"}
    for row in rows:
        assert set(row) == {"id", "role", "session_name", "display_name", "snippet"}
        assert row["display_name"] == row["session_name"]
        assert "缓存" in row["snippet"]


def test_search_matches_three_char_chinese_word(seeded):
    """三字词必须能精确命中（trigram 分词器与 unicode61 都做不到，靠 bigram）。"""
    rows = seeded.search_messages("缓存命中率")

    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["session_name"] == "search-a"
    assert rows[0]["role"] == "assistant"
    assert "缓存命中率" in rows[0]["snippet"]


def test_search_matches_ascii_word(seeded):
    rows = seeded.search_messages("prefix")

    assert rows is not None
    assert {r["session_name"] for r in rows} == {"search-b"}


# ---------------------------------------------------------------------------
# 2. 跨会话 + 会话过滤
# ---------------------------------------------------------------------------


def test_search_can_be_scoped_to_one_session(seeded):
    cross = seeded.search_messages("缓存", limit=20)
    scoped = seeded.search_messages("缓存", limit=20, session_name="search-a")

    assert cross is not None and scoped is not None
    assert len(cross) > len(scoped) > 0
    assert {r["session_name"] for r in scoped} == {"search-a"}
    assert {r["id"] for r in scoped} <= {r["id"] for r in cross}

    # 会话名不存在：不是 None（不可用），而是空列表（无命中）
    assert seeded.search_messages("缓存", session_name="no-such-session") == []


# ---------------------------------------------------------------------------
# 3. 懒同步
# ---------------------------------------------------------------------------


def test_search_lazily_indexes_new_messages_without_rebuild(manager):
    manager.add_message("lazy", "user", "这轮先聊缓存穿透的兜底")

    assert _indexed_max_rowid(manager) == 0  # 写入路径不挂钩子，索引尚未建立

    rows = manager.search_messages("缓存穿透")
    assert rows is not None
    assert len(rows) == 1 and rows[0]["session_name"] == "lazy"
    assert _indexed_max_rowid(manager) > 0  # 检索前补齐了索引

    # 再写一条新消息，仍不需要 rebuild
    manager.add_message("lazy", "assistant", "缓存穿透用布隆过滤器兜")
    assert len(manager.search_messages("布隆过滤器") or []) == 1


# ---------------------------------------------------------------------------
# 4. 全量重建
# ---------------------------------------------------------------------------


def test_rebuild_message_index_returns_count_and_keeps_search_working(seeded):
    before = _message_count(seeded)
    rebuilt = seeded.rebuild_message_index()

    assert rebuilt > 0
    assert rebuilt == before  # user/assistant/tool 三类角色都入索引
    rows = seeded.search_messages("缓存命中率")
    assert rows is not None and len(rows) == 1
    assert _indexed_max_rowid(seeded) > 0


# ---------------------------------------------------------------------------
# 5. 已删除消息不出现（JOIN 过滤孤儿索引行）
# ---------------------------------------------------------------------------


def test_deleted_message_disappears_from_results_but_index_row_remains(seeded):
    rows_before = seeded.search_messages("缓存", limit=20) or []
    victim = next(r for r in rows_before if r["session_name"] == "search-a")
    with seeded._lock:
        seeded.conn.execute("DELETE FROM messages WHERE id = ?", (victim["id"],))
        seeded.conn.commit()

    rows_after = seeded.search_messages("缓存", limit=20)
    assert rows_after is not None
    assert victim["id"] not in {r["id"] for r in rows_after}
    assert rows_after  # 同关键词的其它消息仍然可命中
    # 索引里的孤儿行不会被主动清理，但查询侧 JOIN 已把它排除
    assert _indexed_max_rowid(seeded) >= victim["id"]


# ---------------------------------------------------------------------------
# 6. 片段脱敏
# ---------------------------------------------------------------------------


def test_snippet_masks_secret_keys(manager):
    manager.add_message("secret-session", "user", f"配置 key {SECRET} 已写进环境变量")

    rows = manager.search_messages("配置")
    assert rows is not None and len(rows) == 1
    snippet = rows[0]["snippet"]
    assert SECRET not in snippet
    assert "sk-***" in snippet


# ---------------------------------------------------------------------------
# 7. 空查询
# ---------------------------------------------------------------------------


def test_empty_query_returns_empty_list_not_none(seeded):
    assert seeded.search_messages("") == []
    assert seeded.search_messages("   ") == []


# ---------------------------------------------------------------------------
# 8. FTS5 不可用降级
# ---------------------------------------------------------------------------


def test_fts_unavailable_returns_none_and_tool_reports_it(manager, monkeypatch):
    manager.add_message("fts-off", "user", "缓存相关的消息")
    monkeypatch.setattr(manager, "_ensure_message_fts", lambda *a, **k: False)
    monkeypatch.setattr("core.app_context.get_manager", lambda: manager)

    assert manager.search_messages("缓存") is None
    assert manager.sync_message_index() == 0
    assert manager.rebuild_message_index() == 0

    output = tools.search_sessions("缓存")
    assert "不可用" in output
    assert "FTS5" in output
