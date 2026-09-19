"""Git 工作台不可变值对象、错误码与操作状态机。

风格照 core/harness/models.py：str 枚举 + frozen dataclass + 显式校验函数。
状态机三维正交（规格 §14）：operation_kind × operation_phase × conflicts，
唯一耦合规则 kind==IDLE ⇔ phase==NONE；冲突维度独立可非空。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class GitErrorCode(str, Enum):
    """规格 §7.4 错误码（全表 + 追加 invalid_state）。"""

    GIT_NOT_FOUND = "git_not_found"
    GIT_VERSION_UNSUPPORTED = "git_version_unsupported"
    NOT_A_REPOSITORY = "not_a_repository"
    BARE_REPOSITORY_UNSUPPORTED = "bare_repository_unsupported"
    REPOSITORY_UNTRUSTED = "repository_untrusted"
    UNSAFE_GIT_EXTENSION = "unsafe_git_extension"
    REPOSITORY_BUSY = "repository_busy"
    WORKSPACE_PROCESS_ACTIVE = "workspace_process_active"
    STALE_SNAPSHOT = "stale_snapshot"
    DIRTY_WORKTREE = "dirty_worktree"
    BRANCH_IN_USE = "branch_in_use"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    CONFLICTS_PRESENT = "conflicts_present"
    INVALID_REF = "invalid_ref"
    IDENTITY_MISSING = "identity_missing"
    AUTHENTICATION_REQUIRED = "authentication_required"
    PERMISSION_DENIED = "permission_denied"
    NETWORK_FAILED = "network_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PATCH_CHECK_FAILED = "patch_check_failed"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_INVALID = "confirmation_invalid"
    UNSUPPORTED_FILE_KIND = "unsupported_file_kind"
    GIT_COMMAND_FAILED = "git_command_failed"
    INVALID_STATE = "invalid_state"


class OperationKind(str, Enum):
    IDLE = "idle"
    MERGE = "merge"
    REBASE = "rebase"
    CHERRY_PICK = "cherry_pick"
    REVERT = "revert"
    UNSUPPORTED_EXTERNAL = "unsupported_external"


class OperationPhase(str, Enum):
    NONE = "none"
    RUNNING = "running"
    PAUSED_FOR_CONFLICT = "paused_for_conflict"
    PAUSED_FOR_EDIT = "paused_for_edit"
    INTERRUPTED = "interrupted"


class HeadState(str, Enum):
    NORMAL = "normal"
    DETACHED = "detached"
    UNBORN = "unborn"


_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*)://[^/@\s]+@")
_CRED_QUERY_RE = re.compile(r"(?i)(token|password|secret|access_token|private_key)=([^&\s]+)")


def redact_text(text: str) -> str:
    """移除 URL userinfo 与凭据形态的 Query 参数（规格 §16：凭据不进入日志/错误）。"""
    if not text:
        return text
    text = _URL_USERINFO_RE.sub(r"\1://***:***@", text)
    text = _CRED_QUERY_RE.sub(r"\1=***", text)
    return text


@dataclass
class GitError(Exception):
    """显式失败错误对象（规格 §7.4）：安全人类说明 + 退出码 + 脱敏命令摘要 + 可否重试。

    注意：不能 frozen——Python 异常机制需要写 __traceback__，frozen 会让 raise 本身崩溃。
    """

    code: GitErrorCode
    message: str
    exit_code: Optional[int] = None
    command_summary: str = ""
    retryable: bool = False

    def __str__(self) -> str:
        parts = [f"[{self.code.value}] {redact_text(self.message)}"]
        if self.command_summary:
            parts.append(f"command: {self.command_summary}")
        if self.exit_code is not None:
            parts.append(f"exit={self.exit_code}")
        return " | ".join(parts)


def validate_operation_state(
    kind: OperationKind,
    phase: OperationPhase,
    conflicts: Tuple[ConflictEntry, ...] = (),
) -> None:
    """校验操作状态机组合合法性：kind==IDLE ⇔ phase==NONE；冲突维度独立。"""
    if not isinstance(kind, OperationKind) or not isinstance(phase, OperationPhase):
        raise GitError(
            GitErrorCode.INVALID_STATE,
            f"非法操作状态类型: kind={kind!r} phase={phase!r}",
        )
    idle = kind is OperationKind.IDLE
    none = phase is OperationPhase.NONE
    if idle != none:
        raise GitError(
            GitErrorCode.INVALID_STATE,
            f"非法操作状态组合: kind={kind.value} 与 phase={phase.value} 必须同时为 idle/none 或同时非空",
        )
    for entry in conflicts:
        if not isinstance(entry, ConflictEntry):
            raise GitError(
                GitErrorCode.INVALID_STATE,
                f"冲突条目类型非法: {entry!r}",
            )


@dataclass(frozen=True)
class RepositoryIdentity:
    """仓库身份（规格 §7.1 + §5.1 仓库形态识别）。路径均为规范化绝对路径。"""

    workspace_path: str
    worktree_root: str
    git_dir: str
    common_git_dir: str
    is_bare: bool
    is_linked_worktree: bool
    git_version: str
    capabilities: frozenset
    head_state: HeadState = HeadState.NORMAL
    branch_name: Optional[str] = None


@dataclass(frozen=True)
class FileChange:
    """单文件变更（porcelain v2 解析产物）。"""

    path: str
    index_status: str
    worktree_status: str
    orig_path: Optional[str] = None
    is_binary: bool = False


@dataclass(frozen=True)
class ConflictEntry:
    """未解决冲突条目。kind: both_modified/both_added/deleted_by_them/..."""

    path: str
    kind: str


@dataclass(frozen=True)
class SnapshotVersions:
    """乐观并发版本（规格 §9.3）：写请求必须携带，服务端锁内重算比较。"""

    worktree_version: Optional[str] = None
    refs_version: Optional[str] = None


@dataclass(frozen=True)
class GitSnapshot:
    """状态总览快照（规格 §7.2）。版本由服务端在锁内计算，前端不得自行生成。"""

    repository: RepositoryIdentity
    head_oid: str
    branch_name: Optional[str]
    head_state: HeadState
    upstream: Optional[str]
    ahead: int
    behind: int
    staged: Tuple[FileChange, ...]
    unstaged: Tuple[FileChange, ...]
    untracked: Tuple[str, ...]
    ignored_count: int
    conflicts: Tuple[ConflictEntry, ...]
    stash_count: int
    operation_kind: OperationKind
    operation_phase: OperationPhase
    identity_state: str
    worktree_version: str
    refs_version: str
