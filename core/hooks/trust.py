"""Hooks 信任机制：指纹计算 + 状态机 + 存储（面向 ZCode 能力借鉴 · 规格 01 · C3）。

威胁模型（规格 §4.1）：项目级 hook 配置随仓库 clone 而来，直接执行其命令等同
在本机执行攻击者的任意 shell 命令。因此项目级默认 pending（需用户确认），
用户级默认 trusted（用户自己写的，可撤销）。

指纹设计（规格 §4.3）：只含「影响执行行为」的字段——事件、command、matcher、
归一化 timeout_ms、source_path。``status_message`` 是纯展示字段，变更**不**导致
信任失效（场景 4 验收要求）；command/matcher/timeout_ms 任一变更**都**失效。

fail-closed 细则（规格 §4.2 / §7）：

- 存储 JSON 非法 / 顶层结构损坏 / 读取 IO 失败 → 所有项目级 hook 视为 blocked
  （不执行也不再询问）；用户级不受影响（仍按默认 trusted，否则一次存储故障会
  连用户自己写的 hook 一起停掉）；
- 状态判定每次从磁盘读取、不做进程内缓存——执行期间撤销必须立即生效；
- 过期时间非法（解析失败）视为已过期（fail-closed）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from core.hooks.types import DEFAULT_TIMEOUT_MS

_logger = logging.getLogger("ycode.hooks")

# 信任状态（规格 §4.2 状态机）。只有 TRUSTED 可执行。
TRUST_PENDING = "pending"    # 待用户确认（项目级首次发现）
TRUST_TRUSTED = "trusted"    # 已信任，可执行
TRUST_BLOCKED = "blocked"    # 用户拒绝，或存储损坏（fail-closed）
TRUST_STALE = "stale"        # 配置内容变更，信任失效
TRUST_REVOKED = "revoked"    # 用户主动撤销

_TRUST_STORE_VERSION = 1


def _ycode_dir() -> Path:
    """用户数据目录：与 config.YCODE_DIR 口径一致（YCODE_DATA_DIR 优先）。"""
    env_dir = os.environ.get("YCODE_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".ycode"


def default_hook_trust_path() -> Path:
    """信任存储路径：~/.ycode/hook_trust.json（规格 §4.4，独立于插件信任文件）。"""
    return _ycode_dir() / "hook_trust.json"


def normalize_workspace_identity(workspace_root) -> str:
    """工作目录的规范化绝对路径（workspace_identity 口径）。

    与 ``core/workspace_context.py`` 的 ``_normalize`` 保持一致（规格 §4.4
    明确要求），直接复用该实现而不是复制一份，避免两处口径漂移。
    """
    try:
        from core.workspace_context import _normalize

        return str(_normalize(workspace_root))
    except Exception:
        try:
            return str(Path(str(workspace_root or "")).expanduser().resolve())
        except (OSError, ValueError, TypeError):
            return str(Path.cwd().resolve())


def compute_hook_digest(hook_entry: dict, source_path: str) -> str:
    """计算单条 hook 的信任指纹（规格 §4.3）。

    ``hook_entry`` 是归一化后的条目 dict（event / command / matcher /
    timeout_ms）；``source_path`` 单独传入以区分同名不同位置的 hook。

    实现要点：字段白名单 + 规范化 JSON（sort_keys、紧凑分隔符），
    因此字段顺序无关、纯展示字段（status_message 等）不参与。
    """
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
    """记录是否已过期（expires_at 为预留字段，v1 恒为 null）。

    过期/非法一律按「不再可信」处理（fail-closed）：预留字段一旦被写入，
    坏值不能成为绕过确认的通道。
    """
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
    """hook 信任存储的读写与状态判定。

    为什么每次状态判定都重读磁盘（无进程内缓存）：规格 §5.4 要求「不缓存
    admission」——前序 hook 运行期间发生的 revoke 必须对尚未开始的 hook
    立即生效。存储文件很小，重读成本可忽略。
    """

    def __init__(self, store_path=None, *, logger: logging.Logger | None = None) -> None:
        self._path = Path(store_path) if store_path else default_hook_trust_path()
        self._logger = logger or _logger

    @property
    def store_path(self) -> Path:
        return self._path

    # ── 状态判定 ────────────────────────────────────────────────

    def status_for(
        self,
        *,
        workspace_identity: str,
        digest: str,
        source_path: str,
        scope: str,
    ) -> str:
        """判定单条 hook 当前信任状态（规格 §4.2 状态机）。"""
        key = _record_key(workspace_identity, digest)
        records, revoked, corrupted = self._load_store()
        if scope == "user":
            # 用户级：默认信任（用户自己写的），仅显式撤销后不可执行。
            # 存储损坏不影响用户级：一次存储故障不应停掉用户自己的 hook。
            return TRUST_REVOKED if key in revoked else TRUST_TRUSTED
        if corrupted:
            # fail-closed：存储坏了就无法证明「曾获信任」，全部按 blocked 处理
            # （规格 §7：不再询问，等用户修复/重建存储后重新确认）。
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
        # 同一来源文件曾信任过、但当前 digest 无记录 → 配置内容变更 → stale
        # （规格 §4.2：trusted → stale → 重新确认）。全新文件/全新条目则 pending。
        normalized_source = str(source_path)
        for other in records.values():
            if not isinstance(other, dict):
                continue
            if other.get("decision") != "trusted":
                continue
            if str(other.get("source_path", "")) == normalized_source:
                return TRUST_STALE
        return TRUST_PENDING

    # ── 状态迁移（规格 §4.2 图上的用户操作）────────────────────

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

    # ── 存储读写 ────────────────────────────────────────────────

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
        """读取存储，返回 ``(records, revoked_keys, corrupted)``。

        损坏判定（fail-closed）：读取 IO 失败 / JSON 非法 / 顶层非对象 /
        records 或 revoked 字段类型非法。文件不存在视为「从未授权」，不算损坏。
        """
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


# ── 信任视图与信任操作（C11：GUI 管理页 / 信任卡片 / CLI 共用）──────


def list_hooks_with_status(
    *, workspace_root=None, registry=None, store=None
) -> list[dict]:
    """列出两级 hook 配置并逐条标注信任状态（GUI 管理页 / 信任卡片 / CLI 共用）。

    为什么惰性导入 HookRegistry：``registry`` 模块级已导入本模块
    （``compute_hook_digest``），若本函数在模块级导入 registry 会形成循环导入；
    惰性导入让 trust 保持「被 registry 依赖」的单向依赖关系。

    registry / store 允许注入：调用方（GUI / CLI）各自解析配置路径与存储路径
    （如多实例隔离、测试注入 tmp 路径），视图函数不应再猜一次路径；默认值仅供
    零配置的便捷调用。

    为什么返回纯 dict 而不是 HookRegistration：本视图直接送给 GUI 前端，
    dict 可 JSON 序列化，且把「状态」与「配置」摊平成同一层，前端不必再回查。

    为什么按 ``include_disabled=True`` 加载：管理列表必须能看到被禁用的钩子
    （带 ``disabled`` 标记）才能重新启用；执行侧（runner）仍走默认口径、
    跳过 disabled 条目，因此本视图扩大可见范围不会影响执行行为。
    """
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
    """执行单条 hook 的信任操作（grant/deny/revoke/reset），返回 ``(ok, message)``。

    为什么先查视图再落库：操作对象必须是「当前配置里真实存在的 hook」——
    digest 是内容指纹，配置一变旧 digest 自然失配，此时应让用户刷新而不是把
    信任写到一个已不存在的条目上（否则信任记录会指向幽灵条目，既不可见也不
    可清理）。revoke/reset 也走同一查在流程，保证 GUI 与 CLI 无论 digest 从哪
    来，都得到统一的存在性校验与同一套提示文案。

    非法 action 先于查在返回：不必触碰 registry/store，也避免调用方把拼错的
    动作名误判为「hook 不存在」去排查。

    成功文案约定（GUI 直接展示）：grant→"已信任"；deny→"已拒绝，之后不再询问"；
    revoke→"已撤销信任"；reset→"已重置为待确认"。
    """
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
        # grant/deny 需要 source_path 与 command 落库（供管理页展示「信任的是
        # 哪条命令」），两者都从命中条目取，不信任调用方传来的副本。
        write = st.grant if action == "grant" else st.deny
        ok = write(
            workspace_identity=identity,
            digest=digest,
            source_path=target["source_path"],
            command=target["command"],
        )
        message = "已信任" if action == "grant" else "已拒绝，之后不再询问"
    elif action == "revoke":
        # revoke/reset 只按 identity+digest 操作存储，不需要来源信息。
        ok = st.revoke(workspace_identity=identity, digest=digest)
        message = "已撤销信任"
    else:  # reset
        ok = st.reset(workspace_identity=identity, digest=digest)
        message = "已重置为待确认"
    if not ok:
        # 存储写入失败（磁盘满 / 权限问题）不能假装成功：GUI 卡片会显示旧状态，
        # 用户必须知道这次操作没生效。
        return False, "写入信任存储失败"
    return True, message
