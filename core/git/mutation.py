"""白名单写操作层（规格 §5.4–§5.14）。

本层只负责"单个白名单 git 操作"的参数构造、作用域与执行；
锁、快照校验、风险确认、信任检查由 GitApplicationService 统一编排（§9.3）。
所有操作显式失败：非零退出 → GitError，绝不自动重试或补救。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from core.git.models import (
    GitError,
    GitErrorCode,
    RepositoryIdentity,
)
from core.git.query import StatusV2
from core.git.runner import ExecScope, GitRunner, with_paths

# merge/rebase/cherry-pick/revert 的 Continue/Skip/Abort 合法动作表（§14）
_OPERATION_ACTIONS = {
    "merge": {"continue", "abort"},
    "rebase": {"continue", "skip", "abort"},
    "cherry_pick": {"continue", "abort"},
    "revert": {"continue", "abort"},
}


def stage(runner: GitRunner, identity: RepositoryIdentity, paths: Sequence[str]) -> None:
    if not paths:
        raise GitError(GitErrorCode.INVALID_STATE, "stage 需要至少一个路径")
    runner.run_checked(
        with_paths(["add"], paths),
        cwd=identity.worktree_root,
        scope=ExecScope.INDEX,
        timeout=60.0,
    )


def stage_all(runner: GitRunner, identity: RepositoryIdentity) -> None:
    runner.run_checked(
        ["add", "-A"], cwd=identity.worktree_root, scope=ExecScope.INDEX, timeout=120.0
    )


def unstage(runner: GitRunner, identity: RepositoryIdentity, paths: Sequence[str]) -> None:
    if not paths:
        raise GitError(GitErrorCode.INVALID_STATE, "unstage 需要至少一个路径")
    runner.run_checked(
        with_paths(["restore", "--staged"], paths),
        cwd=identity.worktree_root,
        scope=ExecScope.INDEX,
        timeout=60.0,
    )


def discard_worktree_changes(
    runner: GitRunner, identity: RepositoryIdentity, paths: Sequence[str]
) -> None:
    """Restore 未提交内容（§13 高风险：令牌由服务层校验）。"""
    if not paths:
        raise GitError(GitErrorCode.INVALID_STATE, "discard 需要至少一个路径")
    runner.run_checked(
        with_paths(["restore"], paths),
        cwd=identity.worktree_root,
        scope=ExecScope.WRITE,
        timeout=60.0,
    )


def commit(
    runner: GitRunner,
    identity: RepositoryIdentity,
    title: str,
    body: str = "",
    amend: bool = False,
) -> str:
    """提交已暂存内容（禁止暗中加入未暂存内容：git commit 天然只含 Index）。"""
    if not title.strip():
        raise GitError(GitErrorCode.INVALID_STATE, "提交信息标题不能为空")
    args = ["commit"]
    if amend:
        args.append("--amend")
    args += ["-m", title]
    if body.strip():
        args += ["-m", body]
    result = runner.run_checked(
        args, cwd=identity.worktree_root, scope=ExecScope.WRITE, timeout=120.0
    )
    return _parse_committed_oid(runner, identity)


def _parse_committed_oid(runner: GitRunner, identity: RepositoryIdentity) -> str:
    result = runner.run_checked(
        ["rev-parse", "HEAD"], cwd=identity.worktree_root, scope=ExecScope.READ_ONLY, timeout=15.0
    )
    return result.stdout_text.strip()


def undo_last_commit(
    runner: GitRunner, identity: RepositoryIdentity, keep_staged: bool
) -> None:
    """Undo 最近一次本地提交：soft 保留暂存 / mixed 保留为未暂存（§5.4）。"""
    mode = "soft" if keep_staged else "mixed"
    runner.run_checked(
        ["reset", f"--{mode}", "HEAD~1"],
        cwd=identity.worktree_root,
        scope=ExecScope.WRITE,
        timeout=60.0,
    )


# ---------------------------------------------------------------- 分支（§5.5/§5.6）

def create_branch(
    runner: GitRunner,
    identity: RepositoryIdentity,
    name: str,
    start_point: Optional[str] = None,
    switch_after: bool = False,
) -> None:
    runner.check_branch_name(name, cwd=identity.worktree_root)
    if switch_after:
        args = ["switch", "-c", name]
        if start_point:
            args.append(start_point)
        runner.run_checked(args, cwd=identity.worktree_root, scope=ExecScope.WRITE, timeout=60.0)
    else:
        args = ["branch", name]
        if start_point:
            args.append(start_point)
        runner.run_checked(args, cwd=identity.worktree_root, scope=ExecScope.REF, timeout=30.0)


def switch_branch_checked(
    runner: GitRunner,
    identity: RepositoryIdentity,
    target: str,
    status: StatusV2,
    occupied: Sequence[str],
) -> None:
    """§5.6 分支切换硬规则：存在任何未提交内容/冲突/进行中操作/占用即拒绝。

    界面可提示先提交或显式 Stash，但绝不自动处理。
    """
    blockers: List[str] = []
    if status.staged:
        blockers.append(f"已暂存修改 {len(status.staged)} 个文件")
    if status.unstaged:
        blockers.append(f"未暂存修改 {len(status.unstaged)} 个文件")
    if status.untracked:
        blockers.append(f"未跟踪文件 {len(status.untracked)} 个")
    if status.conflicts:
        blockers.append(f"未解决冲突 {len(status.conflicts)} 个")
    if status.branch_name == target and not occupied:
        raise GitError(GitErrorCode.INVALID_REF, f"当前已处于分支 {target}")
    if occupied:
        blockers.append(f"目标分支被其他 Worktree 占用: {', '.join(occupied)}")
    if blockers:
        raise GitError(
            GitErrorCode.DIRTY_WORKTREE,
            "切换分支被拒绝（工作区不清洁）：\n- " + "\n- ".join(blockers)
            + "\n请先提交，或显式执行 Stash。",
        )
    runner.run_checked(
        ["switch", target], cwd=identity.worktree_root, scope=ExecScope.WRITE, timeout=60.0
    )


def rename_branch(runner: GitRunner, identity: RepositoryIdentity, name: str, new_name: str) -> None:
    runner.check_branch_name(new_name, cwd=identity.worktree_root)
    runner.run_checked(
        ["branch", "-m", name, new_name],
        cwd=identity.worktree_root,
        scope=ExecScope.REF,
        timeout=30.0,
    )


def set_upstream(
    runner: GitRunner, identity: RepositoryIdentity, branch: str, upstream: Optional[str]
) -> None:
    if upstream:
        runner.run_checked(
            ["branch", "-u", upstream, branch],
            cwd=identity.worktree_root,
            scope=ExecScope.REF,
            timeout=30.0,
        )
    else:
        runner.run_checked(
            ["branch", "--unset-upstream", branch],
            cwd=identity.worktree_root,
            scope=ExecScope.REF,
            timeout=30.0,
        )


def delete_local_branch(
    runner: GitRunner, identity: RepositoryIdentity, name: str, force: bool
) -> None:
    runner.run_checked(
        ["branch", "-D" if force else "-d", name],
        cwd=identity.worktree_root,
        scope=ExecScope.REF,
        timeout=30.0,
    )


# ---------------------------------------------------------------- Remote 与同步（§5.7）

def remote_add(runner: GitRunner, identity: RepositoryIdentity, name: str, url: str) -> None:
    runner.run_checked(
        ["remote", "add", name, url], cwd=identity.worktree_root,
        scope=ExecScope.WRITE, timeout=30.0,
    )


def remote_set_url(runner: GitRunner, identity: RepositoryIdentity, name: str, url: str) -> None:
    runner.run_checked(
        ["remote", "set-url", name, url], cwd=identity.worktree_root,
        scope=ExecScope.WRITE, timeout=30.0,
    )


def remote_remove(runner: GitRunner, identity: RepositoryIdentity, name: str) -> None:
    runner.run_checked(
        ["remote", "remove", name], cwd=identity.worktree_root,
        scope=ExecScope.WRITE, timeout=30.0,
    )


def fetch(runner: GitRunner, identity: RepositoryIdentity, remote: Optional[str], prune: bool) -> None:
    args = ["fetch", "--progress"]
    if prune:
        args.append("--prune")
    if remote:
        args.append(remote)
    runner.run_checked(args, cwd=identity.worktree_root, scope=ExecScope.NETWORK, timeout=600.0)


def pull_ff_only(runner: GitRunner, identity: RepositoryIdentity) -> None:
    """Pull 默认只允许 Fast-forward；无法 ff 时显式失败（§5.7）。"""
    runner.run_checked(
        ["pull", "--ff-only"], cwd=identity.worktree_root, scope=ExecScope.NETWORK, timeout=600.0
    )


def push(
    runner: GitRunner,
    identity: RepositoryIdentity,
    remote: str,
    refspec: str,
    set_upstream: bool = False,
    force_with_lease: bool = False,
) -> None:
    """推送。只允许 --force-with-lease（需服务层高风险确认），绝不构造 --force。"""
    args = ["push", "--progress"]
    if set_upstream:
        args.append("-u")
    if force_with_lease:
        args.append("--force-with-lease")
    args += [remote, refspec]
    runner.run_checked(args, cwd=identity.worktree_root, scope=ExecScope.NETWORK, timeout=600.0)


# ---------------------------------------------------------------- Stash（§5.8）

def stash_create(
    runner: GitRunner, identity: RepositoryIdentity, message: str, include_untracked: bool
) -> None:
    args = ["stash", "push", "-m", message or "stash"]
    if include_untracked:
        args.append("-u")
    runner.run_checked(args, cwd=identity.worktree_root, scope=ExecScope.WRITE, timeout=120.0)


def stash_apply(runner: GitRunner, identity: RepositoryIdentity, index: int, pop: bool) -> None:
    """应用指定 stash（stash@{index}）。"""
    verb = "pop" if pop else "apply"
    result = runner.run(
        ["stash", verb, f"stash@{{{index}}}"],
        cwd=identity.worktree_root,
        scope=ExecScope.WRITE,
        timeout=120.0,
    )
    if result.returncode != 0:
        raise GitError(
            GitErrorCode.CONFLICTS_PRESENT if _has_conflict_markers(result.stderr_text)
            else GitErrorCode.GIT_COMMAND_FAILED,
            result.stderr_text.strip()[:400] or f"stash {verb} 失败",
            exit_code=result.returncode,
            command_summary=f"git stash {verb} stash@{{{index}}}",
        )


def stash_pop(runner: GitRunner, identity: RepositoryIdentity, index: int) -> None:
    stash_apply(runner, identity, index, pop=True)


def stash_drop(runner: GitRunner, identity: RepositoryIdentity, index: int) -> None:
    runner.run_checked(
        ["stash", "drop", f"stash@{{{index}}}"],
        cwd=identity.worktree_root,
        scope=ExecScope.WRITE,
        timeout=30.0,
    )


def stash_branch(
    runner: GitRunner, identity: RepositoryIdentity, index: int, name: str
) -> None:
    runner.check_branch_name(name, cwd=identity.worktree_root)
    runner.run_checked(
        ["stash", "branch", name, f"stash@{{{index}}}"],
        cwd=identity.worktree_root,
        scope=ExecScope.WRITE,
        timeout=120.0,
    )


def _has_conflict_markers(stderr: str) -> bool:
    lowered = stderr.lower()
    return "conflict" in lowered


# ---------------------------------------------------------------- Tag（§5.12）

def tag_create(
    runner: GitRunner,
    identity: RepositoryIdentity,
    name: str,
    target: Optional[str] = None,
    annotated: bool = False,
    message: str = "",
) -> None:
    args = ["tag"]
    if annotated:
        args += ["-a", name, "-m", message or name]
    else:
        args.append(name)
    if target:
        args.append(target)
    runner.run_checked(args, cwd=identity.worktree_root, scope=ExecScope.REF, timeout=30.0)


def tag_push(runner: GitRunner, identity: RepositoryIdentity, remote: str, name: str) -> None:
    runner.run_checked(
        ["push", remote, f"refs/tags/{name}"],
        cwd=identity.worktree_root,
        scope=ExecScope.NETWORK,
        timeout=600.0,
    )


def tag_delete_local(runner: GitRunner, identity: RepositoryIdentity, name: str) -> None:
    runner.run_checked(
        ["tag", "-d", name], cwd=identity.worktree_root, scope=ExecScope.REF, timeout=30.0
    )


def tag_delete_remote(runner: GitRunner, identity: RepositoryIdentity, remote: str, name: str) -> None:
    runner.run_checked(
        ["push", remote, "--delete", f"refs/tags/{name}"],
        cwd=identity.worktree_root,
        scope=ExecScope.NETWORK,
        timeout=600.0,
    )


# ---------------------------------------------------------------- Reset / Merge / Rebase / Cherry-pick / Revert（§5.10/§5.11）

def reset(runner: GitRunner, identity: RepositoryIdentity, mode: str, target: str) -> None:
    if mode not in ("soft", "mixed", "hard"):
        raise GitError(GitErrorCode.INVALID_STATE, f"非法 reset 模式: {mode}")
    runner.run_checked(
        ["reset", f"--{mode}", target], cwd=identity.worktree_root, scope=ExecScope.WRITE, timeout=120.0
    )


def clean_untracked(runner: GitRunner, identity: RepositoryIdentity, dry_run: bool = False) -> str:
    """Clean 未跟踪文件：服务层必须先 dry-run 展示清单再执行（§5.3）。"""
    args = ["clean", "-d"]
    args.append("-n" if dry_run else "-f")
    result = runner.run_checked(
        args, cwd=identity.worktree_root, scope=ExecScope.WRITE, timeout=120.0
    )
    return result.stdout_text


def merge(runner: GitRunner, identity: RepositoryIdentity, ref: str, ff_mode: str) -> None:
    if ff_mode not in ("ff", "no_ff"):
        raise GitError(GitErrorCode.INVALID_STATE, f"非法 merge 模式: {ff_mode}")
    args = ["merge"]
    args.append("--ff-only" if ff_mode == "ff" else "--no-ff")
    args.append(ref)
    runner.run_checked(args, cwd=identity.worktree_root, scope=ExecScope.WRITE, timeout=300.0)


def rebase(runner: GitRunner, identity: RepositoryIdentity, onto: str) -> None:
    runner.run_checked(
        ["rebase", onto], cwd=identity.worktree_root, scope=ExecScope.WRITE, timeout=300.0
    )


def cherry_pick(runner: GitRunner, identity: RepositoryIdentity, oids: Sequence[str]) -> None:
    runner.run_checked(
        ["cherry-pick", *oids], cwd=identity.worktree_root, scope=ExecScope.WRITE, timeout=300.0
    )


def revert(runner: GitRunner, identity: RepositoryIdentity, oids: Sequence[str]) -> None:
    runner.run_checked(
        ["revert", "--no-edit", *oids],
        cwd=identity.worktree_root,
        scope=ExecScope.WRITE,
        timeout=300.0,
    )


def operation_continue(runner: GitRunner, identity: RepositoryIdentity, kind: str) -> None:
    _ensure_action_allowed(kind, "continue")
    runner.run_checked(
        [kind.replace("_", "-"), "--continue"],
        cwd=identity.worktree_root,
        scope=ExecScope.WRITE,
        timeout=300.0,
    )


def operation_skip(runner: GitRunner, identity: RepositoryIdentity, kind: str) -> None:
    _ensure_action_allowed(kind, "skip")
    runner.run_checked(
        [kind.replace("_", "-"), "--skip"],
        cwd=identity.worktree_root,
        scope=ExecScope.WRITE,
        timeout=300.0,
    )


def operation_abort(runner: GitRunner, identity: RepositoryIdentity, kind: str) -> None:
    _ensure_action_allowed(kind, "abort")
    runner.run_checked(
        [kind.replace("_", "-"), "--abort"],
        cwd=identity.worktree_root,
        scope=ExecScope.WRITE,
        timeout=300.0,
    )


def _ensure_action_allowed(kind: str, action: str) -> None:
    allowed = _OPERATION_ACTIONS.get(kind)
    if allowed is None:
        raise GitError(
            GitErrorCode.INVALID_STATE,
            f"非法操作类型（状态机无此 kind）: {kind}",
        )
    if action not in allowed:
        raise GitError(
            GitErrorCode.INVALID_STATE,
            f"{kind} 状态下不合法的动作: {action}（合法: {sorted(allowed)}）",
        )


# ---------------------------------------------------------------- Worktree（§5.14，路径校验在服务层）

def worktree_add(
    runner: GitRunner, identity: RepositoryIdentity, path: str, branch: str, new_branch: bool = False
) -> None:
    args = ["worktree", "add"]
    if new_branch:
        runner.check_branch_name(branch, cwd=identity.worktree_root)
        args += ["-b", branch]
    args += [path]
    if not new_branch:
        args.append(branch)
    runner.run_checked(args, cwd=identity.worktree_root, scope=ExecScope.WRITE, timeout=120.0)


def worktree_remove(runner: GitRunner, identity: RepositoryIdentity, path: str, force: bool) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(path)
    runner.run_checked(args, cwd=identity.worktree_root, scope=ExecScope.WRITE, timeout=120.0)


def worktree_prune(runner: GitRunner, identity: RepositoryIdentity) -> None:
    runner.run_checked(
        ["worktree", "prune"], cwd=identity.worktree_root, scope=ExecScope.WRITE, timeout=60.0
    )
