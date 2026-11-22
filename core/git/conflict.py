from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from core.git.models import GitError, GitErrorCode, RepositoryIdentity
from core.git.runner import ExecScope, GitRunner, with_paths


@dataclass(frozen=True)
class ConflictContent:
    path: str
    base: Optional[str]
    ours: Optional[str]
    theirs: Optional[str]
    worktree: Optional[str]


def load_conflict(
    runner: GitRunner, identity: RepositoryIdentity, path: str
) -> ConflictContent:
    def _show(stage: int) -> Optional[str]:
        result = runner.run(
            ["show", f":{stage}:{path}"],
            cwd=identity.worktree_root,
            scope=ExecScope.READ_ONLY,
            timeout=30.0,
        )
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", "replace")

    try:
        wt = open(
            os.path.join(identity.worktree_root, path), "r", encoding="utf-8", errors="replace"
        ).read()
    except OSError:
        wt = None
    content = ConflictContent(
        path=path, base=_show(1), ours=_show(2), theirs=_show(3), worktree=wt
    )
    if content.ours is None and content.theirs is None and content.base is None:
        raise GitError(
            GitErrorCode.INVALID_STATE,
            f"路径不在冲突状态: {path}",
        )
    return content


def resolve_conflict(
    runner: GitRunner,
    identity: RepositoryIdentity,
    path: str,
    result_text: str,
) -> None:
    target = os.path.join(identity.worktree_root, path)
    os.makedirs(os.path.dirname(target) or identity.worktree_root, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(result_text)
    runner.run_checked(
        with_paths(["add"], [path]),
        cwd=identity.worktree_root,
        scope=ExecScope.INDEX,
        timeout=30.0,
    )


def accept_side(
    runner: GitRunner,
    identity: RepositoryIdentity,
    path: str,
    side: str,
) -> None:
    """接受一侧（current/incoming），或对"删除/保留"冲突直接选边并标记解决。"""
    side = side.lower()
    if side not in ("current", "ours", "incoming", "theirs"):
        raise GitError(GitErrorCode.INVALID_STATE, f"非法冲突选边: {side}")
    content = load_conflict(runner, identity, path)
    chosen = content.ours if side in ("current", "ours") else content.theirs
    if chosen is None:
        target = os.path.join(identity.worktree_root, path)
        if os.path.exists(target):
            os.remove(target)
        runner.run_checked(
            with_paths(["add"], [path]),
            cwd=identity.worktree_root,
            scope=ExecScope.INDEX,
            timeout=30.0,
        )
        return
    resolve_conflict(runner, identity, path, chosen)
