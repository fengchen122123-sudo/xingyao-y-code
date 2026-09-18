"""三方冲突读取与解决（规格 §5.10 冲突界面后端）。

- Base/Ours/Theirs 分别来自 index 的 stage 1/2/3（`git show :1:path` 等）；
- Result 由用户决定：接受 Current / Incoming / Both / 手工编辑；
- AI 只能给出建议，结果一律由用户确认后经正常编辑流程落盘（§5.10）；
- 解决 = 写入 Result + `git add` 标记已解决；全部解决后由服务层
  operation_continue 推进（显式，不自动）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from core.git.models import GitError, GitErrorCode, RepositoryIdentity
from core.git.runner import ExecScope, GitRunner, with_paths


@dataclass(frozen=True)
class ConflictContent:
    path: str
    base: Optional[str]      # stage 1（both_added 等场景可能没有）
    ours: Optional[str]      # stage 2（deleted_by_them 等场景可能没有）
    theirs: Optional[str]    # stage 3（deleted_by_us 等场景可能没有）
    worktree: Optional[str]  # 工作区当前内容（含冲突标记）


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
    """把用户确认的 Result 写入工作区并 `git add` 标记解决。

    result_text 为 None（二进制选边等）时用 accept_side。
    """
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
        # 一侧是删除：结果为删除该文件
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
