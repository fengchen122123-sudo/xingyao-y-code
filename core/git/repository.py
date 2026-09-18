"""仓库发现与能力检测（规格 §5.1/§7.1）。

从工作区（可为子目录）解析真实仓库根、Git Dir、Common Git Dir，
识别普通仓库 / 链接 Worktree / Bare / Detached HEAD / Unborn Branch，
并维护基线之上的逐操作能力表。
"""

from __future__ import annotations

import os
from typing import Tuple

from core.git.models import GitError, GitErrorCode, HeadState, RepositoryIdentity
from core.git.runner import MIN_GIT_VERSION, GitRunner

# 基线（Git 2.36.0）保证可用的能力；能力表随逐操作实现扩充
_BASELINE_CAPABILITIES = frozenset(
    {
        "status_v2",        # git status --porcelain=v2 -z --branch --show-stash
        "worktree",         # git worktree list --porcelain
        "rebase_edit",      # interactive rebase 计划编辑（自管 todo 文件）
        "absolute_git_dir", # rev-parse --absolute-git-dir
    }
)


def normalize_repo_path(path: str) -> str:
    """仓库路径规范化：绝对化 + normpath + Windows 大小写归一（§4.5 规范化标识的基础）。"""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def parse_git_version(version_output: str) -> Tuple[int, int, int]:
    from core.git.runner import parse_git_version as _parse

    return _parse(version_output)


def load_capabilities(version: Tuple[int, int, int]) -> frozenset:
    """版本 ≥ 基线时返回基线能力集；否则视为调用方未先做版本门禁（防御）。"""
    if version < MIN_GIT_VERSION:
        raise GitError(
            GitErrorCode.GIT_VERSION_UNSUPPORTED,
            f"Git 版本 {version} 低于基线 {MIN_GIT_VERSION}",
        )
    return _BASELINE_CAPABILITIES


def _strip_output(text: str) -> str:
    return text.strip()


def resolve_repository(workspace_path: str, runner: GitRunner) -> RepositoryIdentity:
    """解析工作区对应的真实仓库身份。非仓库/Bare/低版本显式失败。"""
    ws = str(workspace_path)

    detected = runner.check_available()
    runner.ensure_version(MIN_GIT_VERSION, detected)

    # 仓库判定：bare 优先（bare 内 --show-toplevel 会失败）
    bare_check = runner.run(["rev-parse", "--is-bare-repository"], cwd=ws, timeout=15.0)
    if bare_check.returncode != 0:
        raise GitError(
            GitErrorCode.NOT_A_REPOSITORY,
            f"目录不是 Git 仓库（工作区与父级均未找到 .git）: {redact_ws(ws)}",
            exit_code=bare_check.returncode,
            command_summary="git rev-parse --is-bare-repository",
        )
    if _strip_output(bare_check.stdout_text) == "true":
        raise GitError(
            GitErrorCode.BARE_REPOSITORY_UNSUPPORTED,
            f"Git 工作台不支持管理裸仓库: {redact_ws(ws)}",
            command_summary="git rev-parse --is-bare-repository",
        )

    root = _strip_output(
        runner.run_checked(["rev-parse", "--show-toplevel"], cwd=ws, timeout=15.0).stdout_text
    )

    git_dir = normalize_repo_path(
        _strip_output(
            runner.run_checked(["rev-parse", "--absolute-git-dir"], cwd=ws, timeout=15.0).stdout_text
        )
    )
    common_raw = _strip_output(
        runner.run_checked(["rev-parse", "--git-common-dir"], cwd=ws, timeout=15.0).stdout_text
    )
    if not os.path.isabs(common_raw):
        common_raw = os.path.join(root, common_raw)
    common_dir = normalize_repo_path(common_raw)

    is_linked = git_dir != common_dir

    branch_name = None
    head_state = HeadState.DETACHED
    sym = runner.run(["symbolic-ref", "-q", "--short", "HEAD"], cwd=ws, timeout=15.0)
    if sym.returncode == 0:
        branch_name = _strip_output(sym.stdout_text) or None
        head_state = HeadState.DETACHED
        verify = runner.run(["rev-parse", "-q", "--verify", "HEAD"], cwd=ws, timeout=15.0)
        head_state = HeadState.NORMAL if verify.returncode == 0 else HeadState.UNBORN

    return RepositoryIdentity(
        workspace_path=ws,
        worktree_root=root,
        git_dir=git_dir,
        common_git_dir=common_dir,
        is_bare=False,
        is_linked_worktree=is_linked,
        git_version=f"{detected[0]}.{detected[1]}.{detected[2]}",
        capabilities=load_capabilities(detected),
        head_state=head_state,
        branch_name=branch_name,
    )


def probe_identity_state(runner: GitRunner, ws: str) -> str:
    """提交身份可用性探测（规格 §5.2"Git 身份是否可用于提交"）：屏蔽全局/系统配置，
    只看仓库本地配置是否齐备；返回 "ok" | "identity_missing"。"""
    env = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    email = runner.run(["config", "user.email"], cwd=ws, timeout=10.0, env_extra=env)
    name = runner.run(["config", "user.name"], cwd=ws, timeout=10.0, env_extra=env)
    if email.returncode == 0 and name.returncode == 0 and email.stdout_text.strip() and name.stdout_text.strip():
        return "ok"
    return "identity_missing"


def redact_ws(ws: str) -> str:
    from core.git.models import redact_text

    return redact_text(ws)
