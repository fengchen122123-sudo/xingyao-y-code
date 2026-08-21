"""数据库 schema 历史遗留清理的回归测试。"""

import sqlite3

from core.memory import MemoryManager


def test_create_tables_drops_legacy_session_diffs(tmp_path):
    """session_diffs 表来自 1.2.5 开发期已移除的 diff 持久化方案：
    create_tables 必须幂等清掉旧构建遗留的表与数据，防止永久残留。
    """
    db_path = tmp_path / "ycode_gui.db"

    # 模拟旧版本库：session_diffs 表 + 一行历史数据
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE session_diffs (session_id INTEGER, payload TEXT)")
    conn.execute("INSERT INTO session_diffs VALUES (1, 'legacy')")
    conn.commit()
    conn.close()

    # 新版本启动：DatabaseManager.__init__ → create_tables 执行清理
    manager = MemoryManager(str(db_path))
    try:
        cursor = manager.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_diffs'"
        )
        assert cursor.fetchone() is None, "遗留 session_diffs 表应被 DROP"
        # 核心表不受影响
        cursor = manager.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        )
        assert cursor.fetchone() is not None
    finally:
        manager.conn.close()
