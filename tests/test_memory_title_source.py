"""sessions.title_source 与会话自动命名的持久化语义测试。"""

from core.memory import MemoryManager


def _manager(tmp_path):
    return MemoryManager(str(tmp_path / "title_source.db"))


def test_title_source_defaults_to_default_and_manual_rename_marks_it(tmp_path):
    manager = _manager(tmp_path)
    try:
        manager.create_session_explicit("session_abc123")

        assert manager.get_session_title_source("session_abc123") == "default"

        ok, final = manager.rename_session(
            "session_abc123", "手动名", title_source="manual"
        )

        assert ok is True
        assert final == "手动名"
        assert manager.get_session_title_source(final) == "manual"
    finally:
        manager.conn.close()


def test_rename_session_if_untitled_only_applies_once(tmp_path):
    manager = _manager(tmp_path)
    try:
        manager.create_session_explicit("session_abc123")

        ok, final = manager.rename_session_if_untitled("session_abc123", "AI标题")

        assert ok is True
        assert final == "AI标题"
        assert manager.get_session_title_source(final) == "ai"

        # 后续回合不再允许自动改名，保护已经定下来的标题
        ok2, final2 = manager.rename_session_if_untitled(final, "另一个标题")

        assert ok2 is False
        assert final2 == final
        assert manager.get_session_title_source(final) == "ai"
    finally:
        manager.conn.close()


def test_rename_session_if_untitled_rejects_after_manual_rename(tmp_path):
    manager = _manager(tmp_path)
    try:
        manager.create_session_explicit("session_abc123")
        manager.rename_session("session_abc123", "手动名", title_source="manual")

        ok, final = manager.rename_session_if_untitled("手动名", "AI标题")

        assert ok is False
        assert final == "手动名"
        assert manager.get_session_title_source("手动名") == "manual"
    finally:
        manager.conn.close()


def test_rename_session_default_argument_keeps_title_source(tmp_path):
    manager = _manager(tmp_path)
    try:
        manager.create_session_explicit("session_abc123")
        manager.rename_session("session_abc123", "第一步", title_source="ai")

        # 不带 title_source 的调用只改名，不重置来源标记
        ok, final = manager.rename_session("第一步", "第二步")

        assert ok is True
        assert final == "第二步"
        assert manager.get_session_title_source(final) == "ai"
    finally:
        manager.conn.close()
