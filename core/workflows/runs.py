"""星流工作流运行记录（ycode.db ``workflow_runs`` 表）。

「什么时候执行、执行得怎么样」的台账：``workflow_run`` 工具发起执行时落一条
``running`` 记录并拿到 run_id；模型按契约在收尾时调 ``workflow_finish`` 回写
终态（成功 / 失败 + 一句话结论）。应用启动时把遗留 ``running`` 一律标记为
``interrupted``（与星轨自动化同款恢复语义——记录只是历史，绝不自动重放）。

存储经 ``init_run_storage(manager)`` 注入（gui_bridge 启动时）；未注入（CLI /
headless、或测试未初始化）时 ``get_run_storage()`` 返回 None，工具层全部
best-effort 降级——工作流照常执行，只是不留台账。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger("ycode.workflows")

#: 终态集合：finish 只允许 running → completed/failed；interrupted 仅由恢复写入。
RUN_STATUSES = ("running", "completed", "failed", "interrupted")
FINISH_STATUSES = ("completed", "failed")

_RUNS_TABLE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS workflow_runs (
        id TEXT PRIMARY KEY,
        workflow_name TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT '',
        session_name TEXT NOT NULL DEFAULT '',
        args_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'running',
        summary TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        artifact_dir TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workflow_runs_name ON workflow_runs(workflow_name, started_at)",
    """
    CREATE TABLE IF NOT EXISTS workflow_run_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workflow_run_events_run ON workflow_run_events(run_id, seq)",
)


#: 行 → dict 的列序（SELECT * 依赖建表列序，显式列出防漂移）。
_RUN_COLUMNS = (
    "id",
    "workflow_name",
    "scope",
    "session_name",
    "args_json",
    "status",
    "summary",
    "error",
    "artifact_dir",
    "started_at",
    "finished_at",
)

_EVENT_COLUMNS = ("seq", "run_id", "kind", "payload_json", "created_at")


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_run_id() -> str:
    return f"wfr-{uuid.uuid4().hex[:12]}"


def _row_to_run(row) -> dict:
    values = dict(zip(_RUN_COLUMNS, tuple(row)))
    try:
        values["args"] = json.loads(values.pop("args_json") or "{}")
    except ValueError:
        values["args"] = {}
    return values


class WorkflowRunStorage:
    """工作流运行记录 DAO。持有 MemoryManager，复用其连接与可重入锁。"""

    def __init__(self, manager):
        self._manager = manager
        self._create_tables()

    @property
    def conn(self):
        return self._manager.conn

    def _create_tables(self) -> None:
        with self._manager._lock:
            cursor = self.conn.cursor()
            for ddl in _RUNS_TABLE_DDL:
                cursor.execute(ddl)
            self.conn.commit()

    # -- 写入 --

    def create_run(
        self,
        workflow_name: str,
        *,
        scope: str = "",
        session_name: str = "",
        args: Optional[dict] = None,
        artifact_dir: str = "",
    ) -> dict:
        """落一条 running 记录，返回完整 run（含 id）。"""
        run_id = new_run_id()
        now = iso_now()
        with self._manager._lock:
            self.conn.execute(
                "INSERT INTO workflow_runs (id, workflow_name, scope, session_name,"
                " args_json, status, artifact_dir, started_at)"
                " VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
                (
                    run_id,
                    str(workflow_name),
                    str(scope or ""),
                    str(session_name or ""),
                    json.dumps(args or {}, ensure_ascii=False),
                    str(artifact_dir or ""),
                    now,
                ),
            )
            self.conn.commit()
        return {
            "id": run_id,
            "workflow_name": str(workflow_name),
            "scope": str(scope or ""),
            "session_name": str(session_name or ""),
            "args": args or {},
            "status": "running",
            "summary": "",
            "error": "",
            "artifact_dir": str(artifact_dir or ""),
            "started_at": now,
            "finished_at": "",
        }

    def finish_run(self, run_id: str, status: str, summary: str = "") -> bool:
        """回写终态：只接受 running → completed/failed，幂等（已终态不再改写）。"""
        if status not in FINISH_STATUSES:
            raise ValueError(f"finish 只允许 {FINISH_STATUSES}，got {status!r}")
        run_id = str(run_id or "")
        if not run_id:
            return False
        with self._manager._lock:
            cursor = self.conn.execute(
                "UPDATE workflow_runs SET status = ?, summary = ?, error = ?,"
                " finished_at = ? WHERE id = ? AND status = 'running'",
                (
                    status,
                    str(summary or "")[:2000],
                    str(summary or "")[:2000] if status == "failed" else "",
                    iso_now(),
                    run_id,
                ),
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def recover_stale_runs(self) -> int:
        """启动恢复：遗留 running 一律标 interrupted（历史留痕，不重放）。"""
        with self._manager._lock:
            cursor = self.conn.execute(
                "UPDATE workflow_runs SET status = 'interrupted',"
                " error = '应用退出或会话中断，运行未收尾', finished_at = ?"
                " WHERE status = 'running'",
                (iso_now(),),
            )
            self.conn.commit()
            return cursor.rowcount

    # -- 读取 --

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._manager._lock:
            row = self.conn.execute(
                "SELECT * FROM workflow_runs WHERE id = ?", (str(run_id or ""),)
            ).fetchone()
        return _row_to_run(row) if row else None

    def list_runs(self, workflow_name: str = "", limit: int = 50) -> list[dict]:
        """按开始时间倒序；workflow_name 为空返回全部工作流的最近运行。"""
        limit = max(1, min(int(limit or 50), 200))
        with self._manager._lock:
            if workflow_name:
                rows = self.conn.execute(
                    "SELECT * FROM workflow_runs WHERE workflow_name = ?"
                    " ORDER BY started_at DESC, rowid DESC LIMIT ?",
                    (str(workflow_name), limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM workflow_runs ORDER BY started_at DESC, rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_row_to_run(row) for row in rows]

    # -- 节点级事件（解释执行器的进度流）--

    def append_event(self, run_id: str, kind: str, payload: dict) -> int:
        """落一条运行事件，返回 seq；台账未初始化时返回 0（best-effort）。"""
        with self._manager._lock:
            cursor = self.conn.execute(
                "INSERT INTO workflow_run_events (run_id, kind, payload_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    str(run_id or ""),
                    str(kind or ""),
                    json.dumps(payload or {}, ensure_ascii=False),
                    iso_now(),
                ),
            )
            self.conn.commit()
            return int(cursor.lastrowid or 0)

    def list_events(self, run_id: str, limit: int = 200) -> list[dict]:
        with self._manager._lock:
            rows = self.conn.execute(
                "SELECT * FROM workflow_run_events WHERE run_id = ? ORDER BY seq LIMIT ?",
                (str(run_id or ""), max(1, min(int(limit or 200), 1000))),
            ).fetchall()
        events = []
        for row in rows:
            values = dict(zip(_EVENT_COLUMNS, tuple(row)))
            try:
                values["payload"] = json.loads(values.pop("payload_json") or "{}")
            except ValueError:
                values["payload"] = {}
            events.append(values)
        return events

    def clear_runs(self, workflow_name: str) -> int:
        """清空某工作流的运行历史（面板维护入口），含节点级事件。"""
        with self._manager._lock:
            run_ids = [
                row[0]
                for row in self.conn.execute(
                    "SELECT id FROM workflow_runs WHERE workflow_name = ?",
                    (str(workflow_name),),
                ).fetchall()
            ]
            removed = self.conn.execute(
                "DELETE FROM workflow_runs WHERE workflow_name = ?", (str(workflow_name),)
            ).rowcount
            for run_id in run_ids:
                self.conn.execute(
                    "DELETE FROM workflow_run_events WHERE run_id = ?", (run_id,)
                )
            self.conn.commit()
            return removed


# -- 进程级句柄（gui_bridge 启动时注入；未注入时工具层 best-effort 降级）--

_storage: Optional[WorkflowRunStorage] = None

#: 实时事件汇（gui_bridge 注册 → emit_event 推前端）；签名 (run_id, kind, payload)。
_event_sink: Optional[Callable] = None

#: 取消事件提供器（gui_bridge 注册）；签名 () -> threading.Event | None，
#: 返回当前会话/回合的取消事件，解释执行器据此协作中断。
_cancel_provider: Optional[Callable] = None


def init_run_storage(manager) -> WorkflowRunStorage:
    global _storage
    _storage = WorkflowRunStorage(manager)
    return _storage


def get_run_storage() -> Optional[WorkflowRunStorage]:
    return _storage


def set_event_sink(callback: Optional[Callable]) -> None:
    global _event_sink
    _event_sink = callback


def set_cancel_provider(provider: Optional[Callable]) -> None:
    global _cancel_provider
    _cancel_provider = provider


def get_cancel_provider() -> Optional[Callable]:
    return _cancel_provider


def publish_run_event(run_id: str, kind: str, payload: dict) -> None:
    """运行事件发布：台账落库（best-effort）+ 实时汇推送（best-effort）。

    任何一侧不可用都不影响另一侧；绝不向执行器抛异常。
    """
    try:
        storage = _storage
        if storage is not None:
            storage.append_event(run_id, kind, payload)
    except Exception:
        logger.warning("工作流事件落库失败（run=%s kind=%s）", run_id, kind, exc_info=True)
    try:
        sink = _event_sink
        if sink is not None:
            sink(run_id, kind, payload or {})
    except Exception:
        logger.debug("工作流事件推送失败（run=%s kind=%s）", run_id, kind, exc_info=True)
