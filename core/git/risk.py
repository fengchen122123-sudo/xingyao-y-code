from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from core.git.models import GitError, GitErrorCode, SnapshotVersions


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_REVERSIBLE = "local_reversible"
    REMOTE_WRITE = "remote_write"
    HISTORY_REWRITE = "history_rewrite"
    UNRECOVERABLE = "unrecoverable"


_OPERATION_RISK: Dict[str, RiskLevel] = {
    # 只读
    "status": RiskLevel.READ_ONLY,
    "log": RiskLevel.READ_ONLY,
    "diff": RiskLevel.READ_ONLY,
    "blame": RiskLevel.READ_ONLY,
    "reflog": RiskLevel.READ_ONLY,
    "fetch": RiskLevel.READ_ONLY,
    "stage": RiskLevel.LOCAL_REVERSIBLE,
    "unstage": RiskLevel.LOCAL_REVERSIBLE,
    "discard_hunk": RiskLevel.UNRECOVERABLE,
    "commit": RiskLevel.LOCAL_REVERSIBLE,
    "undo_last_commit": RiskLevel.LOCAL_REVERSIBLE,
    "branch_create": RiskLevel.LOCAL_REVERSIBLE,
    "branch_rename": RiskLevel.LOCAL_REVERSIBLE,
    "branch_delete_safe": RiskLevel.LOCAL_REVERSIBLE,
    "branch_delete_force": RiskLevel.UNRECOVERABLE,
    "switch": RiskLevel.LOCAL_REVERSIBLE,
    "set_upstream": RiskLevel.LOCAL_REVERSIBLE,
    "remote_add": RiskLevel.LOCAL_REVERSIBLE,
    "remote_set_url": RiskLevel.LOCAL_REVERSIBLE,
    "pull": RiskLevel.LOCAL_REVERSIBLE,
    "stash_create": RiskLevel.LOCAL_REVERSIBLE,
    "stash_apply": RiskLevel.LOCAL_REVERSIBLE,
    "stash_pop": RiskLevel.LOCAL_REVERSIBLE,
    "stash_branch": RiskLevel.LOCAL_REVERSIBLE,
    "tag_create": RiskLevel.LOCAL_REVERSIBLE,
    "tag_delete_local": RiskLevel.LOCAL_REVERSIBLE,
    "merge": RiskLevel.LOCAL_REVERSIBLE,
    "cherry_pick": RiskLevel.LOCAL_REVERSIBLE,
    "revert": RiskLevel.LOCAL_REVERSIBLE,
    "reset_soft": RiskLevel.LOCAL_REVERSIBLE,
    "reset_mixed": RiskLevel.LOCAL_REVERSIBLE,
    "worktree_add": RiskLevel.LOCAL_REVERSIBLE,
    "worktree_remove": RiskLevel.LOCAL_REVERSIBLE,
    "worktree_prune": RiskLevel.LOCAL_REVERSIBLE,
    "ignore_add": RiskLevel.LOCAL_REVERSIBLE,
    "apply_patch": RiskLevel.LOCAL_REVERSIBLE,
    "export_patch": RiskLevel.READ_ONLY,
    "rebase_plan_write": RiskLevel.HISTORY_REWRITE,
    "push": RiskLevel.REMOTE_WRITE,
    "tag_push": RiskLevel.REMOTE_WRITE,
    "tag_delete_remote": RiskLevel.UNRECOVERABLE,
    "branch_delete_remote": RiskLevel.UNRECOVERABLE,
    "push_force_with_lease": RiskLevel.HISTORY_REWRITE,
    "rebase": RiskLevel.HISTORY_REWRITE,
    "amend_published": RiskLevel.HISTORY_REWRITE,
    # 不可恢复（高风险确认 + 输入目标分支名/短 Hash）
    "reset_hard": RiskLevel.UNRECOVERABLE,
    "clean": RiskLevel.UNRECOVERABLE,
    "stash_drop": RiskLevel.UNRECOVERABLE,
    "remote_delete": RiskLevel.UNRECOVERABLE,
    "worktree_remove_dirty": RiskLevel.UNRECOVERABLE,
}


def classify_risk(operation: str) -> RiskLevel:
    level = _OPERATION_RISK.get(operation)
    if level is None:
        raise GitError(
            GitErrorCode.UNSAFE_GIT_EXTENSION,
            f"操作 {redact(operation)} 不在风险白名单内",
        )
    return level


def requires_confirmation(level: RiskLevel) -> bool:
    """历史改写 / 不可恢复 → 高风险两段式确认。"""
    return level in (RiskLevel.HISTORY_REWRITE, RiskLevel.UNRECOVERABLE)


def requires_typed_confirmation(level: RiskLevel) -> bool:
    return level is RiskLevel.UNRECOVERABLE


def redact(text: str) -> str:
    from core.git.models import redact_text

    return redact_text(text)


@dataclass
class _Binding:
    session_id: str
    repo_digest: str
    params_digest: str
    worktree_version: Optional[str]
    refs_version: Optional[str]
    expires_at: float


class ConfirmationService:

    def __init__(self, ttl_seconds: float = 60.0):
        self._secret = secrets.token_bytes(32)
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._bindings: Dict[str, _Binding] = {}

    @staticmethod
    def params_digest(params: dict) -> str:
        """参数摘要：规范化 JSON 的 SHA-256（服务端核对原始结构化参数）。"""
        canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def issue(
        self,
        *,
        session_id: str,
        repo_digest: str,
        params_digest: str,
        versions: SnapshotVersions,
        ttl_seconds: Optional[float] = None,
    ) -> str:
        nonce = secrets.token_hex(16)
        token = hmac.new(self._secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()[:40]
        ttl = self._ttl if ttl_seconds is None else ttl_seconds
        with self._lock:
            self._bindings[token] = _Binding(
                session_id=str(session_id),
                repo_digest=str(repo_digest),
                params_digest=str(params_digest),
                worktree_version=versions.worktree_version,
                refs_version=versions.refs_version,
                expires_at=time.time() + ttl,
            )
        return token

    def verify_and_consume(
        self,
        token: str,
        *,
        session_id: str,
        repo_digest: str,
        params_digest: str,
        versions: SnapshotVersions,
    ) -> None:
        """全部绑定一致才通过；一次性：无论成败都立即消费。"""
        with self._lock:
            binding = self._bindings.pop(str(token), None)
        if binding is None:
            raise _invalid("确认令牌无效或已使用")
        if time.time() > binding.expires_at:
            raise _invalid("确认令牌已过期（60 秒有效期）")
        if binding.session_id != str(session_id):
            raise _invalid("确认令牌与会话不匹配")
        if binding.repo_digest != str(repo_digest):
            raise _invalid("确认令牌与仓库不匹配")
        if binding.params_digest != str(params_digest):
            raise _invalid("确认令牌与操作参数不匹配")
        if not _version_same(binding.worktree_version, versions.worktree_version):
            raise _invalid("工作区状态已变化，旧确认已失效，请重新确认")
        if not _version_same(binding.refs_version, versions.refs_version):
            raise _invalid("共享引用状态已变化，旧确认已失效，请重新确认")


def _version_same(expected: Optional[str], actual: Optional[str]) -> bool:
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    return hmac.compare_digest(str(expected), str(actual))


def _invalid(message: str) -> GitError:
    return GitError(GitErrorCode.CONFIRMATION_INVALID, message)
