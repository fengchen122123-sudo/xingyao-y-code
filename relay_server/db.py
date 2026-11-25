from __future__ import annotations

import sqlite3


def connect(path) -> sqlite3.Connection:
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
            last_seen INTEGER,
            device_code TEXT
        )
        """
    )
    # 兼容既有库：补齐 device_code 列与唯一索引（NULL 允许多个，仅约束非空设备编码）
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(devices)").fetchall()}
    if "device_code" not in cols:
        conn.execute("ALTER TABLE devices ADD COLUMN device_code TEXT")
    if "active_jti" not in cols:
        # 同一设备只保留一张有效远端票：刷新后旧链接立即作废（单活语义）
        conn.execute("ALTER TABLE devices ADD COLUMN active_jti TEXT")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_device_code
        ON devices(device_code) WHERE device_code IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_tokens (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            revoked INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            last_used INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mcp_tokens_device_id
        ON mcp_tokens(device_id)
        """
    )
    conn.commit()
    return conn
