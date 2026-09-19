"""只读查询层（规格 §5.2/§5.9）：porcelain v2 解析、操作状态检测与各只读查询。

状态解析只吃 `git status --porcelain=v2 -z --branch --show-stash` 的原始字节，
绝不解析本地化的人类可读输出（规格 §5.2/§8）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from core.git.models import (
    ConflictEntry,
    FileChange,
    GitError,
    GitErrorCode,
    HeadState,
    OperationKind,
    OperationPhase,
    RepositoryIdentity,
)
from core.git.runner import ExecScope, GitRunner

_UNBORN_MARK = "(initial)"
_DETACHED_MARK = "(detached)"

# 未合并 XY → 冲突类型（git status v2 unmerged entries）
_CONFLICT_KINDS = {
    "DD": "both_deleted",
    "AU": "added_by_us",
    "UD": "deleted_by_them",
    "UA": "added_by_them",
    "DU": "deleted_by_us",
    "AA": "both_added",
    "UU": "both_modified",
}


@dataclass(frozen=True)
class StatusV2:
    """porcelain v2 解析产物（原始条目，不做分组）。"""

    head_oid: Optional[str]
    branch_name: Optional[str]
    upstream: Optional[str]
    ahead: int
    behind: int
    stash_count: int
    entries: Tuple[FileChange, ...]
    untracked: Tuple[str, ...]
    conflicts: Tuple[ConflictEntry, ...]

    # porcelain v2 的"未修改"标记是 "."（区别于 v1 的空格）
    @property
    def staged(self) -> Tuple[FileChange, ...]:
        return tuple(e for e in self.entries if e.index_status not in (".",))

    @property
    def unstaged(self) -> Tuple[FileChange, ...]:
        return tuple(e for e in self.entries if e.worktree_status not in (".",))

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def parse_status_v2(raw: bytes) -> StatusV2:
    """解析 `git status --porcelain=v2 -z --branch --show-stash` 输出。

    -z 格式：所有记录 NUL 结尾；`2` 重命名条目的 <path> 与 <origPath> 各自
    NUL 结尾（已用真实仓库字节实验核对）；路径不做任何引号/转义猜测。
    """
    head_oid: Optional[str] = None
    branch_name: Optional[str] = None
    upstream: Optional[str] = None
    ahead = 0
    behind = 0
    stash_count = 0
    entries: List[FileChange] = []
    untracked: List[str] = []
    conflicts: List[ConflictEntry] = []

    segments = raw.split(b"\x00")
    index = 0
    while index < len(segments):
        segment = segments[index]
        index += 1
        if not segment:
            continue
        line = _decode(segment)
        if line.startswith("# "):
            tokens = line[2:].split(" ")
            key = tokens[0]
            if key == "branch.oid":
                value = tokens[1] if len(tokens) > 1 else ""
                head_oid = None if value == _UNBORN_MARK else value
            elif key == "branch.head":
                value = tokens[1] if len(tokens) > 1 else ""
                branch_name = None if value == _DETACHED_MARK else value
            elif key == "branch.upstream" and len(tokens) > 1:
                upstream = tokens[1]
            elif key == "branch.ab" and len(tokens) > 2:
                ahead = _safe_int(tokens[1].lstrip("+"))
                behind = _safe_int(tokens[2].lstrip("-"))
            elif key == "stash" and len(tokens) > 1:
                stash_count = _safe_int(tokens[1])
        elif line.startswith("1 "):
            # 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
            parts = line.split(" ", 8)
            if len(parts) == 9:
                entries.append(
                    FileChange(
                        path=parts[8],
                        index_status=parts[1][0],
                        worktree_status=parts[1][1],
                        orig_path=None,
                    )
                )
        elif line.startswith("2 "):
            # 2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <score> <path>\0<origPath>
            parts = line.split(" ", 9)
            orig_path: Optional[str] = None
            if index < len(segments):
                orig_path = _decode(segments[index]) or None
                index += 1
            if len(parts) == 10:
                entries.append(
                    FileChange(
                        path=parts[9],
                        index_status=parts[1][0],
                        worktree_status=parts[1][1],
                        orig_path=orig_path,
                    )
                )
        elif line.startswith("u "):
            # u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>
            parts = line.split(" ", 10)
            if len(parts) == 11:
                xy = parts[1]
                conflicts.append(
                    ConflictEntry(
                        path=parts[10],
                        kind=_CONFLICT_KINDS.get(xy, "unknown"),
                    )
                )
        elif line.startswith("? "):
            untracked.append(line[2:])
        # "! "（ignored）在 --ignored 下才出现；忽略条目只计数量时另行查询
    return StatusV2(
        head_oid=head_oid,
        branch_name=branch_name,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        stash_count=stash_count,
        entries=tuple(entries),
        untracked=tuple(untracked),
        conflicts=tuple(conflicts),
    )


def _safe_int(text: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def count_ignored(runner: GitRunner, worktree_root: str) -> int:
    """忽略文件数量（--porcelain=v2 -z --ignored=matching 的 `!` 条目数）。"""
    result = runner.run(
        ["status", "--porcelain=v2", "-z", "--ignored=matching"],
        cwd=worktree_root,
        scope=ExecScope.READ_ONLY,
        timeout=60.0,
    )
    if result.returncode != 0:
        return 0
    return sum(
        1
        for segment in result.stdout.split(b"\x00")
        if segment.startswith(b"! ")
    )


def detect_operation_state(
    runner: GitRunner,
    identity: RepositoryIdentity,
    has_conflicts: bool,
) -> Tuple[OperationKind, OperationPhase]:
    """读取 Git 目录状态文件识别进行中操作（规格 §14 状态机）。

    外部 Bisect/Am/未知 Sequencer 一律识别为 unsupported_external，不得误判 idle。
    """
    git_dir = identity.git_dir
    common_dir = identity.common_git_dir

    def exists(path: str) -> bool:
        return os.path.exists(os.path.join(path))

    if exists(os.path.join(git_dir, "BISECT_LOG")) or exists(os.path.join(common_dir, "BISECT_LOG")):
        return OperationKind.UNSUPPORTED_EXTERNAL, OperationPhase.RUNNING
    if exists(os.path.join(git_dir, "rebase-apply", "applying")):
        # git am：工作台不管理，显式标记
        return OperationKind.UNSUPPORTED_EXTERNAL, OperationPhase.RUNNING
    if exists(os.path.join(git_dir, "rebase-merge")) or exists(os.path.join(git_dir, "rebase-apply")):
        kind = OperationKind.REBASE
        phase = OperationPhase.PAUSED_FOR_EDIT
        if has_conflicts:
            phase = OperationPhase.PAUSED_FOR_CONFLICT
        return kind, phase
    if exists(os.path.join(git_dir, "MERGE_HEAD")):
        return OperationKind.MERGE, (
            OperationPhase.PAUSED_FOR_CONFLICT if has_conflicts else OperationPhase.RUNNING
        )
    if exists(os.path.join(git_dir, "CHERRY_PICK_HEAD")):
        return OperationKind.CHERRY_PICK, (
            OperationPhase.PAUSED_FOR_CONFLICT if has_conflicts else OperationPhase.RUNNING
        )
    if exists(os.path.join(git_dir, "REVERT_HEAD")):
        return OperationKind.REVERT, (
            OperationPhase.PAUSED_FOR_CONFLICT if has_conflicts else OperationPhase.RUNNING
        )
    return OperationKind.IDLE, OperationPhase.NONE


def fetch_status(
    runner: GitRunner,
    identity: RepositoryIdentity,
) -> StatusV2:
    """获取真实状态（权威读取入口）。失败显式抛出。"""
    result = runner.run_checked(
        ["status", "--porcelain=v2", "-z", "--branch", "--show-stash"],
        cwd=identity.worktree_root,
        scope=ExecScope.READ_ONLY,
        timeout=60.0,
    )
    return parse_status_v2(result.stdout)


def head_state_from(status: StatusV2) -> HeadState:
    if status.branch_name is None:
        return HeadState.DETACHED
    if status.head_oid is None:
        return HeadState.UNBORN
    return HeadState.NORMAL


def not_a_repository_guard(runner: GitRunner, workspace_path: str) -> None:
    """供服务层在解析失败时给出准确错误码的辅助。"""
    probe = runner.run(["rev-parse", "--is-inside-work-tree"], cwd=workspace_path, timeout=15.0)
    if probe.returncode != 0:
        raise GitError(
            GitErrorCode.NOT_A_REPOSITORY,
            f"目录不是 Git 仓库: {workspace_path}",
            exit_code=probe.returncode,
        )
