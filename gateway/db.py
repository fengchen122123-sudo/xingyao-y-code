"""网关 SQLite。启动时只向前迁移，失败则由调用方拒绝接流量。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id            TEXT PRIMARY KEY,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'user',
  disabled      INTEGER NOT NULL DEFAULT 0,
  auth_epoch    INTEGER NOT NULL DEFAULT 0,
  quota_json    TEXT NOT NULL DEFAULT '{}',
  created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
  token_hash  TEXT PRIMARY KEY,
  account_id  TEXT NOT NULL REFERENCES accounts(id),
  expires_at  INTEGER NOT NULL,
  revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS devices (
  id          TEXT PRIMARY KEY,
  account_id  TEXT NOT NULL REFERENCES accounts(id),
  name        TEXT NOT NULL,
  token_hash  TEXT UNIQUE NOT NULL,
  revoked     INTEGER NOT NULL DEFAULT 0,
  last_seen   INTEGER,
  created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
  account_id     TEXT PRIMARY KEY REFERENCES accounts(id),
  container_name TEXT NOT NULL,
  worker_token   TEXT NOT NULL,
  desired_state  TEXT NOT NULL DEFAULT 'stopped',
  last_activity  INTEGER,
  updated_at     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_daily (
  account_id TEXT NOT NULL,
  day        TEXT NOT NULL,
  model      TEXT NOT NULL,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  turns      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, day, model)
);
CREATE TABLE IF NOT EXISTS usage_watermarks (
  account_id TEXT NOT NULL,
  model      TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  turns_total INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (account_id, model)
);
CREATE TABLE IF NOT EXISTS access_denylist (
  jti         TEXT PRIMARY KEY,
  account_id  TEXT NOT NULL,
  expires_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);
"""

CURRENT_VERSION = 1


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path, check_same_thread=False)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("PRAGMA foreign_keys=ON")
    migrate(database)
    return database


def migrate(database: sqlite3.Connection) -> None:
    database.executescript(SCHEMA)
    row = database.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    current = int(row[0] or 0) if row and row[0] is not None else 0
    if current > CURRENT_VERSION:
        raise RuntimeError(f"网关数据库版本 {current} 高于程序 {CURRENT_VERSION}")
    if current < CURRENT_VERSION:
        database.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (CURRENT_VERSION, int(time.time())),
        )
    database.commit()


def account_count(database: sqlite3.Connection) -> int:
    row = database.execute("SELECT COUNT(*) FROM accounts").fetchone()
    return int(row[0])
