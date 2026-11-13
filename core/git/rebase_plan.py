"""Interactive Rebase 计划读取与改写（规格 §5.10/§7 阶段任务）。

实现方式：不解析/生成面向终端的 ASCII 文本，直接操作 Git 的 todo 文件——
以临时 `GIT_SEQUENCE_EDITOR`（指向自身写入器）方式参与 `git rebase -i`
流程；读取时解析 todo 文件为结构化行。崩溃/中断后不自动继续（§14）。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import List, Tuple

from core.git.models import GitError, GitErrorCode, RepositoryIdentity
from core.git.runner import GitRunner

_VALID_ACTIONS = {"pick", "reword", "edit", "squash", "fixup", "drop", "exec", "break", "label", "reset", "merge"}


@dataclass(frozen=True)
class RebasePlanLine:
    action: str
    oid: str
    summary: str


def read_rebase_plan(runner: GitRunner, identity: RepositoryIdentity) -> List[RebasePlanLine]:
    """读取当前 rebase-merge todo（仅 rebase 停在编辑/冲突时存在）。"""
    todo_path = os.path.join(identity.git_dir, "rebase-merge", "git-rebase-todo")
    if not os.path.exists(todo_path):
        raise GitError(
            GitErrorCode.INVALID_STATE,
            "当前没有可编辑的 Rebase 计划（rebase-merge/git-rebase-todo 不存在）",
        )
    with open(todo_path, "r", encoding="utf-8", errors="replace") as handle:
        return parse_plan_text(handle.read())


def parse_plan_text(text: str) -> List[RebasePlanLine]:
    """解析 todo 文本为结构化行；注释行忽略（纯函数，表驱动测试）。"""
    lines: List[RebasePlanLine] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        action = parts[0].lower()
        if action not in _VALID_ACTIONS:
            continue
        if action in ("exec", "break", "label", "reset", "merge"):
            lines.append(RebasePlanLine(action=action, oid="", summary=line))
        else:
            oid = parts[1] if len(parts) > 1 else ""
            summary = parts[2] if len(parts) > 2 else ""
            lines.append(RebasePlanLine(action=action, oid=oid, summary=summary))
    return lines


def serialize_plan(lines: List[RebasePlanLine]) -> str:
    out: List[str] = []
    for line in lines:
        if line.action in ("exec", "break", "label", "reset", "merge"):
            out.append(line.summary)
        else:
            out.append(f"{line.action} {line.oid} {line.summary}".rstrip())
    return "\n".join(out) + "\n"


def start_interactive_rebase_with_plan(
    runner: GitRunner,
    identity: RepositoryIdentity,
    onto: str,
    plan_lines: List[RebasePlanLine],
) -> None:
    """以给定计划启动 interactive rebase：GIT_SEQUENCE_EDITOR 指向自身写入器。

    plan_lines 必须是完整计划（含每行的 pick/drop/squash 等动作）。
    """
    plan_text = serialize_plan(plan_lines)
    if not plan_text.strip():
        raise GitError(GitErrorCode.INVALID_STATE, "Rebase 计划为空")
    writer_script = _make_plan_writer(plan_text)
    env_patch = {"GIT_SEQUENCE_EDITOR": writer_script}
    result = runner.run(
        ["rebase", "-i", onto],
        cwd=identity.worktree_root,
        timeout=300.0,
        env_extra=env_patch,
    )
    if result.returncode != 0:
        raise GitError(
            GitErrorCode.GIT_COMMAND_FAILED,
            result.stderr_text.strip()[:400] or "interactive rebase 失败",
            exit_code=result.returncode,
            command_summary="git rebase -i <onto>",
        )


def write_rebase_plan(runner: GitRunner, identity: RepositoryIdentity, lines: List[RebasePlanLine]) -> None:
    """改写**正在进行中**的 rebase 计划：直接替换 todo 文件（rebase 停在
    edit/conflict 时安全；Git 文档保证该状态下 todo 可编辑）。"""
    todo_path = os.path.join(identity.git_dir, "rebase-merge", "git-rebase-todo")
    if not os.path.exists(todo_path):
        raise GitError(
            GitErrorCode.INVALID_STATE,
            "当前没有可编辑的 Rebase 计划（rebase-merge/git-rebase-todo 不存在）",
        )
    _write_todo_atomic(todo_path, serialize_plan(lines))


def _make_plan_writer(plan_text: str) -> str:
    """生成临时脚本：git 以 todo 路径为 $1 调用它，脚本把计划内容覆盖上去。"""
    if os.name == "nt":
        fd, script_path = tempfile.mkstemp(suffix=".cmd")
        plan_path = script_path + ".plan"
        with open(plan_path, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write(plan_text)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write(f'@echo off\r\ncopy /Y "{plan_path}" "%1" > NUL\r\n')
        return script_path
    fd, script_path = tempfile.mkstemp(suffix=".sh")
    plan_path = script_path + ".plan"
    with open(plan_path, "w", encoding="utf-8") as handle:
        handle.write(plan_text)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f'#!/bin/sh\ncp "{plan_path}" "$1"\n')
    os.chmod(script_path, 0o755)
    return script_path


def _write_todo_atomic(path: str, text: str) -> None:
    tmp = path + ".ycode-tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def rebase_plan_todos(runner: GitRunner, identity: RepositoryIdentity) -> Tuple[Tuple[str, str, str], ...]:
    """GUI/CLI 友好格式：(action, short_oid, summary) 列表。"""
    return tuple((l.action, l.oid[:8], l.summary) for l in read_rebase_plan(runner, identity))
