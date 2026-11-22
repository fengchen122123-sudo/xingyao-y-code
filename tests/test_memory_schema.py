import sqlite3

from core.memory import MemoryManager


def test_create_tables_drops_legacy_session_diffs(tmp_path):
    db_path = tmp_path / "ycode_gui.db"

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE session_diffs (session_id INTEGER, payload TEXT)")
    conn.execute("INSERT INTO session_diffs VALUES (1, 'legacy')")
    conn.commit()
    conn.close()

    manager = MemoryManager(str(db_path))
    try:
        cursor = manager.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_diffs'"
        )
        assert cursor.fetchone() is None, "遗留 session_diffs 表应被 DROP"
        cursor = manager.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        )
        assert cursor.fetchone() is not None
    finally:
        manager.conn.close()
