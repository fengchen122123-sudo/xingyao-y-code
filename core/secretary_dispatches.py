"""秘书派活台账的存储层（`secretary_dispatches` 表的纯数据访问）。

职责边界：
- 只做 SQL 读写，不做业务判断、不发 notice、不 import gui_bridge；
- 复用 ``MemoryManager`` 的共享连接（``manager.conn``）与可重入锁
  （``manager.acquire_lock()``），因此不需要自己建连接、也不引入第二个
  写句柄（WAL 下第二个写句柄会与主连接抢锁）；
- **所有方法都必须吞掉异常**：台账故障（缺表、连接异常、并发写冲突）只记
  ``logger.debug``，绝不外抛——派活与回合收尾路径都调这里，台账坏掉不能
  把整轮对话带崩。因此 ``create`` 失败返回 ``None``，其余方法失败返回空值。

状态取值：``queued``（目标会话忙，已入队）/ ``running`` / ``done`` /
``failed`` / ``cancelled`` / ``blocked``（依赖未满足或上游失败）。
其中后四个是终态，写 ``finished_at``。
"""

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
]

logger = logging.getLogger(__name__)

# --- 状态常量 ---------------------------------------------------------------
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_BLOCKED = "blocked"

#: 终态集合：落到这些状态时写 ``finished_at``
TERMINAL_STATUSES = frozenset(
    {STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED, STATUS_BLOCKED}
)

# --- 长度上限（防止一条超长内容把台账撑爆） --------------------------------
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
        error            TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_secretary_dispatches_target "
    "ON secretary_dispatches(target_session)",
    "CREATE INDEX IF NOT EXISTS idx_secretary_dispatches_secretary "
    "ON secretary_dispatches(secretary_session, status)",
)


def _now() -> str:
    """当前本地时间，统一 ``%Y-%m-%d %H:%M:%S`` 文本（与表里其他时间列同风格）。"""
    return datetime.now().strftime(_TIME_FMT)


def _clip(value, limit: int) -> str:
    """把任意值转成字符串并截断到 ``limit`` 个字符。"""
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
    """``depends_on`` 列是 JSON 文本，解析失败一律当空列表（老数据/脏数据不致命）。"""
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
    """把依赖列表序列化成 JSON 文本；非法项直接丢弃，保证往返稳定。"""
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
    """sqlite3.Row / tuple → dict，``depends_on`` 解析回列表。"""
    data = {}
    for index, name in enumerate(_COLUMNS):
        try:
            data[name] = row[index]
        except Exception:
            data[name] = None
    data["depends_on"] = _parse_depends_on(data.get("depends_on"))
    return data


class DispatchStore:
    """派活台账读写。

    ``manager`` 需提供 ``.conn``（sqlite3 连接）与 ``.acquire_lock()``
    （返回可重入锁）——通常就是 ``core.memory.MemoryManager``。
    """

    def __init__(self, manager):
        self._manager = manager
        # 表是否已确认存在（惰性补建，见 _ensure_table）
        self._table_ready = False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _ensure_table(self) -> None:
        """惰性补建台账表（缺表兜底）。

        为什么需要：``MemoryManager.create_tables`` 只在构造时跑一次。进程如果
        早于本次升级启动（比如 GUI 一直开着），它的连接里没有
        ``secretary_dispatches`` 表，派活就会静默写不进去、事后也查不到。
        这里用 IF NOT EXISTS 在首次访问时补建，成功一次后不再重复；失败只记
        debug 并且下次访问再试——台账故障永远不能拖垮派活路径。
        """
        if self._table_ready:
            return
        try:
            with self._manager.acquire_lock():
                cursor = self._manager.conn.cursor()
                for statement in _SCHEMA_STATEMENTS:
                    cursor.execute(statement)
                self._manager.conn.commit()
            self._table_ready = True
        except Exception:
            logger.debug("补建秘书派活台账表失败", exc_info=True)

    def _cursor(self):
        self._ensure_table()
        return self._manager.conn.cursor()

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
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
        """回写一条记录的上游依赖（真实 ``dispatch_id`` 列表）；失败静默。

        批量派活的用法：整批先建台账行拿到 id，再把"同批次下标依赖"翻译成
        真实 id 调本方法回写——因此它必须在建行之后还能改 ``depends_on``。

        容错口径与其余写方法一致：id 非法 / 库异常只记 ``logger.debug``。写失败
        的后果只是下游永远等不到解锁（保持 ``blocked``），不该把派活整批带崩。
        """
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

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
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
        """按可选条件过滤，按 id 倒序返回；``limit`` 夹紧到 1..50。

        ``target_like`` 是目标会话名 / 显示名的**子串**过滤（SQL 侧
        ``LIKE '%关键词%'``，任一侧命中即算命中），与 ``target_session`` 的
        精确匹配并存、互不影响。过滤必须落在 SQL 里：先取满一窗再在内存里
        筛，台账超过窗口上限后更早的记录永远查不到。
        """
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
