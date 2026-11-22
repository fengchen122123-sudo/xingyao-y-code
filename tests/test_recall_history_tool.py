import json

import pytest

import core.tools as tools
from core.memory import MemoryManager


@pytest.fixture
def manager():
    m = MemoryManager(":memory:")
    m.create_session_explicit("recall-session", workdir="GLOBAL", source="GUI")
    return m


def _seed_history(manager, count):
    last_id = 0
    for i in range(count):
        last_id = manager.add_message(
            "recall-session",
            "user" if i % 2 == 0 else "assistant",
            f"消息 {i} 的内容",
        ) or last_id
    return last_id


def _mark_compressed(manager, summary, boundary):
    cursor = manager.conn.cursor()
    cursor.execute(
        "UPDATE sessions SET memory_summary = ?, summarized_until_msg_id = ? "
        "WHERE session_name = ?",
        (summary, boundary, "recall-session"),
    )
    manager.conn.commit()


def test_archive_generations_increment_and_index_order(manager):
    _seed_history(manager, 10)
    gen1 = manager.archive_summary_generation("recall-session", "第一代摘要", "主题A", 4)
    gen2 = manager.archive_summary_generation("recall-session", "第二代摘要", "主题B", 8)
    assert (gen1, gen2) == (1, 2)

    rows, total = manager.get_summary_generation_index("recall-session", 10)
    assert total == 2
    assert [r["gen"] for r in rows] == [2, 1]
    assert rows[0]["topics"] == "主题B"
    assert rows[1]["until_msg_id"] == 4


def test_index_injected_into_summary_block_and_byte_stable(manager):
    _seed_history(manager, 10)
    _mark_compressed(manager, "当前摘要", 6)
    manager.archive_summary_generation("recall-session", "更早摘要", "历史主题", 3)

    history_a = manager.get_history_for_llm("recall-session")
    history_b = manager.get_history_for_llm("recall-session")

    first = history_a[0]
    assert first["role"] == "user"
    assert "当前摘要" in first["content"]
    assert "[更早摘要回溯]" in first["content"]
    assert "gen#1" in first["content"]
    assert "recall_history" in first["content"]
    # 两次构建字节一致：索引行在两次压缩之间必须稳定，不破坏前缀缓存
    assert json.dumps(history_a, ensure_ascii=False, sort_keys=False) == json.dumps(
        history_b, ensure_ascii=False, sort_keys=False
    )


def test_no_archive_keeps_summary_block_unchanged(manager):
    _seed_history(manager, 10)
    _mark_compressed(manager, "当前摘要", 6)

    history = manager.get_history_for_llm("recall-session")
    assert history[0]["role"] == "user"
    assert "当前摘要" in history[0]["content"]
    assert "[更早摘要回溯]" not in history[0]["content"]


def test_get_compressed_messages_respects_watermark_and_order(manager):
    _seed_history(manager, 10)
    _mark_compressed(manager, "当前摘要", 6)

    rows = manager.get_compressed_messages("recall-session", before_msg_id=6, limit=4)
    assert [r["id"] for r in rows] == [3, 4, 5, 6]
    assert rows[0]["role"] in ("user", "assistant")
    assert "消息 2" in rows[0]["content"]

    rows_all = manager.get_compressed_messages("recall-session", before_msg_id=99, limit=50)
    assert max(r["id"] for r in rows_all) == 6


def test_undo_last_interaction_clears_archive(manager):
    _seed_history(manager, 10)
    _mark_compressed(manager, "当前摘要", 8)
    manager.archive_summary_generation("recall-session", "归档摘要", "主题", 8)

    manager.undo_last_interaction("recall-session")

    rows, total = manager.get_summary_generation_index("recall-session", 10)
    assert total == 0 and rows == []
    cursor = manager.conn.cursor()
    cursor.execute(
        "SELECT memory_summary, summarized_until_msg_id FROM sessions WHERE session_name = ?",
        ("recall-session",),
    )
    summary, boundary = cursor.fetchone()
    assert summary is None and (boundary or 0) == 0


def test_rename_session_migrates_archive(manager):
    _seed_history(manager, 10)
    manager.archive_summary_generation("recall-session", "第一代摘要", "主题A", 4)
    manager.archive_summary_generation("recall-session", "第二代摘要", "主题B", 8)

    ok, final_name = manager.rename_session("recall-session", "renamed-session")
    assert ok and final_name == "renamed-session"

    rows, total = manager.get_summary_generation_index("renamed-session", 10)
    assert total == 2
    assert [r["gen"] for r in rows] == [2, 1]
    assert manager.get_summary_generation_index("recall-session", 10) == ([], 0)

    assert manager.archive_summary_generation("renamed-session", "第三代摘要", "主题C", 10) == 3


def test_recall_history_tool_returns_formatted_rows(manager, monkeypatch):
    _seed_history(manager, 10)
    _mark_compressed(manager, "当前摘要", 6)
    monkeypatch.setattr("core.app_context.get_thread_session", lambda: None)
    monkeypatch.setattr("core.app_context.get_current_session", lambda: "recall-session")
    monkeypatch.setattr("core.app_context.get_manager", lambda: manager)

    output = tools.recall_history(before_msg_id=6, limit=3)

    assert "#msg_id=" in output
    assert "[assistant]" in output or "[user]" in output
    assert "消息 3" in output or "消息 4" in output
    assert "把 before_msg_id 设为本批最早一条" in output


def test_recall_history_tool_without_watermark_returns_hint(manager, monkeypatch):
    _seed_history(manager, 10)
    monkeypatch.setattr("core.app_context.get_thread_session", lambda: None)
    monkeypatch.setattr("core.app_context.get_current_session", lambda: "recall-session")
    monkeypatch.setattr("core.app_context.get_manager", lambda: manager)

    output = tools.recall_history(before_msg_id=1)
    assert "没有已压缩的原始消息" in output


def test_recall_history_tool_without_session_errors(monkeypatch):
    monkeypatch.setattr("core.app_context.get_thread_session", lambda: None)
    monkeypatch.setattr("core.app_context.get_current_session", lambda: "")
    output = tools.recall_history(before_msg_id=5)
    assert "无法确定当前会话" in output
