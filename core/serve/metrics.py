"""serve 进程的 /metrics。GUI 不注册这条路由。"""

from __future__ import annotations

import sqlite3
import time

from core.lan_server import PROCESS_BOOT_ID

_STARTED = time.time()
_LAST_ACTIVITY = time.time()


def note_activity() -> None:
    global _LAST_ACTIVITY
    _LAST_ACTIVITY = time.time()


def collect_metrics() -> dict:
    usage = []
    sessions = 0
    turns_total = 0
    active_turn = False
    try:
        import config

        database = sqlite3.connect(config.GUI_DB_FILE)
        try:
            usage = _token_usage(database)
            sessions = _count(database, "SELECT COUNT(*) FROM sessions")
            turns_total = _count(
                database,
                """
                SELECT COUNT(*) FROM harness_turns
                WHERE status = 'completed'
                  AND COALESCE(turn_kind, '') <> 'resume'
                """,
            )
            active_turn = _count(
                database,
                """
                SELECT COUNT(*) FROM harness_turns
                WHERE status IN ('created', 'preparing', 'running', 'cancelling')
                """,
            ) > 0
        finally:
            database.close()
    except Exception:
        pass
    try:
        from core.version import __version__ as version
    except Exception:
        version = ""
    return {
        "version": version,
        "boot_id": PROCESS_BOOT_ID,
        "uptime_s": int(time.time() - _STARTED),
        "last_activity_ts": int(_LAST_ACTIVITY),
        "active_turn": active_turn,
        "token_usage": usage,
        "sessions": sessions,
        "turns_total": turns_total,
    }


def _count(database, sql: str) -> int:
    row = database.execute(sql).fetchone()
    return int(row[0] or 0) if row else 0


def _token_usage(database) -> list:
    rows = database.execute(
        """
        SELECT COALESCE(model, ''),
               SUM(prompt_tokens),
               SUM(completion_tokens),
               SUM(total_tokens)
        FROM token_usage
        GROUP BY COALESCE(model, '')
        """
    ).fetchall()
    return [
        {
            "model": str(model or ""),
            "prompt_tokens": int(prompt or 0),
            "completion_tokens": int(completion or 0),
            "total_tokens": int(total or 0),
        }
        for model, prompt, completion, total in rows
    ]
