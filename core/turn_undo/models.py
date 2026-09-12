"""星瑶按回合撤销：严格枚举、值对象与结构化错误。

本包是"按回合撤销文件修改"功能的唯一语义层：
- models.py     枚举、状态迁移表、值对象、DTO、错误（本模块）
- storage.py    SQLite 元数据 DAO、内容寻址快照仓库与 GC
- recorder.py   受控文件修改前后的记录与验证
- coordinator.py 规范化工作区互斥锁与活跃回合判定
- transaction.py 多文件恢复事务
- recovery.py   崩溃中断检测、路径冻结与显式恢复
- prompt_context.py 模型动态纠正上下文
- service.py    GUI、CLI、模型工具共用的编排入口

铁律：未知枚举、非法状态迁移必须显式失败；快照失败时文件修改必须失败。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# 严格枚举
# ---------------------------------------------------------------------------

class TurnUndoStatus(str, enum.Enum):
    """回合撤销生命周期状态（严格枚举，非法迁移显式失败）。"""

    RECORDING = "recording"
    READY = "ready"
    NO_CHANGES = "no_changes"
    UNAVAILABLE = "unavailable"
    CONFLICTED = "conflicted"
    REVERTING = "reverting"
    REVERTED = "reverted"
    INTERRUPTED = "interrupted"
    RECOVERING = "recovering"
    FAILED = "failed"


class SnapshotAvailability(str, enum.Enum):
    """快照保留状态；与撤销生命周期正交，过期不得覆盖 reverted 审计事实。"""

    AVAILABLE = "available"
    EXPIRED = "expired"
    MISSING = "missing"
    CORRUPT = "corrupt"
    NOT_REQUIRED = "not_required"


class NodeKind(str, enum.Enum):
    """受控路径的结点种类。严格可撤销节点只有 missing 与 regular_file。"""

    MISSING = "missing"
    REGULAR_FILE = "regular_file"
    SYMLINK = "symlink"
    DIRECTORY = "directory"
    SPECIAL = "special"


class OwnerKind(str, enum.Enum):
    """检查点归属：前台回合 / 独立后台任务。"""

    FOREGROUND_TURN = "foreground_turn"
    BACKGROUND_TASK = "background_task"


class SourceKind(str, enum.Enum):
    """文件修改来源。"""

    MAIN_AGENT = "main_agent"
    SUBAGENT = "subagent"


class OperationKind(str, enum.Enum):
    """撤销操作种类。continue/cancel 恢复必须引用未解决的父操作。"""

    REVERT = "revert"
    CONTINUE_REVERT = "continue_revert"
    CANCEL_REVERT = "cancel_revert"


class OperationStatus(str, enum.Enum):
    """撤销操作状态（与回合生命周期分离）。"""

    PREVIEWED = "previewed"
    PREPARING = "preparing"
    PREPARED = "prepared"
    APPLYING = "applying"
    VERIFYING = "verifying"
    COMMITTED = "committed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    COMPENSATED = "compensated"
    RECOVERY_REQUIRED = "recovery_required"


class FileStageStatus(str, enum.Enum):
    """操作文件三段（stage/apply/verify）各自独立持久化的进度。"""

    PENDING = "pending"
    STAGED = "staged"
    APPLIED = "applied"
    VERIFIED = "verified"
    FAILED = "failed"


class PositionKind(str, enum.Enum):
    """message_turn_links 的消息位置种类；未知角色必须显式扩展而非塞默认值。"""

    USER = "user"
    ASSISTANT_INTERMEDIATE = "assistant_intermediate"
    ASSISTANT_FINAL = "assistant_final"
    TOOL = "tool"
    NOTICE = "notice"
    ERROR = "error"


class RecoveryAction(str, enum.Enum):
    """中断事务的两种显式恢复方向。"""

    CONTINUE_REVERT = "continue_revert"
    CANCEL_REVERT = "cancel_revert"


class RollbackKind(str, enum.Enum):
    """单文件恢复动作。"""

    RESTORE_CONTENT = "restore_content"      # 用 before blob 恢复普通文件内容
    DELETE_CREATED = "delete_created"        # 本轮新建文件 → 移入事务隔离区
    RESTORE_DELETED = "restore_deleted"      # 本轮删除文件 → 从快照原子就位
    RESTORE_PATH_STATE = "restore_path_state"  # 重命名等路径状态恢复


# 状态机迁移表（规格 5.5）：未列出的迁移必须显式失败。
CHECKPOINT_TRANSITIONS: dict[TurnUndoStatus, frozenset[TurnUndoStatus]] = {
    TurnUndoStatus.RECORDING: frozenset({
        TurnUndoStatus.READY,
        TurnUndoStatus.NO_CHANGES,
        TurnUndoStatus.UNAVAILABLE,
    }),
    TurnUndoStatus.READY: frozenset({
        TurnUndoStatus.CONFLICTED,
        TurnUndoStatus.REVERTING,
        TurnUndoStatus.UNAVAILABLE,
    }),
    TurnUndoStatus.CONFLICTED: frozenset({
        TurnUndoStatus.READY,
        TurnUndoStatus.UNAVAILABLE,
    }),
    TurnUndoStatus.FAILED: frozenset({
        TurnUndoStatus.READY,
        TurnUndoStatus.CONFLICTED,
        TurnUndoStatus.UNAVAILABLE,
    }),
    TurnUndoStatus.REVERTING: frozenset({
        TurnUndoStatus.REVERTED,
        TurnUndoStatus.INTERRUPTED,
        TurnUndoStatus.FAILED,
    }),
    TurnUndoStatus.INTERRUPTED: frozenset({
        TurnUndoStatus.RECOVERING,
        TurnUndoStatus.UNAVAILABLE,
    }),
    TurnUndoStatus.RECOVERING: frozenset({
        TurnUndoStatus.REVERTED,
        TurnUndoStatus.READY,
        TurnUndoStatus.INTERRUPTED,
        TurnUndoStatus.UNAVAILABLE,
    }),
}

# 生命周期终态：只允许 SnapshotAvailability 更新，不允许状态迁移。
CHECKPOINT_TERMINAL_STATUSES: frozenset[TurnUndoStatus] = frozenset({
    TurnUndoStatus.NO_CHANGES,
    TurnUndoStatus.UNAVAILABLE,
    TurnUndoStatus.REVERTED,
})

OPERATION_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.PREVIEWED: frozenset({
        OperationStatus.PREPARING,
        OperationStatus.CANCELLED,
        OperationStatus.FAILED,
    }),
    OperationStatus.PREPARING: frozenset({
        OperationStatus.PREPARED,
        OperationStatus.FAILED,
    }),
    OperationStatus.PREPARED: frozenset({
        OperationStatus.APPLYING,
        OperationStatus.FAILED,
    }),
    OperationStatus.APPLYING: frozenset({
        OperationStatus.VERIFYING,
        OperationStatus.INTERRUPTED,
    }),
    OperationStatus.VERIFYING: frozenset({
        OperationStatus.COMMITTED,
        OperationStatus.INTERRUPTED,
    }),
    OperationStatus.INTERRUPTED: frozenset({
        OperationStatus.RECOVERY_REQUIRED,
        OperationStatus.COMMITTED,
        OperationStatus.COMPENSATED,
    }),
    OperationStatus.RECOVERY_REQUIRED: frozenset({
        OperationStatus.COMMITTED,
        OperationStatus.COMPENSATED,
    }),
}


def require_checkpoint_transition(current: TurnUndoStatus, target: TurnUndoStatus) -> None:
    """校验检查点状态迁移；同状态幂等读取放行，非法迁移显式失败。"""
    if current is target:
        return
    allowed = CHECKPOINT_TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise TurnUndoError(
            "invalid_state_transition",
            f"非法检查点状态迁移: {current.value} -> {target.value}",
        )


def require_operation_transition(current: OperationStatus, target: OperationStatus) -> None:
    if current is target:
        return
    allowed = OPERATION_TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise TurnUndoError(
            "invalid_state_transition",
            f"非法撤销操作状态迁移: {current.value} -> {target.value}",
        )


# ---------------------------------------------------------------------------
# 结构化错误
# ---------------------------------------------------------------------------

class TurnUndoError(Exception):
    """携带稳定错误码的显式失败；GUI/CLI 不得从人类文本解析业务状态。"""

    def __init__(self, code: str, message: str, *, detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class UndoErrorCode:
    """稳定错误码常量（规格 19.2）。"""

    CHECKPOINT_NOT_FOUND = "checkpoint_not_found"
    CHECKPOINT_UNAVAILABLE = "checkpoint_unavailable"
    CHECKPOINT_EXPIRED = "checkpoint_expired"
    FILE_CONFLICT = "file_conflict"
    WORKSPACE_BUSY = "workspace_busy"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_ALREADY_USED = "token_already_used"
    TOKEN_SCOPE_MISMATCH = "token_scope_mismatch"
    OPERATION_SCOPE_MISMATCH = "operation_scope_mismatch"
    SNAPSHOT_MISSING = "snapshot_missing"
    SNAPSHOT_CORRUPT = "snapshot_corrupt"
    UNSUPPORTED_NODE_KIND = "unsupported_node_kind"
    REVERT_INTERRUPTED = "revert_interrupted"
    RECOVERY_REQUIRED = "recovery_required"
    RECOVERY_OPERATION_CYCLE = "recovery_operation_cycle"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    # 补充码：仅用于内部判别，语义同样稳定。
    WORKSPACE_IDENTITY_MISMATCH = "workspace_identity_mismatch"
    IO_ERROR = "io_error"
    INVALID_INPUT = "invalid_input"
    SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
    CHECKPOINT_CLOSED = "checkpoint_closed"
    # 线程未绑定任何检查点上下文（回合外调用）：与"上下文存在但检查点已封闭"
    # 严格区分——前者允许工具降级为不可撤销写入，后者必须在触盘前拒绝（规格 9.2）
    NO_ACTIVE_CHECKPOINT = "no_active_checkpoint"


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeState:
    """一次受控修改前/后的精确结点状态。

    identity 为可靠获取时的卷/设备号与 File ID（Windows 用 st_dev+st_ino，
    POSIX 用 st_dev+st_ino）；link_count 为硬链接计数；symlink_target 仅用于
    冲突识别，不跟随链接读取内容。
    """

    kind: NodeKind
    exists: bool
    size: Optional[int] = None
    sha256: Optional[str] = None
    mode: Optional[int] = None
    identity: Optional[str] = None
    link_count: Optional[int] = None
    symlink_target: Optional[str] = None
    blob_sha256: Optional[str] = None

    def fingerprint_fields(self) -> tuple:
        return (
            self.kind.value,
            self.exists,
            self.size,
            self.sha256,
            self.mode,
            self.identity,
            self.link_count,
            self.symlink_target,
        )


@dataclass(frozen=True)
class PlannedPathMutation:
    """受控工具在真正写文件前声明的完整计划修改。

    after_content 是计划写入的完整字节（删除时为 None 且 after.kind=missing）。
    before 由记录器自行从磁盘捕获，不信任工具转述。
    """

    canonical_path: str
    display_path: str
    after: NodeState
    after_content: Optional[bytes]
    source_kind: SourceKind
    source_id: str
    tool_name: str
    tool_call_id: str

    def __post_init__(self):
        if self.after.kind is NodeKind.MISSING:
            if self.after_content is not None:
                raise TurnUndoError(
                    "invalid_input",
                    "计划 after 为 missing 时不得携带内容字节",
                )
        else:
            if self.after_content is None:
                raise TurnUndoError(
                    "invalid_input",
                    "计划 after 非缺失时必须携带完整内容字节",
                )


@dataclass(frozen=True)
class MutationHandle:
    """写入句柄：before/after blob 与数据库记录全部持久化成功后签发。

    不可伪造（由记录器内部签发）；封闭检查点不再签发新句柄，迟到写入在
    触盘前失败。previous_after 保存被本次计划替换前的已验证 after 状态，
    供写入失败时回滚恢复记录完整性。
    """

    handle_id: str
    checkpoint_id: str
    file_id: str
    canonical_path: str
    planned_after: NodeState
    is_first_mutation: bool
    previous_after: Optional[NodeState] = None


@dataclass(frozen=True)
class FileConflict:
    """预检发现的单文件结构化冲突。"""

    file_id: str
    canonical_path: str
    display_path: str
    expected: NodeState
    actual: NodeState
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileChangeSummary:
    """回合内单文件的净变更摘要（来自最初 before -> 最终 after）。"""

    file_id: str
    display_path: str
    before_kind: NodeKind
    after_kind: Optional[NodeKind]
    net_added: int
    net_removed: int
    diff_kind: str


@dataclass
class TurnUndoSummary:
    """会话内一个回合检查点的摘要（列表/批量状态查询用）。"""

    checkpoint_id: str
    session_id: int
    turn_id: str
    workspace_root: str
    status: TurnUndoStatus
    snapshot_availability: SnapshotAvailability
    record_version: int
    owner_kind: OwnerKind
    parent_checkpoint_id: Optional[str]
    description: str
    started_at_ms: int
    completed_at_ms: Optional[int]
    reverted_at_ms: Optional[int]
    has_uncovered_command_effects: bool
    file_count: int
    net_added: int
    net_removed: int
    unavailable_code: Optional[str] = None


@dataclass
class TurnUndoPreview:
    """预检结果：无冲突时携带影响摘要与确认所需元数据。"""

    checkpoint_id: str
    turn_id: str
    status: TurnUndoStatus
    snapshot_availability: SnapshotAvailability
    record_version: int
    file_count: int
    restore_content_count: int
    delete_created_count: int
    restore_deleted_count: int
    restore_path_state_count: int
    created_files: tuple[str, ...]
    conflicts: tuple[FileConflict, ...] = ()
    has_uncovered_command_effects: bool = False
    subsequent_completed_turns: int = 0
    workspace_root: str = ""


@dataclass
class TurnUndoResult:
    """撤销执行结果（结构化摘要，不携带令牌或文件内容）。"""

    checkpoint_id: str
    turn_id: str
    status: TurnUndoStatus
    operation_id: str
    reverted_at_ms: int
    restored_files: tuple[str, ...] = ()
    deleted_created_files: tuple[str, ...] = ()
    restored_deleted_files: tuple[str, ...] = ()
    idempotent: bool = False
    original_reverted_at_ms: Optional[int] = None


@dataclass
class TurnFileDiff:
    """按 file_id 惰性生成的展示用 diff（有上限）。"""

    file_id: str
    display_path: str
    diff_kind: str
    diff_text: Optional[str]
    net_added: int
    net_removed: int
    before_size: Optional[int]
    after_size: Optional[int]
    truncated: bool = False


@dataclass
class RecoveryPreview:
    """中断恢复预览：已处理、剩余与未知文件清单。"""

    operation_id: str
    checkpoint_id: str
    action: RecoveryAction
    processed_files: tuple[str, ...]
    remaining_files: tuple[str, ...]
    unknown_files: tuple[str, ...]
    recovery_required: bool = False
    child_operation_id: Optional[str] = None


@dataclass
class RecoveryResult:
    """恢复执行结果。"""

    operation_id: str
    parent_operation_id: Optional[str]
    action: RecoveryAction
    status: OperationStatus
    checkpoint_status: TurnUndoStatus
    affected_files: tuple[str, ...] = ()
