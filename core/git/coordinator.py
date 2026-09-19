"""双级锁、快照版本与操作兼容矩阵（规格 §9）。

- Worktree 锁保护该 Worktree 的工作区/Index；Common Repository 锁保护共享引用
  与配置；同时需要两把锁时固定先 Common 后 Worktree，避免死锁（§9.1）。
- 快照版本按 §7.2 材料清单 SHA-256，不读工作区文件正文；比较用恒定时间。
- 兼容矩阵按 §9.2 表逐行实现；无法分类的命令按"未知前台变更命令"保守处理。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from contextlib import contextmanager
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple

from core.git.models import RepositoryIdentity
from core.git.process_registry import ProcessRecord, active_processes
from core.git.repository import normalize_repo_path


class GitOperationCategory(str, Enum):
    """§9.2 操作类别：决定与同 Worktree 活动命令的兼容策略。"""

    READ_ONLY = "readonly"
    INDEX = "index"
    REF_ONLY = "ref_only"
    WORKTREE_REWRITE = "worktree_rewrite"
    COMMON_REPOSITORY = "common_repository"


# ---------------------------------------------------------------- 双级锁

_LOCKS_MASTER = threading.Lock()
_COMMON_LOCKS: Dict[str, threading.RLock] = {}
_WORKTREE_LOCKS: Dict[str, threading.RLock] = {}


def _get_lock(table: Dict[str, threading.RLock], key: str) -> threading.RLock:
    with _LOCKS_MASTER:
        lock = table.get(key)
        if lock is None:
            lock = threading.RLock()
            table[key] = lock
        return lock


@contextmanager
def common_repository_lock(common_git_dir: str):
    lock = _get_lock(_COMMON_LOCKS, normalize_repo_path(common_git_dir))
    with lock:
        yield


@contextmanager
def worktree_lock(worktree_path: str):
    lock = _get_lock(_WORKTREE_LOCKS, normalize_repo_path(worktree_path))
    with lock:
        yield


@contextmanager
def repository_locks(common_git_dir: str, worktree_path: str):
    """固定获取顺序：先 Common 后 Worktree（规格 §9.1，防死锁）。"""
    with common_repository_lock(common_git_dir):
        with worktree_lock(worktree_path):
            yield


# ---------------------------------------------------------------- 快照版本

def _stable_digest(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_worktree_version(
    *,
    identity: RepositoryIdentity,
    head_oid: str,
    index_material: str,
    status_bytes: bytes,
    operation_markers: str,
    conflict_material: str,
) -> str:
    """§7.2 worktree_version：身份 + HEAD + Index 材料 + 原始 status 字节 +
    操作状态标记 + 冲突阶段的 SHA-256；绝不读取工作区文件正文。"""
    return _stable_digest(
        {
            "scope": "worktree",
            "git_dir": normalize_repo_path(identity.git_dir),
            "worktree_root": normalize_repo_path(identity.worktree_root),
            "head_oid": head_oid,
            "index_material": index_material,
            "status_bytes": hashlib.sha256(status_bytes).hexdigest(),
            "operation_markers": operation_markers,
            "conflict_material": conflict_material,
        }
    )


def compute_refs_version(
    *,
    common_git_dir: str,
    ref_oids: Dict[str, str],
    remote_config_digest: str,
) -> str:
    """§7.2 refs_version：Common Git Dir + 目标 Ref/上游 OID + Remote 配置摘要。"""
    return _stable_digest(
        {
            "scope": "refs",
            "common_git_dir": normalize_repo_path(common_git_dir),
            "ref_oids": dict(sorted(ref_oids.items())),
            "remote_config_digest": remote_config_digest,
        }
    )


def versions_match(expected: Optional[str], actual: Optional[str]) -> bool:
    """恒定时间比较（规格 §7.2：服务端在锁内重算并恒定格式比较）。"""
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    return hmac.compare_digest(str(expected), str(actual))


# ---------------------------------------------------------------- 兼容矩阵

# 可安全识别、不与 Index/Ref/共享仓库操作互斥的风险类别
_SAFE_RISK_CLASSES = frozenset({"readonly", "service", "git"})


def check_compatible(
    category: GitOperationCategory,
    records: Iterable[ProcessRecord],
) -> Tuple[bool, List[str]]:
    """§9.2 操作兼容矩阵。返回 (是否允许, 需要停止的任务清单)。"""
    blockers: List[str] = []
    for record in records:
        label = f"{record.kind}:{record.handle}"
        if category is GitOperationCategory.READ_ONLY:
            continue
        if category is GitOperationCategory.WORKTREE_REWRITE:
            # 表格行：同 Worktree 存在任何活动命令或后台服务时拒绝
            blockers.append(label)
            continue
        # INDEX / REF_ONLY / COMMON_REPOSITORY：可识别只读或长期监听服务允许
        if record.risk_class in _SAFE_RISK_CLASSES:
            continue
        blockers.append(label)
    return (not blockers, blockers)


def compatible_for_workspace(
    category: GitOperationCategory,
    workspace: str,
) -> Tuple[bool, List[str]]:
    """便捷入口：直接查询注册表中该工作区的活动进程并套用矩阵。"""
    return check_compatible(category, active_processes(workspace))
