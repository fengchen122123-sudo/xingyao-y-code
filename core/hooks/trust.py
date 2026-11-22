from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from core.hooks.types import DEFAULT_TIMEOUT_MS

_logger = logging.getLogger("ycode.hooks")

TRUST_PENDING = "pending"
TRUST_TRUSTED = "trusted"
TRUST_BLOCKED = "blocked"
TRUST_STALE = "stale"
TRUST_REVOKED = "revoked"

_TRUST_STORE_VERSION = 1


def _ycode_dir() -> Path:
    env_dir = os.environ.get("YCODE_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".ycode"


def default_hook_trust_path() -> Path:
    return _ycode_dir() / "hook_trust.json"


def normalize_workspace_identity(workspace_root) -> str:
    try:
        from core.workspace_context import _normalize

        return str(_normalize(workspace_root))
    except Exception:
        try:
            return str(Path(str(workspace_root or "")).expanduser().resolve())
        except (OSError, ValueError, TypeError):
            return str(Path.cwd().resolve())


def compute_hook_digest(hook_entry: dict, source_path: str) -> str:
    entry = hook_entry if isinstance(hook_entry, dict) else {}
    raw_timeout = entry.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    try:
        timeout_ms = int(raw_timeout)
    except (TypeError, ValueError):
        timeout_ms = DEFAULT_TIMEOUT_MS
    normalized = {
        "event": str(entry.get("event", "")),
        "command": str(entry.get("command", "")),
        "matcher": str(entry.get("matcher", "*")),
        "timeout_ms": timeout_ms,
        "source_path": str(source_path),
    }
    payload = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_key(workspace_identity: str, digest: str) -> str:
    return f"{workspace_identity}::{digest}"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _record_is_expired(record: dict) -> bool:
    expires_at = record.get("expires_at")
    if expires_at is None:
        return False
    if not isinstance(expires_at, str) or not expires_at.strip():
        return True
    try:
        deadline = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    try:
        now = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now()
    except (ValueError, OSError):
        return True
    return deadline <= now


class HookTrustStore:

    def __init__(self, store_path=None, *, logger: logging.Logger | None = None) -> None:
        self._path = Path(store_path) if store_path else default_hook_trust_path()
        self._logger = logger or _logger

    @property
    def store_path(self) -> Path:
        return self._path

    def status_for(
        self,
        *,
        workspace_identity: str,
        digest: str,
        source_path: str,
        scope: str,
    ) -> str:
        key = _record_key(workspace_identity, digest)
        records, revoked, corrupted = self._load_store()
        if scope == "user":
            # 用户级：默认信任（用户自己写的），仅显式撤销后不可执行。
            # 存储损坏不影响用户级：一次存储故障不应停掉用户自己的 hook。
            return TRUST_REVOKED if key in revoked else TRUST_TRUSTED
        if corrupted:
            return TRUST_BLOCKED
        if key in revoked:
            return TRUST_REVOKED
        record = records.get(key)
        if isinstance(record, dict):
            decision = record.get("decision")
            if decision == "trusted":
                return TRUST_PENDING if _record_is_expired(record) else TRUST_TRUSTED
            if decision == "blocked":
                return TRUST_BLOCKED
        normalized_source = str(source_path)
        for other in records.values():
            if not isinstance(other, dict):
                continue
            if other.get("decision") != "trusted":
                continue
            if str(other.get("source_path", "")) == normalized_source:
                return TRUST_STALE
        return TRUST_PENDING

    def grant(
        self, *, workspace_identity: str, digest: str, source_path: str, command: str = ""
    ) -> bool:
        """用户确认信任（pending/stale → trusted）。写入失败返回 False。"""
        return self._write_record(
            workspace_identity=workspace_identity,
            digest=digest,
            decision="trusted",
            source_path=source_path,
            command=command,
        )

    def deny(
        self, *, workspace_identity: str, digest: str, source_path: str, command: str = ""
    ) -> bool:
        """用户拒绝（pending → blocked，不再询问）。"""
        return self._write_record(
            workspace_identity=workspace_identity,
            digest=digest,
            decision="blocked",
            source_path=source_path,
            command=command,
        )

    def revoke(self, *, workspace_identity: str, digest: str) -> bool:
        """用户撤销（trusted → revoked）：记录进 revoked 列表并清除信任记录。"""
        key = _record_key(workspace_identity, digest)
        records, revoked, _ = self._load_store()
        records.pop(key, None)
        revoked.add(key)
        return self._save(records, revoked)

    def reset(self, *, workspace_identity: str, digest: str) -> bool:
        """手动重置（blocked/revoked → pending）：清除记录与撤销标记。"""
        key = _record_key(workspace_identity, digest)
        records, revoked, _ = self._load_store()
        records.pop(key, None)
        revoked.discard(key)
        return self._save(records, revoked)

    def _write_record(
        self,
        *,
        workspace_identity: str,
        digest: str,
        decision: str,
        source_path: str,
        command: str,
    ) -> bool:
        key = _record_key(workspace_identity, digest)
        records, revoked, _ = self._load_store()
        revoked.discard(key)
        records[key] = {
            "decision": decision,
            "source_path": str(source_path),
            "command_at_grant": str(command),
            "granted_at": _now_iso(),
            "expires_at": None,
        }
        return self._save(records, revoked)

    def _load_store(self) -> tuple[dict, set, bool]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}, set(), False
        except OSError as exc:
            self._logger.warning("hook 信任存储读取失败（fail-closed）: %s", exc)
            return {}, set(), True
        try:
            data = json.loads(raw)
        except ValueError:
            self._logger.warning("hook 信任存储 JSON 非法（fail-closed）: %s", self._path)
            return {}, set(), True
        if not isinstance(data, dict):
            self._logger.warning(
                "hook 信任存储顶层不是对象（fail-closed）: %s", self._path
            )
            return {}, set(), True
        records = data.get("records")
        if records is None:
            records = {}
        elif not isinstance(records, dict):
            self._logger.warning(
                "hook 信任存储 records 字段非法（fail-closed）: %s", self._path
            )
            return {}, set(), True
        revoked_raw = data.get("revoked")
        if revoked_raw is None:
            revoked_raw = []
        elif not isinstance(revoked_raw, list):
            self._logger.warning(
                "hook 信任存储 revoked 字段非法（fail-closed）: %s", self._path
            )
            return {}, set(), True
        revoked = {str(item) for item in revoked_raw if isinstance(item, str)}
        return dict(records), revoked, False

    def _save(self, records: dict, revoked: set) -> bool:
        """原子写存储（临时文件 + os.replace），失败只 warning 不抛错。"""
        payload = {
            "version": _TRUST_STORE_VERSION,
            "records": records,
            "revoked": sorted(revoked),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_name(self._path.name + ".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp_path, self._path)
            return True
        except OSError as exc:
            self._logger.warning("hook 信任存储写入失败: %s", exc)
            return False


def list_hooks_with_status(
    *, workspace_root=None, registry=None, store=None
) -> list[dict]:
    from core.hooks.registry import HookRegistry

    reg = registry if registry is not None else HookRegistry()
    st = store if store is not None else HookTrustStore()
    identity = normalize_workspace_identity(workspace_root)
    hooks: list[dict] = []
    for item in reg.load(workspace_root=workspace_root, include_disabled=True):
        hooks.append(
            {
                "event": item.event.value,
                "matcher": item.matcher,
                "command": item.command,
                "timeout_ms": item.timeout_ms,
                "status_message": item.status_message,
                "source_path": item.source_path,
                "scope": item.scope,
                "digest": item.digest,
                "disabled": item.disabled,
                "status": st.status_for(
                    workspace_identity=identity,
                    digest=item.digest,
                    source_path=item.source_path,
                    scope=item.scope,
                ),
            }
        )
    return hooks


def apply_hook_trust_action(
    action, digest, *, workspace_root=None, registry=None, store=None
) -> tuple[bool, str]:
    if action not in ("grant", "deny", "revoke", "reset"):
        return False, f"未知操作: {action}"
    st = store if store is not None else HookTrustStore()
    identity = normalize_workspace_identity(workspace_root)
    target = next(
        (
            item
            for item in list_hooks_with_status(
                workspace_root=workspace_root, registry=registry, store=st
            )
            if item["digest"] == digest
        ),
        None,
    )
    if target is None:
        return False, "未找到对应的 hook（配置可能已变更，请刷新后重试）"
    if action in ("grant", "deny"):
        write = st.grant if action == "grant" else st.deny
        ok = write(
            workspace_identity=identity,
            digest=digest,
            source_path=target["source_path"],
            command=target["command"],
        )
        message = "已信任" if action == "grant" else "已拒绝，之后不再询问"
    elif action == "revoke":
        ok = st.revoke(workspace_identity=identity, digest=digest)
        message = "已撤销信任"
    else:
        ok = st.reset(workspace_identity=identity, digest=digest)
        message = "已重置为待确认"
    if not ok:
        # 存储写入失败（磁盘满 / 权限问题）不能假装成功：GUI 卡片会显示旧状态，
        # 用户必须知道这次操作没生效。
        return False, "写入信任存储失败"
    return True, message
