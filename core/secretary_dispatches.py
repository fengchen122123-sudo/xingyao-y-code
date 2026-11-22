from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import List, Optional

__all__ = [
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_CANCELLED",
    "STATUS_BLOCKED",
    "TERMINAL_STATUSES",
    "DispatchStore",
    "format_secretary_dispatch_summary",
]

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_BLOCKED = "blocked"

TERMINAL_STATUSES = frozenset(
    {STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED, STATUS_BLOCKED}
)

_MAX_TITLE = 120
_MAX_EXCERPT = 800
_MAX_ERROR = 500
_MAX_LIST = 50

_DEFAULT_LIST_LIMIT = 20

_TIME_FMT = "%Y-%m-%d %H:%M:%S"

_COLUMNS = (
    "id",
    "secretary_session",
    "target_session",
    "target_display",
    "workdir",
    "title",
    "prompt",
    "status",
    "depends_on",
    "batch_id",
    "started_at",
    "finished_at",
    "result_excerpt",
    "error",
    "reported",
)

_SELECT_COLUMNS = ", ".join(_COLUMNS)

#: 台账建表语句：与 ``core/memory.py`` 的 ``create_tables`` 同源——那边在进程启动
#: 时建表，这里给「进程早于本次升级启动」的缺表场景兜底（GUI 常驻进程的
#: MemoryManager 构造只跑一次，旧连接里没有新表）。两边 DDL 必须逐字一致。
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS secretary_dispatches (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        secretary_session TEXT NOT NULL,
        target_session    TEXT NOT NULL,
        target_display   TEXT NOT NULL DEFAULT '',
        workdir          TEXT NOT NULL DEFAULT '',
        title            TEXT NOT NULL DEFAULT '',
        prompt           TEXT NOT NULL,
        status           TEXT NOT NULL DEFAULT 'running',
        depends_on       TEXT NOT NULL DEFAULT '[]',
        batch_id         TEXT NOT NULL DEFAULT '',
        started_at       TEXT NOT NULL DEFAULT '',
        finished_at      TEXT NOT NULL DEFAULT '',
        result_excerpt   TEXT NOT NULL DEFAULT '',
        error            TEXT NOT NULL DEFAULT '',
        reported         INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_secretary_dispatches_target "
    "ON secretary_dispatches(target_session)",
    "CREATE INDEX IF NOT EXISTS idx_secretary_dispatches_secretary "
    "ON secretary_dispatches(secretary_session, status)",
)


def _now() -> str:
    return datetime.now().strftime(_TIME_FMT)


def _clip(value, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit:
        return text[:limit]
    return text


def _text(value) -> str:
    return "" if value is None else str(value)


def _to_int(value) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _parse_depends_on(raw) -> List[int]:
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
    except Exception:
        return []
    if not isinstance(loaded, (list, tuple)):
        return []
    out: List[int] = []
    for item in loaded:
        parsed = _to_int(item)
        if parsed is not None:
            out.append(parsed)
    return out


def _dump_depends_on(value) -> str:
    if value is None:
        items: List[int] = []
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = []
        for item in value:
            parsed = _to_int(item)
            if parsed is not None:
                items.append(parsed)
    else:
        parsed = _to_int(value)
        items = [] if parsed is None else [parsed]
    try:
        return json.dumps(items, ensure_ascii=False)
    except Exception:
        return "[]"


def _row_to_dict(row) -> dict:
    data = {}
    for index, name in enumerate(_COLUMNS):
        try:
            data[name] = row[index]
        except Exception:
            data[name] = None
    data["depends_on"] = _parse_depends_on(data.get("depends_on"))
    return data


class DispatchStore:

    def __init__(self, manager):
        self._manager = manager
        self._table_ready = False

    def _ensure_table(self) -> None:
        if self._table_ready:
            return
        try:
            with self._manager.acquire_lock():
                cursor = self._manager.conn.cursor()
                for statement in _SCHEMA_STATEMENTS:
                    cursor.execute(statement)
                cursor.execute("PRAGMA table_info(secretary_dispatches)")
                cols = {col[1] for col in cursor.fetchall()}
                if "reported" not in cols:
                    cursor.execute(
                        "ALTER TABLE secretary_dispatches "
                        "ADD COLUMN reported INTEGER NOT NULL DEFAULT 0"
                    )
                self._manager.conn.commit()
            self._table_ready = True
        except Exception:
            logger.debug("补建秘书派活台账表失败", exc_info=True)

    def _cursor(self):
        self._ensure_table()
        return self._manager.conn.cursor()

    def create(
        self,
        *,
        secretary_session,
        target_session,
        target_display,
        workdir,
        title,
        prompt,
        status,
        depends_on=None,
        batch_id="",
    ) -> Optional[int]:
        """插入一条派活记录，返回新行 id；失败返回 ``None``（绝不外抛）。"""
        try:
            secretary_session_text = _text(secretary_session)
            target_session_text = _text(target_session)
            status_text = _text(status).strip() or STATUS_RUNNING
            now = _now()
            values = (
                secretary_session_text,
                target_session_text,
                _text(target_display),
                _text(workdir),
                _clip(title, _MAX_TITLE),
                _text(prompt),
                status_text,
                _dump_depends_on(depends_on),
                _text(batch_id),
                now,
                "",
                "",
                "",
            )
            with self._manager.acquire_lock():
                cursor = self._cursor()
                cursor.execute(
                    """
                    INSERT INTO secretary_dispatches (
                        secretary_session, target_session, target_display, workdir,
                        title, prompt, status, depends_on, batch_id,
                        started_at, finished_at, result_excerpt, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                dispatch_id = cursor.lastrowid
                self._manager.conn.commit()
            return _to_int(dispatch_id)
        except Exception as exc:  # 台账故障不得阻断派活
            logger.debug("secretary dispatch create failed: %s", exc)
            return None

    def mark_status(
        self,
        dispatch_id,
        status,
        *,
        result_excerpt="",
        error="",
    ) -> None:
        """更新状态（终态时写 ``finished_at``）；失败静默。"""
        try:
            parsed_id = _to_int(dispatch_id)
            if parsed_id is None:
                logger.debug("secretary dispatch mark_status skipped: bad id %r", dispatch_id)
                return
            status_text = _text(status).strip() or STATUS_RUNNING
            finished_at = _now() if status_text in TERMINAL_STATUSES else ""
            excerpt_text = _clip(result_excerpt, _MAX_EXCERPT)
            error_text = _clip(error, _MAX_ERROR)
            with self._manager.acquire_lock():
                cursor = self._cursor()
                cursor.execute(
                    """
                    UPDATE secretary_dispatches
                       SET status = ?, result_excerpt = ?, error = ?, finished_at = ?
                     WHERE id = ?
                    """,
                    (status_text, excerpt_text, error_text, finished_at, parsed_id),
                )
                self._manager.conn.commit()
        except Exception as exc:
            logger.debug("secretary dispatch mark_status failed: %s", exc)

    def set_depends_on(self, dispatch_id, depends_on: List[int]) -> None:
        try:
            parsed_id = _to_int(dispatch_id)
            if parsed_id is None:
                logger.debug(
                    "secretary dispatch set_depends_on skipped: bad id %r", dispatch_id
                )
                return
            payload = _dump_depends_on(depends_on)
            with self._manager.acquire_lock():
                cursor = self._cursor()
                cursor.execute(
                    "UPDATE secretary_dispatches SET depends_on = ? WHERE id = ?",
                    (payload, parsed_id),
                )
                self._manager.conn.commit()
        except Exception as exc:
            logger.debug("secretary dispatch set_depends_on failed: %s", exc)

    def get(self, dispatch_id) -> Optional[dict]:
        """按 id 取一条；不存在或出错返回 ``None``。"""
        try:
            parsed_id = _to_int(dispatch_id)
            if parsed_id is None:
                return None
            with self._manager.acquire_lock():
                cursor = self._cursor()
                cursor.execute(
                    "SELECT %s FROM secretary_dispatches WHERE id = ?" % _SELECT_COLUMNS,
                    (parsed_id,),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return _row_to_dict(row)
        except Exception as exc:
            logger.debug("secretary dispatch get failed: %s", exc)
            return None

    def list(
        self,
        *,
        secretary_session="",
        status="",
        workdir="",
        target_session="",
        target_like="",
        limit=20,
    ) -> List[dict]:
        try:
            parsed_limit = _to_int(limit)
            if parsed_limit is None:
                parsed_limit = _DEFAULT_LIST_LIMIT
            parsed_limit = max(1, min(_MAX_LIST, parsed_limit))

            clauses = []
            params: List[object] = []
            for column, value in (
                ("secretary_session", secretary_session),
                ("status", status),
                ("workdir", workdir),
                ("target_session", target_session),
            ):
                text = _text(value).strip()
                if text:
                    clauses.append("%s = ?" % column)
                    params.append(text)

            like_text = _text(target_like).strip()
            if like_text:
                pattern = "%" + like_text + "%"
                clauses.append("(target_session LIKE ? OR target_display LIKE ?)")
                params.append(pattern)
                params.append(pattern)

            sql = "SELECT %s FROM secretary_dispatches" % _SELECT_COLUMNS
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(parsed_limit)

            with self._manager.acquire_lock():
                cursor = self._cursor()
                cursor.execute(sql, tuple(params))
                rows = cursor.fetchall()
            return [_row_to_dict(row) for row in rows]
        except Exception as exc:
            logger.debug("secretary dispatch list failed: %s", exc)
            return []

    def dependents_of(self, dispatch_id) -> List[dict]:
        """直接下游（``depends_on`` 里含本 id 的记录）。"""
        try:
            parsed_id = _to_int(dispatch_id)
            if parsed_id is None:
                return []
            # 先用 LIKE 粗筛（JSON 文本里含该数字），再在 Python 侧精确匹配，
            # 避免 "1" 命中 "11" / "21" 这类子串误报。
            with self._manager.acquire_lock():
                cursor = self._cursor()
                cursor.execute(
                    "SELECT %s FROM secretary_dispatches "
                    "WHERE depends_on LIKE ? ORDER BY id ASC" % _SELECT_COLUMNS,
                    ("%" + str(parsed_id) + "%",),
                )
                rows = cursor.fetchall()
            out: List[dict] = []
            for row in rows:
                item = _row_to_dict(row)
                if parsed_id in item.get("depends_on") or []:
                    out.append(item)
            return out
        except Exception as exc:
            logger.debug("secretary dispatch dependents_of failed: %s", exc)
            return []

    def unlocked_dependents(self, dispatch_id) -> List[dict]:
        """直接下游中「所有依赖都已 done」的那些（可以直接开跑）。"""
        try:
            dependents = self.dependents_of(dispatch_id)
            if not dependents:
                return []
            wanted = set()
            for item in dependents:
                for dep_id in item.get("depends_on") or []:
                    parsed = _to_int(dep_id)
                    if parsed is not None:
                        wanted.add(parsed)
            if not wanted:
                return []

            placeholders = ", ".join("?" for _ in wanted)
            with self._manager.acquire_lock():
                cursor = self._cursor()
                cursor.execute(
                    "SELECT id, status FROM secretary_dispatches WHERE id IN (%s)"
                    % placeholders,
                    tuple(sorted(wanted)),
                )
                statuses = {row[0]: row[1] for row in cursor.fetchall()}

            out: List[dict] = []
            for item in dependents:
                dep_ids = item.get("depends_on") or []
                if not dep_ids:
                    continue
                if all(statuses.get(dep_id) == STATUS_DONE for dep_id in dep_ids):
                    out.append(item)
            return out
        except Exception as exc:
            logger.debug("secretary dispatch unlocked_dependents failed: %s", exc)
            return []

    def batch_has_active_tasks(self, batch_id: str) -> bool:
        """批次里是否还有正在排队、运行或等待上游解锁的任务。"""
        bid = _text(batch_id).strip()
        if not bid:
            return False
        try:
            with self._manager.acquire_lock():
                cursor = self._cursor()
                cursor.execute(
                    "SELECT 1 FROM secretary_dispatches "
                    "WHERE batch_id = ? "
                    "  AND (status IN (?, ?) OR (status = ? AND COALESCE(error, '') = '')) "
                    "LIMIT 1",
                    (bid, STATUS_QUEUED, STATUS_RUNNING, STATUS_BLOCKED),
                )
                return cursor.fetchone() is not None
        except Exception as exc:
            logger.debug("secretary dispatch batch_has_active_tasks failed: %s", exc)
            return False

    def has_unreported_wakeable(self, secretary_session: str) -> bool:
        sec = _text(secretary_session).strip()
        if not sec:
            return False
        try:
            with self._manager.acquire_lock():
                cursor = self._cursor()
                cursor.execute(
                    "SELECT 1 FROM secretary_dispatches "
                    "WHERE secretary_session = ? "
                    "  AND COALESCE(reported, 0) = 0 "
                    "  AND (status IN (?, ?) OR (status = ? AND COALESCE(error, '') != '')) "
                    "LIMIT 1",
                    (sec, STATUS_DONE, STATUS_FAILED, STATUS_BLOCKED),
                )
                return cursor.fetchone() is not None
        except Exception as exc:
            logger.debug("secretary dispatch has_unreported_wakeable failed: %s", exc)
            return False

    def claim_unreported(
        self,
        secretary_session: str,
        *,
        include_cancelled: bool = True,
    ) -> List[dict]:
        sec = _text(secretary_session).strip()
        if not sec:
            return []
        terminal_statuses = [STATUS_DONE, STATUS_FAILED]
        if include_cancelled:
            terminal_statuses.append(STATUS_CANCELLED)
        placeholders = ", ".join("?" for _ in terminal_statuses)
        try:
            with self._manager.acquire_lock():
                cursor = self._cursor()
                cursor.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM secretary_dispatches "
                    f"WHERE secretary_session = ? "
                    f"  AND COALESCE(reported, 0) = 0 "
                    f"  AND (status IN ({placeholders}) OR (status = ? AND COALESCE(error, '') != '')) "
                    f"ORDER BY id ASC",
                    (sec, *terminal_statuses, STATUS_BLOCKED),
                )
                rows = cursor.fetchall()
                if not rows:
                    return []
                items = [_row_to_dict(row) for row in rows]
                ids = [item["id"] for item in items if item.get("id") is not None]
                if ids:
                    id_marks = ", ".join("?" for _ in ids)
                    cursor.execute(
                        f"UPDATE secretary_dispatches SET reported = 1 WHERE id IN ({id_marks})",
                        tuple(ids),
                    )
                    self._manager.conn.commit()
            return items
        except Exception as exc:
            logger.debug("secretary dispatch claim_unreported failed: %s", exc)
            return []


_STATUS_LABELS = {
    STATUS_DONE: "已完成 (done)",
    STATUS_FAILED: "执行失败 (failed)",
    STATUS_CANCELLED: "用户已取消 (cancelled)",
    STATUS_BLOCKED: "上游失败导致阻塞 (blocked)",
    STATUS_RUNNING: "执行中 (running)",
    STATUS_QUEUED: "排队中 (queued)",
}


def format_secretary_dispatch_summary(rows: List[dict]) -> str:
    items = [row for row in (rows or []) if isinstance(row, dict)]
    if not items:
        return ""
    try:
        from core.injection_markers import neutralize_reserved_markers
    except Exception:
        def neutralize_reserved_markers(val: str) -> str:
            return str(val or "")

    def _safe(value, limit: int = 800) -> str:
        text = neutralize_reserved_markers(str(value or "").strip())
        return text[:limit]

    lines = [
        "<secretary-dispatch-summary>",
        f"以下 {len(items)} 项你此前派出的任务已有终态结果：",
    ]
    for row in items:
        did = row.get("id")
        status = str(row.get("status") or "").strip().lower()
        label = _STATUS_LABELS.get(status, status or "未知")
        title = _safe(row.get("title") or "未命名任务", 120)
        target_session = _safe(row.get("target_session") or "", 120)
        target_display = _safe(row.get("target_display") or target_session, 120)
        workdir = _safe(row.get("workdir") or "GLOBAL", 200)
        batch_id = _safe(row.get("batch_id") or "", 80)

        header = f"- 任务 #{did}「{title}」| 状态：{label} | 会话：{target_display} ({target_session}) | 项目：{workdir}"
        if batch_id:
            header += f" | 批次：{batch_id}"
        lines.append(header)

        if status == STATUS_CANCELLED:
            lines.append(
                "  说明：该任务已被用户主动取消。严格禁止擅自重试或重新派发，除非用户后续明确要求。"
            )
        error_text = _safe(row.get("error") or "", 500)
        excerpt = _safe(row.get("result_excerpt") or "", 800)
        if error_text:
            lines.append(f"  错误：{error_text}")
        if excerpt:
            lines.append(f"  结论摘要：{excerpt}")
    lines.append("</secretary-dispatch-summary>")
    return "\n".join(lines)
