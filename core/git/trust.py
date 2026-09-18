"""仓库信任策略（规格 §4.5）。

参数数组只能防 Shell 重解释，防不住 Git 自身调用的 Hooks/Filter/fsmonitor/
外部 Diff/Textconv/Credential Helper/Remote Helper。手动 Git 工作台对每个仓库
维护独立信任：

- 指纹 = 规范化 Common Git Dir + Worktree + 安全相关配置摘要；
- 任一安全配置变化 → 已有信任失效，重新询问；
- "允许本次"只存进程内存；"信任此仓库"持久化 git_trust.json（原子写）；
- 不保存凭据或 Hook 内容。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Dict, Optional, Tuple

from core.git.models import RepositoryIdentity, redact_text
from core.git.repository import normalize_repo_path
from core.git.runner import ExecScope, GitRunner

TRUST_FILENAME = "git_trust.json"
TRUST_FORMAT_VERSION = 1

# 纳入指纹的安全相关配置键（前缀匹配，小写）。不收 remote.*（URL 变化频繁，
# 外部 Helper 由 runner 恒定注入 protocol.ext.allow=never 与协议白名单兜底）。
_SECURITY_KEY_PREFIXES = (
    "core.hookspath",
    "core.fsmonitor",
    "filter.",
    "diff.",
    "credential.",
    "gpg.program",
    "gpg.ssh.program",
    "sendemail.smtpserver",
)

_LOCK = threading.RLock()
_ONCE_ALLOWED: set = set()


def trust_store_path() -> str:
    """YCODE_DATA_DIR 优先（模式照 core/permission_store.py；config 为根模块）。"""
    import config

    return os.path.join(str(config.YCODE_DIR), TRUST_FILENAME)


def repository_id(identity: RepositoryIdentity) -> str:
    """仓库唯一标识：规范化 Common Git Dir（共享引用的根）。"""
    return normalize_repo_path(identity.common_git_dir)


def collect_security_fingerprint(runner: GitRunner, identity: RepositoryIdentity) -> str:
    """采集安全相关配置摘要。配置读取使用 NUL 分隔（值可含换行）。"""
    result = runner.run_checked(
        ["config", "--list", "-z"], cwd=identity.worktree_root, timeout=15.0
    )
    parts = result.stdout.split(b"\x00")
    selected = []
    i = 0
    while i + 1 < len(parts):
        key = parts[i].decode("utf-8", "replace").strip()
        value = parts[i + 1].decode("utf-8", "replace").strip()
        i += 2
        lowered = key.lower()
        if any(lowered == p or lowered.startswith(p) for p in _SECURITY_KEY_PREFIXES):
            # 只收键名与值的哈希材料，不落盘原文
            selected.append(f"{lowered}={value}")
    material = {
        "repo": repository_id(identity),
        "worktree": normalize_repo_path(identity.worktree_root),
        "security_config": sorted(selected),
    }
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_trust_store(path: Optional[str] = None) -> Dict:
    target = path or trust_store_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("repos"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": TRUST_FORMAT_VERSION, "repos": {}}


def save_trust_store(store: Dict, path: Optional[str] = None) -> None:
    """原子写（复用 config._atomic_write_json：tmp + fsync + os.replace + .bak）。"""
    import config

    target = path or trust_store_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    config._atomic_write_json(target, store, indent=2)


def check_trust(identity: RepositoryIdentity, fingerprint: str) -> str:
    """返回 "trusted" | "allowed_once" | "untrusted"。"""
    repo_id = repository_id(identity)
    with _LOCK:
        if (repo_id, fingerprint) in _ONCE_ALLOWED:
            return "allowed_once"
    store = load_trust_store()
    record = store.get("repos", {}).get(repo_id)
    if record and record.get("fingerprint") == fingerprint:
        return "trusted"
    return "untrusted"


def allow_once(identity: RepositoryIdentity, fingerprint: str) -> None:
    """"允许本次"：仅当前进程内存，不落盘。"""
    with _LOCK:
        _ONCE_ALLOWED.add((repository_id(identity), fingerprint))


def trust_repository(
    identity: RepositoryIdentity,
    fingerprint: str,
    *,
    path: Optional[str] = None,
) -> None:
    """持久化信任：只保存规范化仓库标识、安全配置指纹、确认时间与范围。"""
    repo_id = repository_id(identity)
    store = load_trust_store(path)
    store.setdefault("version", TRUST_FORMAT_VERSION)
    store.setdefault("repos", {})
    store["repos"][repo_id] = {
        "fingerprint": fingerprint,
        "trusted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scope": "repo",
    }
    save_trust_store(store, path)


def require_trust(identity: RepositoryIdentity, fingerprint: str) -> None:
    """未信任仓库只允许硬化元数据查询；触发外部执行机制前必须先过此门。"""
    level = check_trust(identity, fingerprint)
    if level == "trusted":
        return
    raise trust_error(level)


def trust_error(level: str) -> Exception:
    from core.git.models import GitError, GitErrorCode

    if level == "allowed_once":
        return GitError(
            GitErrorCode.REPOSITORY_UNTRUSTED,
            "仓库仅有一次性许可，请重新确认后继续",
        )
    return GitError(
        GitErrorCode.REPOSITORY_UNTRUSTED,
        "仓库未信任：该操作可能触发 Hooks/Filter/外部 Diff/fsmonitor/Credential Helper，"
        "请先在界面确认“允许本次”或“信任此仓库”",
    )
