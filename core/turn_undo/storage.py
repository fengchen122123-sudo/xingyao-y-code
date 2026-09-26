"""星瑶按回合撤销：SQLite 元数据、内容寻址快照仓库与 GC。

- 表结构 DDL 由 :func:`create_turn_undo_tables` 提供，由 ``MemoryManager.create_tables``
  幂等调用（与现有迁移同事务、同锁规范）。
- 所有 DAO 写入函数显式 ``commit()``；参与外部事务的 ``*_locked`` 变体只拿游标，
  由调用方统一提交（message_turn_links 必须与消息正文同事务）。
- 文件字节保存在内容寻址仓库（YCODE_DATA_DIR 优先的 ``undo_blobs/``），
  数据库只保存 SHA-256 引用；长时间文件 I/O 期间不持有 SQLite 连接锁。
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable, Optional

from core.turn_undo.models import (
    CHECKPOINT_TERMINAL_STATUSES,
    FileStageStatus,
    NodeKind,
    OperationKind,
    OperationStatus,
    OwnerKind,
    PositionKind,
    RecoveryAction,
    RollbackKind,
    SnapshotAvailability,
    TurnUndoError,
    TurnUndoStatus,
    UndoErrorCode,
    require_checkpoint_transition,
    require_operation_transition,
)

logger = logging.getLogger(__name__)

# 检查点/操作行的 schema 版本；未知版本显式失败。
TURN_UNDO_SCHEMA_VERSION = 1

# 每会话保留的完整检查点数量（与旧内存检查点上限保持一致，可配置）。
DEFAULT_CHECKPOINT_RETENTION = 50

# 确认令牌默认有效期（毫秒）。
DEFAULT_TOKEN_TTL_MS = 5 * 60 * 1000

# 当前进程随机 boot_id：重启后旧令牌全部失效。
BOOT_ID = secrets.token_hex(16)

_BLOB_GC_LOCK = threading.Lock()


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# DDL（由 MemoryManager.create_tables 幂等调用）
# ---------------------------------------------------------------------------

TURN_UNDO_TABLE_DDL = (
    '''
    CREATE TABLE IF NOT EXISTS turn_undo_checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        session_id INTEGER NOT NULL,
        turn_id TEXT NOT NULL,
        workspace_root TEXT NOT NULL,
        status TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        owner_kind TEXT NOT NULL DEFAULT 'foreground_turn',
        parent_checkpoint_id TEXT,
        started_at_ms INTEGER NOT NULL,
        completed_at_ms INTEGER,
        reverted_at_ms INTEGER,
        has_uncovered_command_effects INTEGER NOT NULL DEFAULT 0,
        unavailable_code TEXT,
        last_error_code TEXT,
        last_error_message TEXT,
        schema_version INTEGER NOT NULL,
        record_version INTEGER NOT NULL DEFAULT 1,
        snapshot_availability TEXT NOT NULL DEFAULT 'available',
        snapshots_expired_at_ms INTEGER,
        UNIQUE(turn_id),
        FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
        FOREIGN KEY(parent_checkpoint_id) REFERENCES turn_undo_checkpoints(checkpoint_id) ON DELETE SET NULL
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_turn_undo_checkpoints_session
        ON turn_undo_checkpoints(session_id, started_at_ms)
    ''',
    '''
    CREATE TABLE IF NOT EXISTS turn_undo_files (
        file_id TEXT PRIMARY KEY,
        checkpoint_id TEXT NOT NULL,
        canonical_path TEXT NOT NULL,
        display_path TEXT NOT NULL,
        before_kind TEXT NOT NULL,
        before_exists INTEGER NOT NULL,
        before_size INTEGER,
        before_sha256 TEXT,
        before_blob_sha256 TEXT,
        before_mode INTEGER,
        before_file_identity TEXT,
        before_link_count INTEGER,
        before_symlink_target TEXT,
        after_kind TEXT,
        after_exists INTEGER,
        after_size INTEGER,
        after_sha256 TEXT,
        after_blob_sha256 TEXT,
        after_mode INTEGER,
        after_file_identity TEXT,
        after_link_count INTEGER,
        after_symlink_target TEXT,
        net_added INTEGER NOT NULL DEFAULT 0,
        net_removed INTEGER NOT NULL DEFAULT 0,
        diff_kind TEXT,
        first_mutated_at_ms INTEGER NOT NULL,
        last_mutated_at_ms INTEGER NOT NULL,
        last_source_kind TEXT,
        last_source_id TEXT,
        last_tool_name TEXT,
        tool_call_id TEXT NOT NULL,
        verified_at_ms INTEGER,
        undo_result TEXT,
        undo_error_code TEXT,
        UNIQUE(checkpoint_id, canonical_path),
        FOREIGN KEY(checkpoint_id) REFERENCES turn_undo_checkpoints(checkpoint_id) ON DELETE CASCADE
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_turn_undo_files_checkpoint
        ON turn_undo_files(checkpoint_id)
    ''',
    '''
    CREATE TABLE IF NOT EXISTS turn_undo_operations (
        operation_id TEXT PRIMARY KEY,
        checkpoint_id TEXT NOT NULL,
        parent_operation_id TEXT,
        resolved_by_operation_id TEXT,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        preview_version INTEGER NOT NULL,
        started_at_ms INTEGER,
        finished_at_ms INTEGER,
        confirmed_at_ms INTEGER,
        last_error_code TEXT,
        last_error_message TEXT,
        schema_version INTEGER NOT NULL,
        FOREIGN KEY(checkpoint_id) REFERENCES turn_undo_checkpoints(checkpoint_id) ON DELETE CASCADE,
        FOREIGN KEY(parent_operation_id) REFERENCES turn_undo_operations(operation_id) ON DELETE SET NULL,
        FOREIGN KEY(resolved_by_operation_id) REFERENCES turn_undo_operations(operation_id) ON DELETE SET NULL
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_turn_undo_operations_checkpoint
        ON turn_undo_operations(checkpoint_id)
    ''',
    '''
    CREATE TABLE IF NOT EXISTS turn_undo_operation_files (
        operation_file_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        file_id TEXT NOT NULL,
        apply_ordinal INTEGER NOT NULL,
        expected_current_fingerprint TEXT NOT NULL,
        rollback_kind TEXT NOT NULL,
        rollback_blob_sha256 TEXT,
        rollback_mode INTEGER,
        target_kind TEXT NOT NULL,
        target_blob_sha256 TEXT,
        target_mode INTEGER,
        stage_status TEXT NOT NULL,
        apply_status TEXT NOT NULL,
        verify_status TEXT NOT NULL,
        last_error_code TEXT,
        UNIQUE(operation_id, file_id),
        UNIQUE(operation_id, apply_ordinal),
        FOREIGN KEY(operation_id) REFERENCES turn_undo_operations(operation_id) ON DELETE CASCADE,
        FOREIGN KEY(file_id) REFERENCES turn_undo_files(file_id) ON DELETE CASCADE
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_turn_undo_operation_files_operation
        ON turn_undo_operation_files(operation_id)
    ''',
    '''
    CREATE TABLE IF NOT EXISTS turn_undo_confirmations (
        confirmation_id TEXT PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        boot_id TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        checkpoint_id TEXT NOT NULL,
        record_version INTEGER NOT NULL,
        workspace_identity TEXT NOT NULL,
        fingerprint_set_sha256 TEXT NOT NULL,
        expires_at_ms INTEGER NOT NULL,
        consumed_at_ms INTEGER,
        FOREIGN KEY(operation_id) REFERENCES turn_undo_operations(operation_id) ON DELETE CASCADE,
        FOREIGN KEY(checkpoint_id) REFERENCES turn_undo_checkpoints(checkpoint_id) ON DELETE CASCADE
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS workspace_corrections (
        correction_id TEXT PRIMARY KEY,
        workspace_root TEXT NOT NULL,
        session_id INTEGER NOT NULL,
        turn_id TEXT NOT NULL,
        operation_id TEXT NOT NULL UNIQUE,
        files_summary TEXT NOT NULL,
        affected_count INTEGER NOT NULL,
        has_uncovered_command_effects INTEGER NOT NULL DEFAULT 0,
        subsequent_completed_turns INTEGER NOT NULL DEFAULT 0,
        created_at_ms INTEGER NOT NULL
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_workspace_corrections_root
        ON workspace_corrections(workspace_root, created_at_ms)
    ''',
    '''
    CREATE TABLE IF NOT EXISTS message_turn_links (
        message_id INTEGER PRIMARY KEY,
        checkpoint_id TEXT NOT NULL,
        position_kind TEXT NOT NULL,
        FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
        FOREIGN KEY(checkpoint_id) REFERENCES turn_undo_checkpoints(checkpoint_id) ON DELETE CASCADE
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS idx_message_turn_links_checkpoint
        ON message_turn_links(checkpoint_id)
    ''',
)


def create_turn_undo_tables(cursor: sqlite3.Cursor) -> None:
    """幂等建表；由 MemoryManager.create_tables 在其事务与锁内调用。"""
    for statement in TURN_UNDO_TABLE_DDL:
        cursor.execute(statement)


# ---------------------------------------------------------------------------
# 内容寻址快照仓库
# ---------------------------------------------------------------------------

def _default_data_dir() -> Path:
    data_dir = os.environ.get("YCODE_DATA_DIR")
    if data_dir:
        return Path(data_dir)
    return Path.home() / ".ycode"


class BlobStore:
    """SHA-256 内容寻址仓库：先写临时文件、刷新校验、再原子命名。

    相同内容只保存一份；读取时强制校验哈希，损坏显式失败。
    """

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else _default_data_dir() / "undo_blobs"
        self.staging = self.root / ".staging"
        self.staging.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def blob_path(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256

    def exists(self, sha256: str) -> bool:
        return self.blob_path(sha256).is_file()

    def put_bytes(self, data: bytes) -> str:
        import io

        return self.put_stream(io.BytesIO(data))

    def put_stream(self, reader) -> str:
        """流式写入并返回内容哈希；不将大文件整体读入内存。"""
        digest = hashlib.sha256()
        staging_path = self.staging / uuid.uuid4().hex
        try:
            with open(staging_path, "wb") as handle:
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            sha256 = digest.hexdigest()
            final_path = self.blob_path(sha256)
            if final_path.exists():
                # 内容去重：同哈希已存在，丢弃临时文件即可。
                staging_path.unlink(missing_ok=True)
                return sha256
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging_path, final_path)
            return sha256
        finally:
            staging_path.unlink(missing_ok=True)

    def read_bytes(self, sha256: str) -> bytes:
        path = self.blob_path(sha256)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise TurnUndoError(
                UndoErrorCode.SNAPSHOT_MISSING,
                f"快照内容缺失: {sha256[:12]}…",
            )
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            raise TurnUndoError(
                UndoErrorCode.SNAPSHOT_CORRUPT,
                f"快照内容哈希校验失败: {sha256[:12]}…",
            )
        return data


# ---------------------------------------------------------------------------
# DAO 行对象（存储层内部表示）
# ---------------------------------------------------------------------------

class CheckpointRow:
    __slots__ = (
        "checkpoint_id", "session_id", "turn_id", "workspace_root", "status",
        "description", "owner_kind", "parent_checkpoint_id", "started_at_ms",
        "completed_at_ms", "reverted_at_ms", "has_uncovered_command_effects",
        "unavailable_code", "last_error_code", "last_error_message",
        "schema_version", "record_version", "snapshot_availability",
        "snapshots_expired_at_ms",
    )

    def __init__(self, row):
        (
            self.checkpoint_id, self.session_id, self.turn_id, self.workspace_root,
            self.status, self.description, self.owner_kind, self.parent_checkpoint_id,
            self.started_at_ms, self.completed_at_ms, self.reverted_at_ms,
            self.has_uncovered_command_effects, self.unavailable_code,
            self.last_error_code, self.last_error_message, self.schema_version,
            self.record_version, self.snapshot_availability,
            self.snapshots_expired_at_ms,
        ) = row

    @property
    def status_enum(self) -> TurnUndoStatus:
        return _parse_status(self.status)

    @property
    def availability_enum(self) -> SnapshotAvailability:
        try:
            return SnapshotAvailability(self.snapshot_availability)
        except ValueError as exc:
            raise TurnUndoError(
                UndoErrorCode.INVALID_STATE_TRANSITION,
                f"未知快照保留状态: {self.snapshot_availability!r}",
            ) from exc


class FileRow:
    __slots__ = (
        "file_id", "checkpoint_id", "canonical_path", "display_path",
        "before_kind", "before_exists", "before_size", "before_sha256",
        "before_blob_sha256", "before_mode", "before_file_identity",
        "before_link_count", "before_symlink_target",
        "after_kind", "after_exists", "after_size", "after_sha256",
        "after_blob_sha256", "after_mode", "after_file_identity",
        "after_link_count", "after_symlink_target",
        "net_added", "net_removed", "diff_kind",
        "first_mutated_at_ms", "last_mutated_at_ms",
        "last_source_kind", "last_source_id", "last_tool_name", "tool_call_id",
        "verified_at_ms", "undo_result", "undo_error_code",
    )

    def __init__(self, row):
        (
            self.file_id, self.checkpoint_id, self.canonical_path, self.display_path,
            self.before_kind, self.before_exists, self.before_size, self.before_sha256,
            self.before_blob_sha256, self.before_mode, self.before_file_identity,
            self.before_link_count, self.before_symlink_target,
            self.after_kind, self.after_exists, self.after_size, self.after_sha256,
            self.after_blob_sha256, self.after_mode, self.after_file_identity,
            self.after_link_count, self.after_symlink_target,
            self.net_added, self.net_removed, self.diff_kind,
            self.first_mutated_at_ms, self.last_mutated_at_ms,
            self.last_source_kind, self.last_source_id, self.last_tool_name,
            self.tool_call_id, self.verified_at_ms, self.undo_result,
            self.undo_error_code,
        ) = row


class OperationRow:
    __slots__ = (
        "operation_id", "checkpoint_id", "parent_operation_id",
        "resolved_by_operation_id", "kind", "status", "preview_version",
        "started_at_ms", "finished_at_ms", "confirmed_at_ms",
        "last_error_code", "last_error_message", "schema_version",
    )

    def __init__(self, row):
        (
            self.operation_id, self.checkpoint_id, self.parent_operation_id,
            self.resolved_by_operation_id, self.kind, self.status, self.preview_version,
            self.started_at_ms, self.finished_at_ms, self.confirmed_at_ms,
            self.last_error_code, self.last_error_message, self.schema_version,
        ) = row

    @property
    def status_enum(self) -> OperationStatus:
        try:
            return OperationStatus(self.status)
        except ValueError as exc:
            raise TurnUndoError(
                UndoErrorCode.INVALID_STATE_TRANSITION,
                f"未知撤销操作状态: {self.status!r}",
            ) from exc

    @property
    def kind_enum(self) -> OperationKind:
        try:
            return OperationKind(self.kind)
        except ValueError as exc:
            raise TurnUndoError(
                UndoErrorCode.INVALID_STATE_TRANSITION,
                f"未知撤销操作种类: {self.kind!r}",
            ) from exc


class OperationFileRow:
    __slots__ = (
        "operation_file_id", "operation_id", "file_id", "apply_ordinal",
        "expected_current_fingerprint", "rollback_kind", "rollback_blob_sha256",
        "rollback_mode", "target_kind", "target_blob_sha256", "target_mode",
        "stage_status", "apply_status", "verify_status", "last_error_code",
    )

    def __init__(self, row):
        (
            self.operation_file_id, self.operation_id, self.file_id, self.apply_ordinal,
            self.expected_current_fingerprint, self.rollback_kind,
            self.rollback_blob_sha256, self.rollback_mode, self.target_kind,
            self.target_blob_sha256, self.target_mode,
            self.stage_status, self.apply_status, self.verify_status,
            self.last_error_code,
        ) = row


_CHECKPOINT_COLUMNS = (
    "checkpoint_id, session_id, turn_id, workspace_root, status, description, "
    "owner_kind, parent_checkpoint_id, started_at_ms, completed_at_ms, "
    "reverted_at_ms, has_uncovered_command_effects, unavailable_code, "
    "last_error_code, last_error_message, schema_version, record_version, "
    "snapshot_availability, snapshots_expired_at_ms"
)
_FILE_COLUMNS = (
    "file_id, checkpoint_id, canonical_path, display_path, before_kind, "
    "before_exists, before_size, before_sha256, before_blob_sha256, before_mode, "
    "before_file_identity, before_link_count, before_symlink_target, "
    "after_kind, after_exists, after_size, after_sha256, after_blob_sha256, "
    "after_mode, after_file_identity, after_link_count, after_symlink_target, "
    "net_added, net_removed, diff_kind, first_mutated_at_ms, last_mutated_at_ms, "
    "last_source_kind, last_source_id, last_tool_name, tool_call_id, "
    "verified_at_ms, undo_result, undo_error_code"
)
_OPERATION_COLUMNS = (
    "operation_id, checkpoint_id, parent_operation_id, resolved_by_operation_id, "
    "kind, status, preview_version, started_at_ms, finished_at_ms, confirmed_at_ms, "
    "last_error_code, last_error_message, schema_version"
)
_OPERATION_FILE_COLUMNS = (
    "operation_file_id, operation_id, file_id, apply_ordinal, "
    "expected_current_fingerprint, rollback_kind, rollback_blob_sha256, "
    "rollback_mode, target_kind, target_blob_sha256, target_mode, "
    "stage_status, apply_status, verify_status, last_error_code"
)


def _parse_status(value: str) -> TurnUndoStatus:
    try:
        return TurnUndoStatus(value)
    except ValueError as exc:
        raise TurnUndoError(
            UndoErrorCode.INVALID_STATE_TRANSITION,
            f"未知检查点状态: {value!r}",
        ) from exc


def _stage_value(value: FileStageStatus) -> str:
    if not isinstance(value, FileStageStatus):
        raise TurnUndoError(
            UndoErrorCode.INVALID_STATE_TRANSITION,
            f"操作文件阶段状态必须是 FileStageStatus: {value!r}",
        )
    return value.value


# ---------------------------------------------------------------------------
# 存储 DAO
# ---------------------------------------------------------------------------

class TurnUndoStorage:
    """回合撤销元数据 DAO。持有 MemoryManager，复用其连接与可重入锁。"""

    def __init__(self, manager, blob_root: Optional[Path] = None):
        self._manager = manager
        self.blob_store = BlobStore(blob_root)

    # -- 基础 --

    @property
    def conn(self):
        return self._manager.conn

    def resolve_session_id(self, session_name: str) -> int:
        """把可改名的会话名解析为不可变主键 sessions.id；入口路由专用。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT id FROM sessions WHERE session_name = ?",
                (session_name,),
            )
            row = cursor.fetchone()
        if row is None:
            raise TurnUndoError(
                UndoErrorCode.CHECKPOINT_NOT_FOUND,
                f"会话不存在: {session_name!r}",
            )
        return int(row[0])

    # -- 检查点 --

    def create_checkpoint(
        self,
        *,
        session_id: int,
        turn_id: str,
        workspace_root: str,
        owner_kind: OwnerKind,
        parent_checkpoint_id: Optional[str] = None,
        description: str = "",
    ) -> CheckpointRow:
        if not isinstance(owner_kind, OwnerKind):
            raise TurnUndoError(
                UndoErrorCode.INVALID_INPUT,
                f"owner_kind 必须是 OwnerKind: {owner_kind!r}",
            )
        checkpoint_id = new_id("ckpt")
        with self._manager._lock:
            cursor = self.conn.cursor()
            try:
                if parent_checkpoint_id is not None:
                    cursor.execute(
                        "SELECT session_id, workspace_root FROM turn_undo_checkpoints "
                        "WHERE checkpoint_id = ?",
                        (parent_checkpoint_id,),
                    )
                    parent = cursor.fetchone()
                    if parent is None:
                        raise TurnUndoError(
                            UndoErrorCode.CHECKPOINT_NOT_FOUND,
                            f"父检查点不存在: {parent_checkpoint_id}",
                        )
                    if int(parent[0]) != session_id or parent[1] != workspace_root:
                        raise TurnUndoError(
                            UndoErrorCode.OPERATION_SCOPE_MISMATCH,
                            "父检查点与会话/工作区不一致，拒绝建立归属",
                        )
                cursor.execute(
                    "INSERT INTO turn_undo_checkpoints "
                    "(checkpoint_id, session_id, turn_id, workspace_root, status, "
                    "description, owner_kind, parent_checkpoint_id, started_at_ms, "
                    "schema_version, record_version, snapshot_availability) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (
                        checkpoint_id, session_id, turn_id, workspace_root,
                        TurnUndoStatus.RECORDING.value, description,
                        owner_kind.value, parent_checkpoint_id, now_ms(),
                        TURN_UNDO_SCHEMA_VERSION,
                        SnapshotAvailability.NOT_REQUIRED.value,
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_checkpoint(checkpoint_id)

    def get_checkpoint(self, checkpoint_id: str) -> CheckpointRow:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM turn_undo_checkpoints "
                "WHERE checkpoint_id = ?",
                (checkpoint_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise TurnUndoError(
                UndoErrorCode.CHECKPOINT_NOT_FOUND,
                f"检查点不存在: {checkpoint_id}",
            )
        return CheckpointRow(row)

    def get_checkpoint_by_turn(self, session_id: int, turn_id: str) -> Optional[CheckpointRow]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM turn_undo_checkpoints "
                "WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            )
            row = cursor.fetchone()
        return CheckpointRow(row) if row else None

    def get_checkpoint_by_turn_global(self, turn_id: str):
        """按全局唯一 turn_id 查检查点（H2 兜底：父 ctx 已注销的后台任务归属）。

        新协议 turn_id 全局唯一，旧格式（session:hex）不在本表时返回 None。
        """
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM turn_undo_checkpoints "
                "WHERE turn_id = ?",
                (str(turn_id or ""),),
            )
            row = cursor.fetchone()
        return CheckpointRow(row) if row else None

    def transition_checkpoint(
        self,
        checkpoint_id: str,
        target: TurnUndoStatus,
        *,
        unavailable_code: Optional[str] = None,
        last_error_code: Optional[str] = None,
        last_error_message: Optional[str] = None,
        completed_at_ms: Optional[int] = None,
        reverted_at_ms: Optional[int] = None,
        availability: Optional[SnapshotAvailability] = None,
    ) -> CheckpointRow:
        """推进检查点状态；非法迁移显式失败，成功时递增 record_version。"""
        if not isinstance(target, TurnUndoStatus):
            raise TurnUndoError(
                UndoErrorCode.INVALID_STATE_TRANSITION,
                f"目标状态必须是 TurnUndoStatus: {target!r}",
            )
        with self._manager._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "SELECT status FROM turn_undo_checkpoints WHERE checkpoint_id = ?",
                    (checkpoint_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise TurnUndoError(
                        UndoErrorCode.CHECKPOINT_NOT_FOUND,
                        f"检查点不存在: {checkpoint_id}",
                    )
                current = _parse_status(row[0])
                require_checkpoint_transition(current, target)
                sets = ["status = ?", "record_version = record_version + 1"]
                params: list = [target.value]
                if unavailable_code is not None:
                    sets.append("unavailable_code = ?")
                    params.append(unavailable_code)
                if last_error_code is not None:
                    sets.append("last_error_code = ?")
                    params.append(last_error_code)
                if last_error_message is not None:
                    sets.append("last_error_message = ?")
                    params.append(last_error_message)
                if completed_at_ms is not None:
                    sets.append("completed_at_ms = ?")
                    params.append(completed_at_ms)
                if reverted_at_ms is not None:
                    sets.append("reverted_at_ms = ?")
                    params.append(reverted_at_ms)
                if availability is not None:
                    sets.append("snapshot_availability = ?")
                    params.append(availability.value)
                params.append(checkpoint_id)
                cursor.execute(
                    f"UPDATE turn_undo_checkpoints SET {', '.join(sets)} "
                    "WHERE checkpoint_id = ? AND status = ?",
                    (*params, current.value),
                )
                if cursor.rowcount != 1:
                    raise TurnUndoError(
                        UndoErrorCode.INVALID_STATE_TRANSITION,
                        f"检查点状态并发迁移失败: {checkpoint_id}",
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_checkpoint(checkpoint_id)

    def update_snapshot_availability(
        self,
        checkpoint_id: str,
        availability: SnapshotAvailability,
        *,
        expired_at_ms: Optional[int] = None,
    ) -> None:
        """快照保留状态更新是正交字段，不迁移生命周期。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "UPDATE turn_undo_checkpoints SET snapshot_availability = ?, "
                    "snapshots_expired_at_ms = COALESCE(?, snapshots_expired_at_ms) "
                    "WHERE checkpoint_id = ?",
                    (availability.value, expired_at_ms, checkpoint_id),
                )
                if cursor.rowcount != 1:
                    raise TurnUndoError(
                        UndoErrorCode.CHECKPOINT_NOT_FOUND,
                        f"检查点不存在: {checkpoint_id}",
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def mark_uncovered_command_effects(self, checkpoint_id: str) -> None:
        """回合内执行过通用终端命令：持久化未覆盖副作用事实。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "UPDATE turn_undo_checkpoints "
                    "SET has_uncovered_command_effects = 1, "
                    "record_version = record_version + 1 WHERE checkpoint_id = ?",
                    (checkpoint_id,),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def compare_and_swap_reverting(
        self,
        checkpoint_id: str,
        *,
        expected_record_version: int,
    ) -> CheckpointRow:
        """短事务 CAS：校验版本并把 ready 检查点推进到 reverting（消费令牌同事务）。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            try:
                self.compare_and_swap_reverting_in_tx(
                    cursor,
                    checkpoint_id=checkpoint_id,
                    expected_record_version=expected_record_version,
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_checkpoint(checkpoint_id)

    def compare_and_swap_reverting_in_tx(
        self,
        cursor,
        *,
        checkpoint_id: str,
        expected_record_version: int,
    ) -> None:
        """CAS 的游标内变体：由调用方与其他写入同事务提交/回滚。"""
        cursor.execute(
            "UPDATE turn_undo_checkpoints "
            "SET status = ?, record_version = record_version + 1 "
            "WHERE checkpoint_id = ? AND status = ? AND record_version = ?",
            (
                TurnUndoStatus.REVERTING.value,
                checkpoint_id,
                TurnUndoStatus.READY.value,
                int(expected_record_version),
            ),
        )
        if cursor.rowcount != 1:
            cursor.execute(
                "SELECT status, record_version FROM turn_undo_checkpoints "
                "WHERE checkpoint_id = ?",
                (checkpoint_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise TurnUndoError(
                    UndoErrorCode.CHECKPOINT_NOT_FOUND,
                    f"检查点不存在: {checkpoint_id}",
                )
            raise TurnUndoError(
                UndoErrorCode.INVALID_STATE_TRANSITION,
                "检查点版本或状态已变化（CAS 失败），"
                f"当前 status={row[0]!r} record_version={row[1]}",
            )

    # -- 文件记录 --

    def insert_file_locked(self, cursor, *, checkpoint_id: str, values: dict) -> str:
        """在调用方事务内插入文件记录；返回 file_id。"""
        file_id = new_id("file")
        cursor.execute(
            f"INSERT INTO turn_undo_files ({_FILE_COLUMNS}) "
            "VALUES (" + ",".join("?" * 34) + ")",
            (
                file_id, checkpoint_id, values["canonical_path"], values["display_path"],
                values["before_kind"], int(values["before_exists"]),
                values["before_size"], values["before_sha256"],
                values["before_blob_sha256"], values["before_mode"],
                values["before_file_identity"], values["before_link_count"],
                values["before_symlink_target"],
                values["after_kind"], values["after_exists"], values["after_size"],
                values["after_sha256"], values["after_blob_sha256"],
                values["after_mode"], values["after_file_identity"],
                values["after_link_count"], values["after_symlink_target"],
                0, 0, values["diff_kind"],
                values["mutated_at_ms"], values["mutated_at_ms"],
                values["source_kind"], values["source_id"], values["tool_name"],
                values["tool_call_id"],
                None, None, None,
            ),
        )
        cursor.execute(
            "UPDATE turn_undo_checkpoints SET record_version = record_version + 1 "
            "WHERE checkpoint_id = ?",
            (checkpoint_id,),
        )
        return file_id

    def update_after_plan_locked(self, cursor, *, file_id: str, checkpoint_id: str, values: dict) -> None:
        """同一文件一轮多次修改：保留最初 before，用新计划替换最终 after 引用。"""
        cursor.execute(
            "UPDATE turn_undo_files SET "
            "after_kind = ?, after_exists = ?, after_size = ?, after_sha256 = ?, "
            "after_blob_sha256 = ?, after_mode = ?, after_file_identity = ?, "
            "after_link_count = ?, after_symlink_target = ?, verified_at_ms = NULL, "
            "last_mutated_at_ms = ?, last_source_kind = ?, last_source_id = ?, "
            "last_tool_name = ?, tool_call_id = ? "
            "WHERE file_id = ? AND checkpoint_id = ?",
            (
                values["after_kind"], values["after_exists"], values["after_size"],
                values["after_sha256"], values["after_blob_sha256"],
                values["after_mode"], values["after_file_identity"],
                values["after_link_count"], values["after_symlink_target"],
                values["mutated_at_ms"], values["source_kind"], values["source_id"],
                values["tool_name"], values["tool_call_id"],
                file_id, checkpoint_id,
            ),
        )
        if cursor.rowcount != 1:
            raise TurnUndoError(
                UndoErrorCode.CHECKPOINT_NOT_FOUND,
                f"文件记录不存在: {file_id}",
            )
        cursor.execute(
            "UPDATE turn_undo_checkpoints SET record_version = record_version + 1 "
            "WHERE checkpoint_id = ?",
            (checkpoint_id,),
        )

    def update_file_verified_locked(
        self,
        cursor,
        *,
        file_id: str,
        actual: dict,
    ) -> None:
        """verify_after_mutation 通过后写入实际身份与校验结果。"""
        cursor.execute(
            "UPDATE turn_undo_files SET "
            "after_kind = ?, after_exists = ?, after_size = ?, after_sha256 = ?, "
            "after_blob_sha256 = ?, after_mode = ?, after_file_identity = ?, "
            "after_link_count = ?, after_symlink_target = ?, verified_at_ms = ? "
            "WHERE file_id = ?",
            (
                actual["after_kind"], int(actual["after_exists"]), actual["after_size"],
                actual["after_sha256"], actual["after_blob_sha256"],
                actual["after_mode"], actual["after_file_identity"],
                actual["after_link_count"], actual["after_symlink_target"],
                now_ms(),
                file_id,
            ),
        )
        if cursor.rowcount != 1:
            raise TurnUndoError(
                UndoErrorCode.CHECKPOINT_NOT_FOUND,
                f"文件记录不存在: {file_id}",
            )

    def finalize_file_stats_locked(
        self,
        cursor,
        *,
        file_id: str,
        net_added: int,
        net_removed: int,
        diff_kind: str,
    ) -> None:
        cursor.execute(
            "UPDATE turn_undo_files SET net_added = ?, net_removed = ?, diff_kind = ? "
            "WHERE file_id = ?",
            (net_added, net_removed, diff_kind, file_id),
        )

    def refresh_after_identity_locked(
        self, cursor, *, file_id: str, identity: str, mode, link_count,
    ) -> None:
        """自身恢复操作验证通过后，把 after 身份刷新为当前磁盘实际值。

        内容已验证等于已验证 after 快照；File ID/硬链接计数因我方原子替换而
        变化属于正常事实，不应在后续预检中被误判为外部同字节替换。
        """
        cursor.execute(
            "UPDATE turn_undo_files SET after_file_identity = ?, after_mode = ?, "
            "after_link_count = ?, verified_at_ms = ? WHERE file_id = ?",
            (identity, mode, link_count, now_ms(), file_id),
        )

    def set_file_undo_result_locked(
        self,
        cursor,
        *,
        file_id: str,
        undo_result: str,
        undo_error_code: Optional[str],
    ) -> None:
        cursor.execute(
            "UPDATE turn_undo_files SET undo_result = ?, undo_error_code = ? "
            "WHERE file_id = ?",
            (undo_result, undo_error_code, file_id),
        )

    def get_file(self, file_id: str) -> FileRow:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT {_FILE_COLUMNS} FROM turn_undo_files WHERE file_id = ?",
                (file_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise TurnUndoError(
                UndoErrorCode.CHECKPOINT_NOT_FOUND,
                f"文件记录不存在: {file_id}",
            )
        return FileRow(row)

    def get_file_by_path(self, checkpoint_id: str, canonical_path: str) -> Optional[FileRow]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT {_FILE_COLUMNS} FROM turn_undo_files "
                "WHERE checkpoint_id = ? AND canonical_path = ?",
                (checkpoint_id, canonical_path),
            )
            row = cursor.fetchone()
        return FileRow(row) if row else None

    def list_files(self, checkpoint_id: str) -> list[FileRow]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT {_FILE_COLUMNS} FROM turn_undo_files "
                "WHERE checkpoint_id = ? ORDER BY first_mutated_at_ms, file_id",
                (checkpoint_id,),
            )
            rows = cursor.fetchall()
        return [FileRow(row) for row in rows]

    # -- 撤销操作 --

    def create_operation(
        self,
        *,
        checkpoint_id: str,
        kind: OperationKind,
        parent_operation_id: Optional[str] = None,
        preview_version: int = 1,
    ) -> OperationRow:
        if not isinstance(kind, OperationKind):
            raise TurnUndoError(
                UndoErrorCode.INVALID_INPUT,
                f"kind 必须是 OperationKind: {kind!r}",
            )
        operation_id = new_id("op")
        with self._manager._lock:
            cursor = self.conn.cursor()
            try:
                if parent_operation_id is not None:
                    cursor.execute(
                        "SELECT checkpoint_id, status FROM turn_undo_operations "
                        "WHERE operation_id = ?",
                        (parent_operation_id,),
                    )
                    parent = cursor.fetchone()
                    if parent is None:
                        raise TurnUndoError(
                            UndoErrorCode.CHECKPOINT_NOT_FOUND,
                            f"父操作不存在: {parent_operation_id}",
                        )
                    if parent[0] != checkpoint_id:
                        raise TurnUndoError(
                            UndoErrorCode.OPERATION_SCOPE_MISMATCH,
                            "拒绝跨检查点的恢复操作父子关系",
                        )
                    if parent[1] not in (
                        OperationStatus.INTERRUPTED.value,
                        OperationStatus.RECOVERY_REQUIRED.value,
                    ):
                        raise TurnUndoError(
                            UndoErrorCode.OPERATION_SCOPE_MISMATCH,
                            "父操作不处于未解决状态，拒绝建立恢复链",
                        )
                    # 循环链防御：沿 resolved_by 链回溯，禁止引用自己的祖先。
                    seen = {parent_operation_id}
                    cursor_cursor = parent_operation_id
                    while cursor_cursor:
                        cursor.execute(
                            "SELECT parent_operation_id FROM turn_undo_operations "
                            "WHERE operation_id = ?",
                            (cursor_cursor,),
                        )
                        ancestor = cursor.fetchone()
                        if ancestor is None or ancestor[0] is None:
                            break
                        if ancestor[0] in seen:
                            raise TurnUndoError(
                                UndoErrorCode.RECOVERY_OPERATION_CYCLE,
                                "恢复操作链存在循环引用",
                            )
                        seen.add(ancestor[0])
                        cursor_cursor = ancestor[0]
                cursor.execute(
                    f"INSERT INTO turn_undo_operations ({_OPERATION_COLUMNS}) "
                    "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)",
                    (
                        operation_id, checkpoint_id, parent_operation_id,
                        kind.value, OperationStatus.PREVIEWED.value,
                        int(preview_version), now_ms(), TURN_UNDO_SCHEMA_VERSION,
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_operation(operation_id)

    def get_operation(self, operation_id: str) -> OperationRow:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT {_OPERATION_COLUMNS} FROM turn_undo_operations "
                "WHERE operation_id = ?",
                (operation_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise TurnUndoError(
                UndoErrorCode.CHECKPOINT_NOT_FOUND,
                f"撤销操作不存在: {operation_id}",
            )
        return OperationRow(row)

    def transition_operation(
        self,
        operation_id: str,
        target: OperationStatus,
        *,
        last_error_code: Optional[str] = None,
        last_error_message: Optional[str] = None,
        finished_at_ms: Optional[int] = None,
        confirmed_at_ms: Optional[int] = None,
        resolved_by_operation_id: Optional[str] = None,
    ) -> OperationRow:
        if not isinstance(target, OperationStatus):
            raise TurnUndoError(
                UndoErrorCode.INVALID_STATE_TRANSITION,
                f"目标状态必须是 OperationStatus: {target!r}",
            )
        with self._manager._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "SELECT status FROM turn_undo_operations WHERE operation_id = ?",
                    (operation_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise TurnUndoError(
                        UndoErrorCode.CHECKPOINT_NOT_FOUND,
                        f"撤销操作不存在: {operation_id}",
                    )
                try:
                    current = OperationStatus(row[0])
                except ValueError as exc:
                    raise TurnUndoError(
                        UndoErrorCode.INVALID_STATE_TRANSITION,
                        f"未知撤销操作状态: {row[0]!r}",
                    ) from exc
                require_operation_transition(current, target)
                sets = ["status = ?"]
                params: list = [target.value]
                if last_error_code is not None:
                    sets.append("last_error_code = ?")
                    params.append(last_error_code)
                if last_error_message is not None:
                    sets.append("last_error_message = ?")
                    params.append(last_error_message)
                if finished_at_ms is not None:
                    sets.append("finished_at_ms = ?")
                    params.append(finished_at_ms)
                if confirmed_at_ms is not None:
                    sets.append("confirmed_at_ms = ?")
                    params.append(confirmed_at_ms)
                if resolved_by_operation_id is not None:
                    sets.append("resolved_by_operation_id = ?")
                    params.append(resolved_by_operation_id)
                params.append(operation_id)
                cursor.execute(
                    f"UPDATE turn_undo_operations SET {', '.join(sets)} "
                    "WHERE operation_id = ? AND status = ?",
                    (*params, current.value),
                )
                if cursor.rowcount != 1:
                    raise TurnUndoError(
                        UndoErrorCode.INVALID_STATE_TRANSITION,
                        f"撤销操作状态并发迁移失败: {operation_id}",
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return self.get_operation(operation_id)

    def resolve_interrupted_parent_locked(
        self,
        cursor,
        *,
        parent_operation_id: str,
        resolved_by_operation_id: str,
        target: OperationStatus,
    ) -> None:
        """恢复子操作提交时，同一事务内原子解决父操作（committed/compensated）。"""
        if target not in (OperationStatus.COMMITTED, OperationStatus.COMPENSATED):
            raise TurnUndoError(
                UndoErrorCode.INVALID_STATE_TRANSITION,
                f"父操作只能被解决为 committed/compensated: {target!r}",
            )
        cursor.execute(
            "UPDATE turn_undo_operations "
            "SET status = ?, resolved_by_operation_id = ?, finished_at_ms = ? "
            "WHERE operation_id = ? AND status IN (?, ?)",
            (
                target.value, resolved_by_operation_id, now_ms(),
                parent_operation_id,
                OperationStatus.INTERRUPTED.value,
                OperationStatus.RECOVERY_REQUIRED.value,
            ),
        )
        if cursor.rowcount != 1:
            raise TurnUndoError(
                UndoErrorCode.INVALID_STATE_TRANSITION,
                f"父操作不处于可解决状态: {parent_operation_id}",
            )

    def distinct_checkpoint_sessions(self) -> list[int]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT DISTINCT session_id FROM turn_undo_checkpoints"
            )
            rows = cursor.fetchall()
        return [int(r[0]) for r in rows]

    def list_checkpoints_by_status(self, statuses) -> list[CheckpointRow]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            placeholders = ",".join("?" * len(statuses))
            cursor.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM turn_undo_checkpoints "
                f"WHERE status IN ({placeholders})",
                tuple(s.value if hasattr(s, "value") else str(s) for s in statuses),
            )
            rows = cursor.fetchall()
        return [CheckpointRow(row) for row in rows]

    def list_operations_for_checkpoint(self, checkpoint_id: str) -> list[OperationRow]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT {_OPERATION_COLUMNS} FROM turn_undo_operations "
                "WHERE checkpoint_id = ? ORDER BY COALESCE(started_at_ms, 0)",
                (checkpoint_id,),
            )
            rows = cursor.fetchall()
        return [OperationRow(row) for row in rows]

    def list_nonterminal_operations(self) -> list[OperationRow]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT {_OPERATION_COLUMNS} FROM turn_undo_operations "
                "WHERE status IN (?, ?, ?, ?, ?) ORDER BY COALESCE(started_at_ms, 0)",
                (
                    OperationStatus.PREVIEWED.value,
                    OperationStatus.PREPARING.value,
                    OperationStatus.PREPARED.value,
                    OperationStatus.APPLYING.value,
                    OperationStatus.VERIFYING.value,
                ),
            )
            rows = cursor.fetchall()
        return [OperationRow(row) for row in rows]

    # -- 操作文件 --

    def insert_operation_file_locked(self, cursor, *, operation_id: str, values: dict) -> str:
        operation_file_id = new_id("opfile")
        cursor.execute(
            f"INSERT INTO turn_undo_operation_files ({_OPERATION_FILE_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_file_id, operation_id, values["file_id"],
                values["apply_ordinal"], values["expected_current_fingerprint"],
                values["rollback_kind"].value, values["rollback_blob_sha256"],
                values["rollback_mode"], values["target_kind"].value,
                values["target_blob_sha256"], values["target_mode"],
                _stage_value(values["stage_status"]),
                _stage_value(values["apply_status"]),
                _stage_value(values["verify_status"]),
                values["last_error_code"],
            ),
        )
        return operation_file_id

    def update_operation_file_stage_locked(
        self,
        cursor,
        *,
        operation_file_id: str,
        stage: str,
        status: FileStageStatus,
        last_error_code: Optional[str] = None,
    ) -> None:
        column = {"stage_status": "stage_status", "apply_status": "apply_status",
                  "verify_status": "verify_status"}.get(stage)
        if column is None:
            raise TurnUndoError(
                UndoErrorCode.INVALID_INPUT,
                f"未知操作文件阶段: {stage!r}",
            )
        cursor.execute(
            f"UPDATE turn_undo_operation_files SET {column} = ?, last_error_code = ? "
            "WHERE operation_file_id = ?",
            (_stage_value(status), last_error_code, operation_file_id),
        )
        if cursor.rowcount != 1:
            raise TurnUndoError(
                UndoErrorCode.CHECKPOINT_NOT_FOUND,
                f"操作文件记录不存在: {operation_file_id}",
            )

    def list_operation_files(self, operation_id: str) -> list[OperationFileRow]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT {_OPERATION_FILE_COLUMNS} FROM turn_undo_operation_files "
                "WHERE operation_id = ? ORDER BY apply_ordinal",
                (operation_id,),
            )
            rows = cursor.fetchall()
        return [OperationFileRow(row) for row in rows]

    # -- 确认令牌 --

    def create_confirmation(
        self,
        *,
        operation_id: str,
        checkpoint_id: str,
        record_version: int,
        workspace_identity: str,
        fingerprint_set_sha256: str,
        token: str,
        ttl_ms: int = DEFAULT_TOKEN_TTL_MS,
    ) -> str:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        confirmation_id = new_id("conf")
        with self._manager._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO turn_undo_confirmations "
                    "(confirmation_id, token_hash, boot_id, operation_id, checkpoint_id, "
                    "record_version, workspace_identity, fingerprint_set_sha256, "
                    "expires_at_ms, consumed_at_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        confirmation_id, token_hash, BOOT_ID, operation_id,
                        checkpoint_id, int(record_version), workspace_identity,
                        fingerprint_set_sha256, now_ms() + int(ttl_ms),
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return token

    def find_confirmation_by_token(self, token: str):
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT confirmation_id, token_hash, boot_id, operation_id, "
                "checkpoint_id, record_version, workspace_identity, "
                "fingerprint_set_sha256, expires_at_ms, consumed_at_ms "
                "FROM turn_undo_confirmations WHERE token_hash = ?",
                (token_hash,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        keys = (
            "confirmation_id", "token_hash", "boot_id", "operation_id",
            "checkpoint_id", "record_version", "workspace_identity",
            "fingerprint_set_sha256", "expires_at_ms", "consumed_at_ms",
        )
        return dict(zip(keys, row))

    def consume_confirmation_locked(self, cursor, *, token_hash: str) -> None:
        """原子单次消费；未生效/已过期/已消费/跨 boot 全部显式失败。"""
        cursor.execute(
            "SELECT boot_id, expires_at_ms, consumed_at_ms FROM turn_undo_confirmations "
            "WHERE token_hash = ?",
            (token_hash,),
        )
        row = cursor.fetchone()
        if row is None:
            raise TurnUndoError(
                UndoErrorCode.TOKEN_SCOPE_MISMATCH,
                "确认令牌不存在",
            )
        boot_id, expires_at_ms, consumed_at_ms = row
        if boot_id != BOOT_ID:
            raise TurnUndoError(
                UndoErrorCode.TOKEN_EXPIRED,
                "确认令牌属于过期的进程生命周期（服务已重启）",
            )
        if consumed_at_ms is not None:
            raise TurnUndoError(
                UndoErrorCode.TOKEN_ALREADY_USED,
                "确认令牌已被消费",
            )
        if int(expires_at_ms) <= now_ms():
            raise TurnUndoError(
                UndoErrorCode.TOKEN_EXPIRED,
                "确认令牌已过期，请重新预检",
            )
        cursor.execute(
            "UPDATE turn_undo_confirmations SET consumed_at_ms = ? "
            "WHERE token_hash = ? AND consumed_at_ms IS NULL",
            (now_ms(), token_hash),
        )
        if cursor.rowcount != 1:
            raise TurnUndoError(
                UndoErrorCode.TOKEN_ALREADY_USED,
                "确认令牌已被并发消费",
            )

    def delete_stale_confirmations(self) -> int:
        """启动清理：删除不属于当前 boot 的未消费令牌记录。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "DELETE FROM turn_undo_confirmations "
                    "WHERE boot_id != ? AND consumed_at_ms IS NULL",
                    (BOOT_ID,),
                )
                deleted = cursor.rowcount
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return deleted

    def has_active_confirmation(self, checkpoint_id: str) -> bool:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT 1 FROM turn_undo_confirmations "
                "WHERE checkpoint_id = ? AND consumed_at_ms IS NULL AND expires_at_ms > ? "
                "LIMIT 1",
                (checkpoint_id, now_ms()),
            )
            return cursor.fetchone() is not None

    # -- 纠正记录 --

    def insert_correction(
        self,
        *,
        workspace_root: str,
        session_id: int,
        turn_id: str,
        operation_id: str,
        files_summary: str,
        affected_count: int,
        has_uncovered_command_effects: bool,
        subsequent_completed_turns: int,
    ) -> str:
        """撤销事实纠正记录；operation_id 唯一约束保证幂等。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    "SELECT correction_id FROM workspace_corrections WHERE operation_id = ?",
                    (operation_id,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    return existing[0]
                correction_id = new_id("corr")
                cursor.execute(
                    "INSERT INTO workspace_corrections "
                    "(correction_id, workspace_root, session_id, turn_id, operation_id, "
                    "files_summary, affected_count, has_uncovered_command_effects, "
                    "subsequent_completed_turns, created_at_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        correction_id, workspace_root, session_id, turn_id,
                        operation_id, files_summary, int(affected_count),
                        int(bool(has_uncovered_command_effects)),
                        int(subsequent_completed_turns), now_ms(),
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return correction_id

    def list_corrections(self, workspace_root: str, limit: int = 20) -> list[dict]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT correction_id, workspace_root, session_id, turn_id, "
                "operation_id, files_summary, affected_count, "
                "has_uncovered_command_effects, subsequent_completed_turns, created_at_ms "
                "FROM workspace_corrections WHERE workspace_root = ? "
                "ORDER BY created_at_ms DESC LIMIT ?",
                (workspace_root, int(limit)),
            )
            rows = cursor.fetchall()
        keys = (
            "correction_id", "workspace_root", "session_id", "turn_id",
            "operation_id", "files_summary", "affected_count",
            "has_uncovered_command_effects", "subsequent_completed_turns",
            "created_at_ms",
        )
        return [dict(zip(keys, row)) for row in rows]

    def count_subsequent_ready_turns(
        self,
        session_id: int,
        workspace_root: str,
        started_at_ms: int,
        exclude_turn_id: str,
    ) -> int:
        """目标回合之后该工作区已完成的可撤销回合数（语义依赖警告用）。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM turn_undo_checkpoints "
                "WHERE workspace_root = ? AND turn_id != ? "
                "AND started_at_ms > ? AND status IN (?, ?)",
                (
                    workspace_root, exclude_turn_id, started_at_ms,
                    TurnUndoStatus.READY.value, TurnUndoStatus.REVERTED.value,
                ),
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    # -- 会话列表 / 批量状态 --

    def list_checkpoints_for_session(self, session_id: int, limit: int = 100) -> list[dict]:
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT c.checkpoint_id, c.turn_id, c.status, c.snapshot_availability, "
                "c.record_version, c.owner_kind, c.parent_checkpoint_id, c.description, "
                "c.started_at_ms, c.completed_at_ms, c.reverted_at_ms, "
                "c.has_uncovered_command_effects, c.unavailable_code, "
                "COUNT(f.file_id), COALESCE(SUM(f.net_added), 0), "
                "COALESCE(SUM(f.net_removed), 0) "
                "FROM turn_undo_checkpoints c "
                "LEFT JOIN turn_undo_files f ON f.checkpoint_id = c.checkpoint_id "
                "WHERE c.session_id = ? "
                "GROUP BY c.checkpoint_id "
                "ORDER BY c.started_at_ms DESC LIMIT ?",
                (session_id, int(limit)),
            )
            rows = cursor.fetchall()
        result = []
        keys = (
            "checkpoint_id", "turn_id", "status", "snapshot_availability",
            "record_version", "owner_kind", "parent_checkpoint_id", "description",
            "started_at_ms", "completed_at_ms", "reverted_at_ms",
            "has_uncovered_command_effects", "unavailable_code",
            "file_count", "net_added", "net_removed",
        )
        for row in rows:
            item = dict(zip(keys, row))
            item["status"] = _parse_status(item["status"]).value
            result.append(item)
        return result

    def list_workdir_ready_checkpoints(self, session_id: int, workspace_root: str) -> list[CheckpointRow]:
        """无参数 /undo 候选：本会话该工作区的 ready 检查点，最新在前。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM turn_undo_checkpoints "
                "WHERE session_id = ? AND workspace_root = ? AND status = ? "
                "AND owner_kind = ? ORDER BY started_at_ms DESC",
                (
                    session_id, workspace_root, TurnUndoStatus.READY.value,
                    OwnerKind.FOREGROUND_TURN.value,
                ),
            )
            rows = cursor.fetchall()
        return [CheckpointRow(row) for row in rows]

    # -- 保留策略与 GC --

    def expire_retained_checkpoints(
        self,
        session_id: int,
        keep: int = DEFAULT_CHECKPOINT_RETENTION,
    ) -> list[str]:
        """超限旧检查点的快照标记 expired（不覆盖生命周期事实），返回过期列表。

        不触碰 interrupted/recovering 等未终态检查点与仍被未消费令牌持有的检查点。
        """
        protected = (
            TurnUndoStatus.RECORDING,
            TurnUndoStatus.REVERTING,
            TurnUndoStatus.INTERRUPTED,
            TurnUndoStatus.RECOVERING,
        )
        with self._manager._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT checkpoint_id, status FROM turn_undo_checkpoints "
                "WHERE session_id = ? ORDER BY started_at_ms DESC",
                (session_id,),
            )
            rows = cursor.fetchall()
        expired: list[str] = []
        for offset, (checkpoint_id, status_value) in enumerate(rows):
            if offset < keep:
                continue
            try:
                status = _parse_status(status_value)
            except TurnUndoError:
                continue
            if status in protected:
                continue
            if self.has_active_confirmation(checkpoint_id):
                continue
            self.update_snapshot_availability(
                checkpoint_id,
                SnapshotAvailability.EXPIRED,
                expired_at_ms=now_ms(),
            )
            expired.append(checkpoint_id)
        return expired

    def referenced_blob_hashes(self, *, include_expired: bool = False) -> set[str]:
        """数据库仍有效引用的内容块集合（GC 依据；二次确认用）。"""
        with self._manager._lock:
            cursor = self.conn.cursor()
            if include_expired:
                cursor.execute(
                    "SELECT before_blob_sha256 FROM turn_undo_files "
                    "WHERE before_blob_sha256 IS NOT NULL "
                    "UNION SELECT after_blob_sha256 FROM turn_undo_files "
                    "WHERE after_blob_sha256 IS NOT NULL "
                    "UNION SELECT rollback_blob_sha256 FROM turn_undo_operation_files "
                    "WHERE rollback_blob_sha256 IS NOT NULL"
                )
            else:
                cursor.execute(
                    "SELECT f.before_blob_sha256 FROM turn_undo_files f "
                    "JOIN turn_undo_checkpoints c ON c.checkpoint_id = f.checkpoint_id "
                    "WHERE f.before_blob_sha256 IS NOT NULL "
                    "AND c.snapshot_availability != ? "
                    "UNION SELECT f.after_blob_sha256 FROM turn_undo_files f "
                    "JOIN turn_undo_checkpoints c ON c.checkpoint_id = f.checkpoint_id "
                    "WHERE f.after_blob_sha256 IS NOT NULL "
                    "AND c.snapshot_availability != ? "
                    "UNION SELECT o.rollback_blob_sha256 FROM turn_undo_operation_files o "
                    "WHERE o.rollback_blob_sha256 IS NOT NULL",
                    (SnapshotAvailability.EXPIRED.value, SnapshotAvailability.EXPIRED.value),
                )
            rows = cursor.fetchall()
        return {row[0] for row in rows if row[0]}

    def collect_blob_garbage(self) -> int:
        """删除磁盘上已无任何数据库有效引用的内容块；专门锁 + 二次确认引用。"""
        with _BLOB_GC_LOCK:
            referenced = self.referenced_blob_hashes(include_expired=False)
            # 二次确认：在删除前重查一次引用集（两个查询之间可能有新写入）。
            referenced |= self.referenced_blob_hashes(include_expired=False)
            removed = 0
            if not self.blob_store.root.exists():
                return 0
            for sub in self.blob_store.root.iterdir():
                if not sub.is_dir() or len(sub.name) != 2:
                    continue
                for blob_path in sub.iterdir():
                    if blob_path.name not in referenced:
                        try:
                            blob_path.unlink()
                            removed += 1
                        except OSError as exc:
                            logger.warning("turn_undo GC 删除快照失败 %s: %s", blob_path, exc)
            return removed


# ---------------------------------------------------------------------------
# message_turn_links（参与外部事务的 locked 写入）
# ---------------------------------------------------------------------------

def insert_message_turn_links_locked(cursor, links: Iterable[tuple[int, str, str]]) -> None:
    """在调用方消息写入事务内写入 message_turn_links。

    links: [(message_id, checkpoint_id, position_kind), ...]
    未知 position_kind 显式失败；同事务提交或回滚由调用方负责。
    """
    for message_id, checkpoint_id, position_kind in links:
        try:
            kind = PositionKind(position_kind)
        except ValueError as exc:
            raise TurnUndoError(
                UndoErrorCode.INVALID_INPUT,
                f"未知 message_turn_links 位置种类: {position_kind!r}",
            ) from exc
        cursor.execute(
            "SELECT 1 FROM turn_undo_checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        )
        if cursor.fetchone() is None:
            raise TurnUndoError(
                UndoErrorCode.CHECKPOINT_NOT_FOUND,
                f"消息关联的检查点不存在: {checkpoint_id}",
            )
        cursor.execute(
            "INSERT INTO message_turn_links (message_id, checkpoint_id, position_kind) "
            "VALUES (?, ?, ?)",
            (int(message_id), checkpoint_id, kind.value),
        )


def update_message_link_position_locked(
    cursor,
    *,
    message_id: int,
    position_kind: str,
) -> None:
    """回合封闭时把最后一条 assistant 关联更新为 assistant_final。

    这是显式按 message_id 的更新，不属于按时间范围回填。
    """
    try:
        kind = PositionKind(position_kind)
    except ValueError as exc:
        raise TurnUndoError(
            UndoErrorCode.INVALID_INPUT,
            f"未知 message_turn_links 位置种类: {position_kind!r}",
        ) from exc
    cursor.execute(
        "UPDATE message_turn_links SET position_kind = ? WHERE message_id = ?",
        (kind.value, int(message_id)),
    )
