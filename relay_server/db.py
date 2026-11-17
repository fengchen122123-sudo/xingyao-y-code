"""relay_server 的 SQLite 连接与 devices 表。

库里只留设备与 token 哈希，在线状态全在内存（RelayRegistry）。
无账号维度：极简中转服务器只认设备。
"""

from __future__ import annotations

import sqlite3


def connect(path) -> sqlite3.Connection:
    """打开（必要时创建）中转库。

    check_same_thread=False：TestClient / uvicorn 的工作线程与创建线程不同，
    与旧网关一致，单进程部署下由调用方保证串行使用。
    """
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            last_seen INTEGER
        )
        """
    )
    conn.commit()
    return conn
