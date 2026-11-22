from __future__ import annotations

import os
from typing import Tuple

from core.git.models import GitError, GitErrorCode, HeadState, RepositoryIdentity
from core.git.runner import MIN_GIT_VERSION, GitRunner

_BASELINE_CAPABILITIES = frozenset(
    {
        "status_v2",
        "worktree",
        "rebase_edit",      # interactive rebase 计划编辑（自管 todo 文件）
        "absolute_git_dir",
    }
)


def normalize_repo_path(path: str) -> str:
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
    ws = str(workspace_path)

    detected = runner.check_available()
    runner.ensure_version(MIN_GIT_VERSION, detected)

    probe = runner.run(
        [
            "rev-parse",
            "--is-bare-repository",
            "--show-toplevel",
            "--absolute-git-dir",
            "--git-common-dir",
        ],
        cwd=ws,
        timeout=15.0,
    )
    lines = [ln.strip() for ln in probe.stdout_text.splitlines() if ln.strip()]
    is_bare_mark = lines[0] if lines else ""
    if probe.returncode != 0 and "not a git repository" in probe.stderr_text.lower():
        raise GitError(
            GitErrorCode.NOT_A_REPOSITORY,
            f"目录不是 Git 仓库（工作区与父级均未找到 .git）: {redact_ws(ws)}",
            exit_code=probe.returncode,
            command_summary="git rev-parse --is-bare-repository",
        )
    if is_bare_mark == "true":
        raise GitError(
            GitErrorCode.BARE_REPOSITORY_UNSUPPORTED,
            f"Git 工作台不支持管理裸仓库: {redact_ws(ws)}",
            command_summary="git rev-parse --is-bare-repository",
        )
    if probe.returncode != 0 or len(lines) < 4:
        from core.git.models import redact_text

        raise GitError(
            GitErrorCode.GIT_COMMAND_FAILED,
            redact_text(probe.stderr_text.strip()[:200]) or "git rev-parse 复合查询失败",
            exit_code=probe.returncode,
            command_summary="git rev-parse [--is-bare-repository --show-toplevel --absolute-git-dir --git-common-dir]",
        )

    root = _strip_output(lines[1])
    git_dir = normalize_repo_path(_strip_output(lines[2]))
    common_raw = _strip_output(lines[3])
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
    env = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    result = runner.run(
        ["config", "--get-regexp", r"^user\.(name|email)$"],
        cwd=ws,
        timeout=10.0,
        env_extra=env,
    )
    seen = set()
    for line in result.stdout_text.splitlines():
        key, _, value = line.partition(" ")
        if key in ("user.name", "user.email") and value.strip():
            seen.add(key)
    if seen == {"user.name", "user.email"}:
        return "ok"
    return "identity_missing"


def redact_ws(ws: str) -> str:
    from core.git.models import redact_text

    return redact_text(ws)
