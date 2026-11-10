"""星轨自动化任务：SQLite 存储层。

- 复用桥既有 MemoryManager 的连接与可重入锁（与 core/turn_undo/storage.py 同一
  挂接模式）；建表幂等，写操作显式 ``commit()``。
- ``claim_due_tasks`` 是防重复触发的唯一闸门：单条 CAS UPDATE 认领
  （``WHERE next_run_at IS ?``），跨线程/跨进程重复触发天然被排除。
- 单飞判定（同任务存在 queued/running 运行时写 skipped 不派发）与启动恢复
  （遗留 queued/running → interrupted，绝不自动重跑）也在这里实现。
- ``on_run_updated`` 回调：所有运行行变化（认领 queued/skipped、running、
  终态、手动创建）提交后回调给桥层统一推送 ``automation_run_updated`` 事件。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Callable, Optional

from core.automation.models import (
    ACTIVE_RUN_STATUSES,
    FINISH_RUN_STATUSES,
)
from core.automation.scheduling import RUN_AT_FORMAT, compute_next_run

logger = logging.getLogger(__name__)

# 每任务保留的运行记录条数（设计规格 5.6）。
RUNS_RETENTION_PER_TASK = 50

# 「错过触发」判定容差：引擎启动时 next_run_at 早于 now-60s 才算错过（设计规格 8.4）。
CATCHUP_GRACE_SECONDS = 60.0

# 一次性任务的陈旧容差：换算后的触发时刻最多允许落后当前时刻这么多秒
# （分钟级换算截断会丢秒，给一分钟容差避免「刚换算就被判过期」）。
ONCE_STALE_TOLERANCE_SECONDS = 60.0


def _normalize_once_config(config: dict) -> dict:
    """once 触发器落库前归一化：``delay_minutes`` 换算成绝对 ``run_at``。

    相对延迟只在保存那一刻由服务端真实时钟锚定一次，换算结果落库、原始
    ``delay_minutes`` 不落库——否则每次编辑保存都会按「当前时刻」重算，
    触发时刻被一路往后推（设计规格 3.1 的 ZCode 1.4-2 移植）。
    """
    if config.get("run_at") is not None:
        return dict(config)
    delay_minutes = config.get("delay_minutes")
    if isinstance(delay_minutes, bool) or not isinstance(delay_minutes, int):
        return dict(config)  # 非法值原样留给校验层报错
    fire_ts = time.time() + delay_minutes * 60
    return {"run_at": datetime.fromtimestamp(fire_ts).strftime(RUN_AT_FORMAT)}


def _normalize_max_runs(value) -> Optional[int]:
    """``max_runs`` 落库归一化：空 = 不限次；其余必须是 ≥1 的整数（bool 不算）。"""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("最多执行次数必须是 ≥1 的整数（留空表示不限次）")
    return int(value)

_TASK_COLUMNS = (
    "id",
    "name",
    "enabled",
    "trigger_type",
    "trigger_config",
    "target_session",
    "prompt",
    "permission_mode",
    "missed_run_policy",
    "notify_on_success",
    "created_at",
    "updated_at",
    "last_run_at",
    "next_run_at",
    "workdir",
    "model_id",
    "max_runs",
    "scheduled_run_count",
    "status",
)

_RUN_COLUMNS = (
    "id",
    "task_id",
    "task_name",
    "trigger_kind",
    "status",
    "session_name",
    "turn_id",
    "error",
    "queued_at",
    "started_at",
    "finished_at",
)

AUTOMATION_TABLE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS automation_tasks (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        trigger_type TEXT NOT NULL,
        trigger_config TEXT NOT NULL DEFAULT '{}',
        target_session TEXT NOT NULL,
        prompt TEXT NOT NULL,
        permission_mode TEXT NOT NULL DEFAULT 'confirm',
        missed_run_policy TEXT NOT NULL DEFAULT 'skip',
        notify_on_success INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_run_at TEXT,
        next_run_at REAL,
        workdir TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT '',
        max_runs INTEGER,
        scheduled_run_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_automation_tasks_next_run
        ON automation_tasks (enabled, next_run_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS automation_runs (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        task_name TEXT NOT NULL DEFAULT '',
        trigger_kind TEXT NOT NULL DEFAULT 'scheduled',
        status TEXT NOT NULL,
        session_name TEXT NOT NULL DEFAULT '',
        turn_id TEXT,
        error TEXT,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_automation_runs_task
        ON automation_runs (task_id, queued_at DESC)
    """,
)

RunUpdatedCallback = Callable[[dict], None]
NextRunComputer = Callable[[str, dict, float], Optional[float]]


def iso_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_automation_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _row_to_task(row) -> dict:
    values = dict(zip(_TASK_COLUMNS, tuple(row)))
    try:
        values["trigger_config"] = json.loads(values.get("trigger_config") or "{}")
    except (TypeError, ValueError):
        values["trigger_config"] = {}
    return values


def _row_to_run(row) -> dict:
    return dict(zip(_RUN_COLUMNS, tuple(row)))


class AutomationStorage:
    """自动化任务与运行记录 DAO。持有 MemoryManager，复用其连接与可重入锁。"""

    def __init__(self, manager):
        self._manager = manager
        # 认领路径产生的运行行（queued/skipped）提交后回调；由桥层注册。
        self.on_run_updated: Optional[RunUpdatedCallback] = None
        self._create_tables()

    # -- 基础 --

    @property
    def conn(self):
        return self._manager.conn

    def _create_tables(self) -> None:
        with self._manager._lock:
            cursor = self.conn.cursor()
            for ddl in AUTOMATION_TABLE_DDL:
                cursor.execute(ddl)
            # 旧库迁移：workdir/model_id 列后加（v1.2.x 前的任务表没有这些列），幂等
            cursor.execute("PRAGMA table_info(automation_tasks)")
            existing_cols = {row[1] for row in cursor.fetchall()}
            if "workdir" not in existing_cols:
                cursor.execute(
                    "ALTER TABLE automation_tasks ADD COLUMN workdir TEXT NOT NULL DEFAULT ''"
                )
            if "model_id" not in existing_cols:
                cursor.execute(
                    "ALTER TABLE automation_tasks ADD COLUMN model_id TEXT NOT NULL DEFAULT ''"
                )
            # 有限次执行三列（设计规格 3.6）：存量任务落 NULL / 0 / 'active'，
            # 语义与升级前逐字节一致（无限循环照跑）
            if "max_runs" not in existing_cols:
                cursor.execute(
                    "ALTER TABLE automation_tasks ADD COLUMN max_runs INTEGER"
                )
            if "scheduled_run_count" not in existing_cols:
                cursor.execute(
                    "ALTER TABLE automation_tasks ADD COLUMN scheduled_run_count INTEGER NOT NULL DEFAULT 0"
                )
            if "status" not in existing_cols:
                cursor.execute(
                    "ALTER TABLE automation_tasks ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
                )
            self.conn.commit()

    # -- 任务 CRUD --

    def list_tasks(self) -> list[dict]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            rows = cursor.execute(
                f"SELECT {', '.join(_TASK_COLUMNS)} FROM automation_tasks ORDER BY created_at, rowid"
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            row = cursor.execute(
                f"SELECT {', '.join(_TASK_COLUMNS)} FROM automation_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return _row_to_task(row) if row is not None else None

    def count_tasks(self) -> int:
        with self._manager._lock:
            cursor = self.conn.cursor()
            row = cursor.execute("SELECT COUNT(*) FROM automation_tasks").fetchone()
        return int(row[0]) if row else 0

    def save_task(self, task: dict) -> str:
        """插入或更新任务；保存时统一重算 next_run_at（startup 恒 NULL）。

        once 任务：``delay_minutes`` 先归一化成 ``run_at`` 再落库，且换算出的
        触发时刻已在过去（含 60 秒容差）时拒绝保存，引导调用方改用
        ``delay_minutes``（设计规格 3.1 的写库前陈旧拒绝）。
        """
        now = iso_now()
        trigger_type = str(task.get("trigger_type") or "")
        config = task.get("trigger_config") if isinstance(task.get("trigger_config"), dict) else {}
        if trigger_type == "once":
            config = _normalize_once_config(config)
        if trigger_type == "startup":
            next_run: Optional[float] = None
        else:
            next_run = compute_next_run(trigger_type, config, time.time())
        if trigger_type == "once" and (
            next_run is None or next_run <= time.time() - ONCE_STALE_TOLERANCE_SECONDS
        ):
            raise ValueError(
                f"一次性触发时刻已过（{config.get('run_at')}），请改用 delay_minutes "
                "指定「距现在多少分钟」后重试"
            )
        max_runs = _normalize_max_runs(task.get("max_runs"))
        with self._manager._lock:
            cursor = self.conn.cursor()
            task_id = str(task.get("id") or "")
            if task_id:
                cursor.execute(
                    "SELECT status FROM automation_tasks WHERE id = ?", (task_id,)
                )
                existing = cursor.fetchone()
                exists = existing is not None
                # 终态不可逆：completed 任务不允许再被编辑（设计规格 3.2）
                if exists and str(existing[0] or "") == "completed":
                    raise ValueError(
                        "任务已完成（执行次数已耗尽），不能再修改；请新建或复制一个任务"
                    )
            else:
                exists = False
                task_id = new_automation_id("task")
            fields = (
                str(task.get("name") or "").strip(),
                1 if task.get("enabled", 1) else 0,
                trigger_type,
                json.dumps(config, ensure_ascii=False),
                str(task.get("target_session") or "").strip(),
                str(task.get("prompt") or ""),
                str(task.get("permission_mode") or "confirm"),
                str(task.get("missed_run_policy") or "skip"),
                1 if task.get("notify_on_success") else 0,
                str(task.get("workdir") or "").strip(),
                str(task.get("model_id") or "").strip(),
            )
            if exists:
                # 只允许改写 max_runs；scheduled_run_count / status 属于生命周期
                # 状态，编辑保存绝不重置（设计规格 3.2「编辑不重置锚点」）
                cursor.execute(
                    """
                    UPDATE automation_tasks
                    SET name = ?, enabled = ?, trigger_type = ?, trigger_config = ?,
                        target_session = ?, prompt = ?, permission_mode = ?,
                        missed_run_policy = ?, notify_on_success = ?, workdir = ?,
                        model_id = ?, updated_at = ?, next_run_at = ?, max_runs = ?
                    WHERE id = ?
                    """,
                    (*fields, now, next_run, max_runs, task_id),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO automation_tasks (
                        id, name, enabled, trigger_type, trigger_config,
                        target_session, prompt, permission_mode, missed_run_policy,
                        notify_on_success, workdir, model_id, max_runs,
                        scheduled_run_count, status, created_at, updated_at,
                        last_run_at, next_run_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'active', ?, ?, NULL, ?)
                    """,
                    (task_id, *fields, max_runs, now, now, next_run),
                )
            self.conn.commit()
        return task_id

    def set_enabled(self, task_id: str, enabled: bool) -> bool:
        """启停任务；重新启用时对定时触发器重算 next_run_at，绝不在过去触发。

        completed 是自然终态、不可逆：一律返回 False（设计规格 3.2「completed
        不可 restart」）。once 任务重新启用时若算不出未来的触发时刻（run_at 已
        过期/无法解析），同样拒绝——否则会留下一个永远不触发却显示「启用」的任务。
        """
        now = iso_now()
        with self._manager._lock:
            cursor = self.conn.cursor()
            row = cursor.execute(
                "SELECT trigger_type, trigger_config, enabled, status FROM automation_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return False
            if str(row[3] or "") == "completed":
                return False
            next_run: Optional[float] = None
            if enabled and row[0] != "startup":
                try:
                    config = json.loads(row[1] or "{}")
                except (TypeError, ValueError):
                    config = {}
                next_run = compute_next_run(str(row[0]), config, time.time())
                if str(row[0]) == "once" and (
                    next_run is None
                    or next_run <= time.time() - ONCE_STALE_TOLERANCE_SECONDS
                ):
                    return False
            cursor.execute(
                "UPDATE automation_tasks SET enabled = ?, updated_at = ?, next_run_at = ? WHERE id = ?",
                (1 if enabled else 0, now, next_run, task_id),
            )
            self.conn.commit()
        return True

    def set_next_run_raw(self, task_id: str, next_run_at: Optional[float]) -> None:
        """直接覆盖 next_run_at。仅供测试与存储内部维护使用。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE automation_tasks SET next_run_at = ? WHERE id = ?",
                (next_run_at, task_id),
            )
            self.conn.commit()

    def delete_task(self, task_id: str) -> bool:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM automation_runs WHERE task_id = ?", (task_id,))
            cursor.execute("DELETE FROM automation_tasks WHERE id = ?", (task_id,))
            deleted = cursor.rowcount > 0
            self.conn.commit()
        return deleted

    # -- 运行记录 --

    def list_runs(self, task_id: Optional[str] = None, limit: int = RUNS_RETENTION_PER_TASK) -> list[dict]:
        query = f"SELECT {', '.join(_RUN_COLUMNS)} FROM automation_runs"
        params: list = []
        if task_id is not None:
            query += " WHERE task_id = ?"
            params.append(task_id)
        query += " ORDER BY queued_at DESC, rowid DESC LIMIT ?"
        params.append(int(limit))
        with self._manager._lock:
            cursor = self.conn.cursor()
            rows = cursor.execute(query, params).fetchall()
        return [_row_to_run(row) for row in rows]

    def clear_runs(self, task_id: str) -> int:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM automation_runs WHERE task_id = ?", (task_id,))
            deleted = cursor.rowcount
            self.conn.commit()
        return deleted

    def has_active_run(self, task_id: str) -> bool:
        placeholders = ", ".join("?" for _ in ACTIVE_RUN_STATUSES)
        with self._manager._lock:
            cursor = self.conn.cursor()
            row = cursor.execute(
                f"SELECT 1 FROM automation_runs WHERE task_id = ? AND status IN ({placeholders}) LIMIT 1",
                (task_id, *ACTIVE_RUN_STATUSES),
            ).fetchone()
        return row is not None

    def mark_run_running(self, run_id: str) -> bool:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE automation_runs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                (iso_now(), run_id),
            )
            updated = cursor.rowcount > 0
            run = self._get_run_locked(cursor, run_id) if updated else None
            self.conn.commit()
        if run is not None:
            self._fire_run_updated(run)
        return updated

    def set_run_turn_id(self, run_id: str, turn_id: str) -> None:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE automation_runs SET turn_id = ? WHERE id = ?", (turn_id, run_id)
            )
            self.conn.commit()

    def finish_run(self, run_id: str, status: str, error: str = "") -> bool:
        """写入运行终态。exactly-once：只有 queued/running 能被收尾。"""
        if status not in FINISH_RUN_STATUSES:
            raise ValueError(f"非法的运行终态: {status!r}")
        with self._manager._lock:
            cursor = self.conn.cursor()
            placeholders = ", ".join("?" for _ in ACTIVE_RUN_STATUSES)
            cursor.execute(
                f"""
                UPDATE automation_runs
                SET status = ?, error = ?, finished_at = ?
                WHERE id = ? AND status IN ({placeholders})
                """,
                (status, error or "", iso_now(), run_id, *ACTIVE_RUN_STATUSES),
            )
            updated = cursor.rowcount > 0
            run = self._get_run_locked(cursor, run_id) if updated else None
            self.conn.commit()
        if run is not None:
            self._fire_run_updated(run)
        return updated

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            return self._get_run_locked(cursor, run_id)

    def _get_run_locked(self, cursor, run_id: str) -> Optional[dict]:
        row = cursor.execute(
            f"SELECT {', '.join(_RUN_COLUMNS)} FROM automation_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        return _row_to_run(row) if row is not None else None

    def recover_stale_runs(self) -> int:
        """启动恢复：上个进程遗留的 queued/running 一律标 interrupted，绝不重跑。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            placeholders = ", ".join("?" for _ in ACTIVE_RUN_STATUSES)
            cursor.execute(
                f"""
                UPDATE automation_runs
                SET status = 'interrupted', error = '应用重启，运行中断', finished_at = ?
                WHERE status IN ({placeholders})
                """,
                (iso_now(), *ACTIVE_RUN_STATUSES),
            )
            recovered = cursor.rowcount
            self.conn.commit()
        return recovered

    def create_run(self, task: dict, *, trigger_kind: str, status: str = "queued", error: str = "") -> dict:
        """公开创建运行行（桥层入口使用），触发 on_run_updated。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            run = self._insert_run_locked(cursor, task, status, trigger_kind=trigger_kind, error=error)
            self.conn.commit()
        self._fire_run_updated(run)
        return run

    def create_manual_run_if_idle(self, task: dict, *, trigger_kind: str = "manual") -> Optional[dict]:
        """原子创建手动运行行：锁内 check+insert（P2-1）。

        同任务已有 queued/running 运行时返回 None（单飞），否则创建 queued 行。
        消除桥层 has_active_run + create_run 两步之间的并发窗口。
        """
        with self._manager._lock:
            cursor = self.conn.cursor()
            # completed 是终态：手动执行同样拒绝（设计规格 3.2）；手动执行不推进
            # scheduled_run_count——试跑不消耗任务生命周期（计数双轨）
            row = cursor.execute(
                "SELECT status FROM automation_tasks WHERE id = ?", (task["id"],)
            ).fetchone()
            if row is None or str(row[0] or "") != "active":
                return None
            if self._has_active_run_locked(cursor, task["id"]):
                return None
            run = self._insert_run_locked(cursor, task, "queued", trigger_kind=trigger_kind)
            self.conn.commit()
        self._fire_run_updated(run)
        return run

    def _insert_run_raw(self, run_id: str, task_id: str, session_name: str, status: str) -> None:
        """测试辅助：直接插入一条最小运行行。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO automation_runs (
                    id, task_id, task_name, trigger_kind, status, session_name,
                    turn_id, error, queued_at, started_at, finished_at
                ) VALUES (?, ?, '', 'scheduled', ?, ?, NULL, NULL, ?, NULL, NULL)
                """,
                (run_id, task_id, status, session_name, iso_now()),
            )
            self.conn.commit()

    def clear_task_last_run_marker(self, task_id: str) -> None:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE automation_tasks SET last_run_at = NULL WHERE id = ?", (task_id,)
            )
            self.conn.commit()

    # -- 引擎认领 --

    def earliest_next_run(self) -> Optional[float]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            row = cursor.execute(
                """
                SELECT MIN(next_run_at) FROM automation_tasks
                WHERE enabled = 1 AND status = 'active'
                  AND trigger_type != 'startup' AND next_run_at IS NOT NULL
                """
            ).fetchone()
        value = row[0] if row else None
        return float(value) if value is not None else None

    def claim_due_tasks(
        self,
        now_ts: float,
        compute_next: NextRunComputer,
        *,
        include_startup: bool = False,
        catchup_enabled: bool = False,
        startup_since: Optional[float] = None,
    ) -> list[tuple[dict, str, str]]:
        """原子认领到期任务，返回 [(task_snapshot, run_id, trigger_kind)]。

        - 常规：CAS UPDATE 认领（trigger_kind="scheduled"）；
        - include_startup（引擎首轮）：startup 任务认领一次（认领后写入
          last_run_at/next_run_at 标记，本轮进程不再触发）。标记是持久化的，
          所以不能用「next_run_at IS NULL」判定：标记写过一次就再不为 NULL，
          任务会永久失效。传 startup_since（本次进程启动时刻）时按「标记早于
          启动时刻」认领——每个进程生命周期恰好触发一次；不传则退化为
          「next_run_at IS NULL 才认领」（仅用于不带进程时钟的裸调用）；
        - catchup_enabled（引擎首轮）：next_run_at 早于容差的启用任务按
          missed_run_policy 处理——skip 留痕重算；run_once 以 "catchup" 认领一次；
        - 单飞：认领前检查同任务是否有 queued/running 运行，命中则写 skipped 行
          且不派发（周期照常推进）。
        """
        claimed: list[tuple[dict, str, str]] = []
        fired: list[dict] = []
        with self._manager._lock:
            cursor = self.conn.cursor()
            if include_startup:
                if startup_since is None:
                    startup_where = (
                        "enabled = 1 AND status = 'active' AND trigger_type = 'startup' "
                        "AND next_run_at IS NULL"
                    )
                    startup_params: tuple = ()
                else:
                    startup_where = (
                        "enabled = 1 AND status = 'active' AND trigger_type = 'startup' "
                        "AND (next_run_at IS NULL OR next_run_at < ?)"
                    )
                    startup_params = (float(startup_since),)
                for row in self._select_tasks_locked(
                    cursor, startup_where, startup_params
                ):
                    task = _row_to_task(row)
                    self._claim_or_skip_locked(
                        cursor,
                        task,
                        None,
                        "startup",
                        claimed,
                        fired,
                        now_ts,
                        startup_since=startup_since,
                    )
            if catchup_enabled:
                threshold = now_ts - CATCHUP_GRACE_SECONDS
                for row in self._select_tasks_locked(
                    cursor,
                    "enabled = 1 AND status = 'active' AND trigger_type != 'startup' AND next_run_at IS NOT NULL AND next_run_at < ?",
                    (threshold,),
                    order="next_run_at",
                ):
                    task = _row_to_task(row)
                    next_ts = self._safe_next(compute_next, task, now_ts)
                    if str(task.get("missed_run_policy")) == "run_once":
                        self._claim_or_skip_locked(
                            cursor, task, next_ts, "catchup", claimed, fired, now_ts
                        )
                    else:
                        self._push_next_run_locked(cursor, task["id"], next_ts)
                        if str(task.get("trigger_type")) == "once":
                            # once + skip：错过即终态（设计规格 3.4）——skipped 留痕
                            # 已写入，这里只落 completed，不再重算下一次
                            cursor.execute(
                                "UPDATE automation_tasks SET status = 'completed' WHERE id = ? AND status = 'active'",
                                (task["id"],),
                            )
                        fired.append(
                            self._insert_run_locked(
                                cursor,
                                task,
                                "skipped",
                                trigger_kind="scheduled",
                                error="应用未运行，触发已过期",
                            )
                        )
            for row in self._select_tasks_locked(
                cursor,
                "enabled = 1 AND status = 'active' AND trigger_type != 'startup' AND next_run_at IS NOT NULL AND next_run_at <= ?",
                (now_ts,),
                order="next_run_at",
            ):
                task = _row_to_task(row)
                next_ts = self._safe_next(compute_next, task, now_ts)
                self._claim_or_skip_locked(
                    cursor, task, next_ts, "scheduled", claimed, fired, now_ts
                )
            self.conn.commit()
        for run in fired:
            self._fire_run_updated(run)
        return claimed

    # -- 认领内部 --

    def _select_tasks_locked(self, cursor, where: str, params: tuple = (), order: str = "created_at, rowid"):
        return cursor.execute(
            f"SELECT {', '.join(_TASK_COLUMNS)} FROM automation_tasks WHERE {where} ORDER BY {order}",
            params,
        ).fetchall()

    def _safe_next(self, compute_next: NextRunComputer, task: dict, now_ts: float) -> Optional[float]:
        """计算下一次触发；异常兜底 1 小时后重试，非严格递增值钳制到 now+1。

        once 任务在按计划时刻认领后就不再有下一次，必须在本函数里直接返回 None
        （让 ``_push_next_run_locked`` 把 next_run_at 置 NULL）；一旦走到下面的
        「钳制到 now+1」分支，next_run_at 永远是未来 1 秒，任务会被无限重触发。
        """
        trigger_type = str(task["trigger_type"])
        if trigger_type == "once":
            return None
        try:
            next_ts = compute_next(trigger_type, task["trigger_config"], now_ts)
        except Exception as exc:  # 单任务配置异常不得停摆引擎
            logger.warning("自动化任务 %s 触发时间计算失败: %s", task["id"], exc)
            return now_ts + 3600.0
        if next_ts is None:
            return None
        if next_ts <= now_ts:
            return now_ts + 1.0
        return float(next_ts)

    def _claim_or_skip_locked(
        self,
        cursor,
        task: dict,
        next_ts: Optional[float],
        trigger_kind: str,
        claimed: list[tuple[dict, str, str]],
        fired: list[dict],
        now_ts: float,
        *,
        startup_since: Optional[float] = None,
    ) -> None:
        """CAS 认领一个到期任务；单飞命中或 CAS 失败时不派发。"""
        if self._has_active_run_locked(cursor, task["id"]):
            if next_ts is not None:
                self._push_next_run_locked(cursor, task["id"], next_ts)
            fired.append(
                self._insert_run_locked(
                    cursor,
                    task,
                    "skipped",
                    trigger_kind=trigger_kind,
                    error="上一次运行尚未结束",
                )
            )
            return
        if trigger_kind == "startup":
            # startup 无计划时刻：用 next_run_at 存「本轮进程已触发」的标记。
            # 标记持久化在库里，故 CAS 不能要求它恒为 NULL（那样标记一写就
            # 永久失效）；传了 startup_since（进程启动时刻）时，条件放宽为
            # 「标记早于本次启动」，同一进程内第二轮仍不会重复触发。
            marker = float(now_ts)
            if startup_since is None:
                cursor.execute(
                    "UPDATE automation_tasks SET next_run_at = ?, last_run_at = ? WHERE id = ? AND enabled = 1 AND next_run_at IS NULL",
                    (marker, iso_now(), task["id"]),
                )
            else:
                cursor.execute(
                    "UPDATE automation_tasks SET next_run_at = ?, last_run_at = ? WHERE id = ? AND enabled = 1 AND (next_run_at IS NULL OR next_run_at < ?)",
                    (marker, iso_now(), task["id"], float(startup_since)),
                )
        else:
            cursor.execute(
                "UPDATE automation_tasks SET next_run_at = ?, last_run_at = ? WHERE id = ? AND enabled = 1 AND next_run_at IS ?",
                (next_ts, iso_now(), task["id"], task["next_run_at"]),
            )
        if cursor.rowcount != 1:
            return  # 已被其他线程/进程抢先认领
        if trigger_kind != "startup":
            # 只对定时/补跑派发计数（设计规格 3.2）：startup 每次进程启动只触发
            # 一次，不构成有限次生命周期
            self._consume_scheduled_run_locked(cursor, task)
        claimed.append((task, self._insert_run_locked(cursor, task, "queued", trigger_kind=trigger_kind)["id"], trigger_kind))

    def _push_next_run_locked(self, cursor, task_id: str, next_ts: Optional[float]) -> None:
        cursor.execute(
            "UPDATE automation_tasks SET next_run_at = ? WHERE id = ?", (next_ts, task_id)
        )

    def _consume_scheduled_run_locked(self, cursor, task: dict) -> None:
        """认领成功后推进 scheduled_run_count；耗尽即落终态 completed（规格 3.2）。

        计数与终态和 CAS 认领同事务（``claim_due_tasks`` 的单个 commit）：要么
        「已计数且已排下一次」，要么整体回滚，不存在计数与运行行不一致的中间态。
        达 ``max_runs`` 的这一次也照常派发——第 N 次执行完才收工（不是第 N-1 次）。
        """
        cursor.execute(
            "UPDATE automation_tasks SET scheduled_run_count = scheduled_run_count + 1 "
            "WHERE id = ? AND status = 'active'",
            (task["id"],),
        )
        if cursor.rowcount != 1:
            return  # 任务已被删除/已终态：不推进计数
        row = cursor.execute(
            "SELECT scheduled_run_count, max_runs FROM automation_tasks WHERE id = ?",
            (task["id"],),
        ).fetchone()
        if row is None:
            return
        count = int(row[0] or 0)
        max_runs = row[1]
        exhausted = max_runs is not None and count >= int(max_runs)
        if not exhausted and str(task.get("trigger_type")) == "once":
            # once 即 max_runs=1 的有限任务：认领一次就终态，不另造一次性分支
            exhausted = count >= 1
        if exhausted:
            # 覆盖本函数调用方刚写的 next_ts：终态任务不再有任何触发时刻
            cursor.execute(
                "UPDATE automation_tasks SET status = 'completed', next_run_at = NULL "
                "WHERE id = ?",
                (task["id"],),
            )

    def _has_active_run_locked(self, cursor, task_id: str) -> bool:
        placeholders = ", ".join("?" for _ in ACTIVE_RUN_STATUSES)
        row = cursor.execute(
            f"SELECT 1 FROM automation_runs WHERE task_id = ? AND status IN ({placeholders}) LIMIT 1",
            (task_id, *ACTIVE_RUN_STATUSES),
        ).fetchone()
        return row is not None

    def _insert_run_locked(
        self,
        cursor,
        task: dict,
        status: str,
        *,
        trigger_kind: str,
        error: str = "",
    ) -> dict:
        run_id = new_automation_id("run")
        queued_at = iso_now()
        cursor.execute(
            """
            INSERT INTO automation_runs (
                id, task_id, task_name, trigger_kind, status, session_name,
                turn_id, error, queued_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, NULL)
            """,
            (run_id, task["id"], task["name"], trigger_kind, status, task["target_session"], error, queued_at),
        )
        self._prune_runs_locked(cursor, task["id"])
        return {
            "id": run_id,
            "task_id": task["id"],
            "task_name": task["name"],
            "trigger_kind": trigger_kind,
            "status": status,
            "session_name": task["target_session"],
            "turn_id": None,
            "error": error,
            "queued_at": queued_at,
            "started_at": None,
            "finished_at": None,
        }

    def _prune_runs_locked(self, cursor, task_id: str) -> None:
        cursor.execute(
            f"""
            DELETE FROM automation_runs
            WHERE task_id = ? AND id NOT IN (
                SELECT id FROM automation_runs WHERE task_id = ?
                ORDER BY queued_at DESC, rowid DESC LIMIT {int(RUNS_RETENTION_PER_TASK)}
            )
            """,
            (task_id, task_id),
        )

    def _fire_run_updated(self, run: dict) -> None:
        callback = self.on_run_updated
        if callback is None:
            return
        try:
            callback(dict(run))
        except Exception as exc:  # 回调异常绝不影响存储层
            logger.warning("automation on_run_updated 回调失败: %s", exc)
