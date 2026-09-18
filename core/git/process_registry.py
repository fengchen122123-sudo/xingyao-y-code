"""Workspace Process Registry（规格 §9.2）。

登记 execute_command / GUI 内嵌终端 / 后台任务的活动状态，供 Git 操作兼容矩阵
判断"同 Worktree 存在哪些活动命令"。登记失败绝不影响命令执行本身（调用方
以 try/except 包裹）。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from core.git.repository import normalize_repo_path

# risk_class 语义：
#   git       —— 可识别的直接 git 命令（可接入 GitOperationCoordinator）
#   readonly  —— 可识别的只读查询命令
#   service   —— 可识别的长期监听服务
#   mutation  —— 可识别的仓库/文件变更命令
#   unknown   —— 无法可靠分类（按"未知前台变更命令"从保守处理）
_RISK_READONLY_COMMANDS = frozenset(
    {"ls", "dir", "cat", "type", "pwd", "whoami", "echo", "find", "grep", "rg", "head", "tail", "wc"}
)


@dataclass(frozen=True)
class ProcessRecord:
    handle: str
    workspace: str
    kind: str
    foreground: bool
    long_running: bool
    risk_class: str


_LOCK = threading.RLock()
_RECORDS: Dict[str, ProcessRecord] = {}


def register_process(
    workspace: str,
    handle: Optional[str] = None,
    *,
    kind: str = "execute_command",
    foreground: bool = True,
    long_running: bool = False,
    risk_class: str = "unknown",
) -> ProcessRecord:
    record = ProcessRecord(
        handle=handle or uuid.uuid4().hex,
        workspace=normalize_repo_path(workspace),
        kind=kind,
        foreground=foreground,
        long_running=long_running,
        risk_class=risk_class,
    )
    with _LOCK:
        _RECORDS[record.handle] = record
    return record


def unregister_process(handle: str) -> bool:
    with _LOCK:
        return _RECORDS.pop(handle, None) is not None


def active_processes(workspace: str) -> Tuple[ProcessRecord, ...]:
    with _LOCK:
        key = normalize_repo_path(workspace)
        return tuple(record for record in _RECORDS.values() if record.workspace == key)


def clear_registry() -> None:
    with _LOCK:
        _RECORDS.clear()


def _first_token(command: str) -> str:
    text = (command or "").strip()
    if text.startswith('"'):
        token = text.split('"', 2)[1]
    else:
        token = text.split(None, 1)[0] if text else ""
    return (
        token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower().removesuffix(".exe")
    )


def risk_class_for_command(command: str) -> str:
    """轻量命令风险分类：识别 git / 只读查询，其余一律 unknown（从保守）。"""
    token = _first_token(command)
    if token == "git":
        return "git"
    if token in _RISK_READONLY_COMMANDS:
        return "readonly"
    return "unknown"
