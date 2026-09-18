"""Hunk/行级 Patch 的生成、预检与应用（规格 §5.3/§5.13）。

铁律：Patch 必须从当前快照生成精确 diff，先 `git apply --check` 预检再应用；
快照过期或上下文不匹配时直接拒绝（patch_check_failed），绝不模糊应用或三方补救。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from core.git.models import GitError, GitErrorCode, RepositoryIdentity
from core.git.query_refs import DiffDocument, diff_document
from core.git.runner import ExecScope, GitRunner, with_paths


def generate_hunk_patch(
    runner: GitRunner,
    identity: RepositoryIdentity,
    path: str,
    *,
    staged: bool = False,
) -> str:
    """生成单个文件的工作区/暂存区 diff 文本（用于 Hunk 选择与反向应用）。"""
    doc: DiffDocument = diff_document(
        runner, identity, path=path, staged=staged
    )
    if not doc.raw_text.strip():
        return ""
    return doc.raw_text


def apply_patch_text(
    runner: GitRunner,
    identity: RepositoryIdentity,
    patch_text: str,
    *,
    staged: bool = False,
    reverse: bool = False,
) -> None:
    """把 patch 文本应用到工作区（或 Index）。先 --check 预检，失败显式拒绝。"""
    if not patch_text.strip():
        raise GitError(GitErrorCode.INVALID_STATE, "patch 内容为空")
    args: List[str] = ["apply", "--whitespace=nowarn"]
    if staged:
        args.append("--cached")
    if reverse:
        args.append("--reverse")
    result = _run_apply_with_stdin(runner, identity, args, patch_text)
    if result.returncode != 0:
        stderr = result.stderr_text.strip()
        raise GitError(
            GitErrorCode.PATCH_CHECK_FAILED,
            f"Patch 应用失败: {stderr[:300]}",
            exit_code=result.returncode,
            command_summary="git apply -",
        )


def _run_apply_with_stdin(
    runner: GitRunner,
    identity: RepositoryIdentity,
    args: List[str],
    patch_text: str,
):
    """--check 预检通过后才真正应用（两次进程调用，均经 stdin 传 patch）。"""
    check_args = [*args, "--check", "-"]
    check = runner.run(
        check_args,
        cwd=identity.worktree_root,
        scope=ExecScope.INDEX,
        timeout=60.0,
        stdin=True,
        stdin_data=patch_text.encode("utf-8"),
    )
    if check.returncode != 0:
        stderr = check.stderr_text.strip()
        raise GitError(
            GitErrorCode.PATCH_CHECK_FAILED,
            f"Patch 预检失败（上下文不匹配或快照已过期）: {stderr[:300]}",
            exit_code=check.returncode,
            command_summary="git apply --check -",
        )
    return runner.run(
        [*args, "-"],
        cwd=identity.worktree_root,
        scope=ExecScope.INDEX if args and "--cached" in args else ExecScope.WRITE,
        timeout=60.0,
        stdin=True,
        stdin_data=patch_text.encode("utf-8"),
    )


def build_stage_hunk_patch(
    base_patch: str,
    selected_hunk_indexes: Sequence[int],
    hunks,
) -> str:
    """从完整 diff 文本中抽取选中的 Hunk 构成可暂存 patch（纯函数）。

    hunks 为 query_refs.parse_unified_diff 的产物；保留 diff 头部（diff --git/
    index/---/+++ 行）以生成合法 patch。
    """
    if not selected_hunk_indexes:
        raise GitError(GitErrorCode.INVALID_STATE, "未选择任何 Hunk")
    header_lines: List[str] = []
    for line in base_patch.splitlines():
        if line.startswith("@@"):
            break
        header_lines.append(line)
    header = "\n".join(header_lines)
    parts: List[str] = []
    for index in selected_hunk_indexes:
        try:
            hunk = hunks[index]
        except IndexError:
            raise GitError(GitErrorCode.INVALID_STATE, f"Hunk 序号越界: {index}")
        body = "\n".join([f"@@ -{hunk.old_start},{hunk.old_lines} "
                          f"+{hunk.new_start},{hunk.new_lines} @@ {hunk.header}".rstrip(),
                          *hunk.lines])
        parts.append(body)
    return header + "\n" + "\n".join(parts) + "\n"


def check_patch_applies(
    runner: GitRunner,
    identity: RepositoryIdentity,
    patch_text: str,
    *,
    staged: bool = False,
) -> bool:
    """dry-run：patch 是否能干净应用（不落盘）。"""
    try:
        apply_patch_text(runner, identity, patch_text, staged=staged)
        return True
    except GitError as error:
        if error.code is GitErrorCode.PATCH_CHECK_FAILED:
            return False
        raise


def export_patch(
    runner: GitRunner,
    identity: RepositoryIdentity,
    *,
    left: Optional[str] = None,
    right: Optional[str] = None,
    staged: bool = False,
) -> str:
    """把工作区/暂存区/两引用差异导出为 patch 文本。"""
    doc = diff_document(runner, identity, left=left, right=right, staged=staged)
    return doc.raw_text
