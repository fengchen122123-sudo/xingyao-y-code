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
    """解析工作区对应的真实仓库身份。非仓库/Bare/低版本显式失败。

    is-bare / show-toplevel / absolute-git-dir / git-common-dir 四个查询合并为
    一次 rev-parse：输出按旗标顺序逐行返回（普通仓库四行全有；裸仓库 rc=128
    但 stdout 首行仍是 "true"，实测核对）。快照/preview/写操作每次都经过这里，
    慢环境下省下的每个进程都是秒级开销。
    """
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
        # 非裸仓库却没拿全四行（异常 git 形态）：保留原始 stderr 显式失败
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
    """提交身份可用性探测（规格 §5.2"Git 身份是否可用于提交"）：屏蔽全局/系统配置，
    只看仓库本地配置是否齐备；返回 "ok" | "identity_missing"。

    user.name / user.email 两键合并为一次 config --get-regexp（输出行格式
    `<key> <value>`，值含空格时 partition 取其余整段）。
    """
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
