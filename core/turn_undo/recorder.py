"""星瑶按回合撤销：受控文件修改前后的记录与验证（FileMutationRecorder）。

契约（规格 9.3/9.4）：
- prepare_mutation()：工具在真正写文件前调用。记录器自行从磁盘捕获最初
  before 状态（不信任工具转述）、把计划 after 字节写入内容寻址仓库、
  在数据库持久化 before/after 记录，全部成功后签发不可伪造的 MutationHandle。
- verify_after_mutation()：工具写入成功后、声称成功前调用。立即读取实际
  节点状态与计划 after 精确比较，记录实际 File ID/硬链接计数/权限位；
  不一致、验证落库失败或文件在验证前被外部改变 → 检查点标记不可安全撤销
  并显式报错，工具不得声称成功。

严格可撤销节点只有 missing 与普通文件；最终路径符号链接、目录、特殊文件、
硬链接（nlink>1）在写入前拒绝。无活动检查点上下文的线程（回合外的独立
调用，如直接调用 tools.write_file 的测试/脚本）不记录、也不阻断——严格
保证只覆盖回合作用域内的受控修改；回合内快照/落库失败则修改必须失败。
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import threading
import uuid
from pathlib import Path
from typing import Optional

from core.turn_undo.models import (
    MutationHandle,
    OwnerKind,
    NodeKind,
    NodeState,
    SnapshotAvailability,
    SourceKind,
    TurnUndoError,
    TurnUndoStatus,
    UndoErrorCode,
    UNDO_RESULT_SKIPPED,
)
from core.turn_undo.storage import TurnUndoStorage, now_ms

logger = logging.getLogger(__name__)

# 惰性读取，超限的大文件不整体载入内存（写入类工具自身已有 50KB/10MB 上限）
_CAPTURE_CHUNK = 1024 * 1024


def canonicalize_path(path: str | os.PathLike) -> str:
    """规范化绝对路径：解析中间目录符号链接；最终组件不跟随符号链接。

    Windows 下 normcase 统一大小写折叠；执行前身份不得改变由调用方锁与
    指纹检查保证。
    """
    value = os.fspath(path)
    absolute = os.path.abspath(value)
    parent = os.path.dirname(absolute)
    name = os.path.basename(absolute)
    real_parent = os.path.realpath(parent)
    return os.path.normcase(os.path.join(real_parent, name))


def display_path_for(workspace_root: str, canonical_path: str) -> str:
    """优先返回工作区相对路径；工作区外路径返回规范化绝对路径。"""
    try:
        rel = os.path.relpath(canonical_path, workspace_root)
    except ValueError:
        return canonical_path
    if rel.startswith(".."):
        return canonical_path
    return rel.replace(os.sep, "/")


def capture_node_state(path: str) -> NodeState:
    """lstat 捕获结点种类与身份；不跟随最终符号链接。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return NodeState(kind=NodeKind.MISSING, exists=False)
    except OSError as exc:
        raise TurnUndoError(
            UndoErrorCode.IO_ERROR,
            f"无法读取目标状态 {path}: {exc}",
        ) from exc
    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            target = None
        return NodeState(
            kind=NodeKind.SYMLINK,
            exists=True,
            identity=_identity_of(st),
            link_count=st.st_nlink,
            symlink_target=target,
        )
    if stat.S_ISDIR(st.st_mode):
        return NodeState(
            kind=NodeKind.DIRECTORY,
            exists=True,
            identity=_identity_of(st),
            link_count=st.st_nlink,
        )
    if stat.S_ISREG(st.st_mode):
        # 内容哈希流式计算：预检与中途外部改动检测依赖精确 SHA-256（规格 11.2）
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(_CAPTURE_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
            content_sha = digest.hexdigest()
        except OSError:
            content_sha = None
        return NodeState(
            kind=NodeKind.REGULAR_FILE,
            exists=True,
            size=st.st_size,
            sha256=content_sha,
            mode=stat.S_IMODE(st.st_mode),
            identity=_identity_of(st),
            link_count=st.st_nlink,
        )
    return NodeState(kind=NodeKind.SPECIAL, exists=True, identity=_identity_of(st))


def _identity_of(st: os.stat_result) -> str:
    return f"{st.st_dev}:{st.st_ino}"


def _reject_unsupported(node: NodeState, canonical_path: str) -> None:
    """严格可撤销边界：最终路径符号链接/目录/特殊文件/多硬链接一律拒绝。"""
    if node.kind is NodeKind.MISSING:
        return
    if node.kind is NodeKind.SYMLINK:
        raise TurnUndoError(
            UndoErrorCode.UNSUPPORTED_NODE_KIND,
            f"目标路径是符号链接，拒绝受控修改: {canonical_path}",
        )
    if node.kind is NodeKind.DIRECTORY:
        raise TurnUndoError(
            UndoErrorCode.UNSUPPORTED_NODE_KIND,
            f"目标路径是目录，拒绝受控修改: {canonical_path}",
        )
    if node.kind is NodeKind.SPECIAL:
        raise TurnUndoError(
            UndoErrorCode.UNSUPPORTED_NODE_KIND,
            f"目标路径是设备/管道等特殊文件，拒绝受控修改: {canonical_path}",
        )
    if node.link_count is not None and node.link_count > 1:
        raise TurnUndoError(
            UndoErrorCode.UNSUPPORTED_NODE_KIND,
            f"目标文件存在 {node.link_count} 个硬链接，拒绝受控修改: {canonical_path}",
        )


# ---------------------------------------------------------------------------
# 检查点上下文注册表（线程绑定）
# ---------------------------------------------------------------------------

class RecorderContext:
    """一个活动回合检查点在记录器内的表示。"""

    __slots__ = (
        "checkpoint_id", "session_id", "turn_id", "workspace_root",
        "owner", "owner_kind",
    )

    def __init__(self, *, checkpoint_id: str, session_id: int, turn_id: str,
                 workspace_root: str, owner: bool, owner_kind: str):
        self.checkpoint_id = checkpoint_id
        self.session_id = session_id
        self.turn_id = turn_id
        self.workspace_root = workspace_root
        self.owner = owner
        self.owner_kind = owner_kind


_context_lock = threading.Lock()
_active_contexts: dict[str, RecorderContext] = {}
_context_by_turn: dict[str, str] = {}  # turn_id -> checkpoint_id（新协议全局唯一）
_tls = threading.local()

# 记录器写库与签发句柄的串行锁；文件 I/O 不在其内。
_record_lock = threading.RLock()


def set_tool_context(tool_name: str, tool_call_id: str) -> None:
    """适配层在执行工具前登记当前工具名与 call_id（TLS），供记录器采集来源。"""
    _tls.tool_name = str(tool_name or "")
    _tls.tool_call_id = str(tool_call_id or "")


def clear_tool_context() -> None:
    _tls.tool_name = ""
    _tls.tool_call_id = ""


def _current_tool() -> tuple[str, str]:
    return (
        getattr(_tls, "tool_name", "") or "unknown_tool",
        getattr(_tls, "tool_call_id", "") or "",
    )


def _storage_for_ctx() -> TurnUndoStorage:
    from core.turn_undo.service import get_default_storage

    return get_default_storage()


def begin_recording(storage: TurnUndoStorage, checkpoint) -> RecorderContext:
    """注册新检查点上下文并把当前线程绑定为 owner。"""
    ctx = RecorderContext(
        checkpoint_id=checkpoint.checkpoint_id,
        session_id=checkpoint.session_id,
        turn_id=checkpoint.turn_id,
        workspace_root=checkpoint.workspace_root,
        owner=True,
        owner_kind=checkpoint.owner_kind,
    )
    with _context_lock:
        existing = _active_contexts.get(ctx.checkpoint_id)
        if existing is not None:
            raise TurnUndoError(
                UndoErrorCode.INVALID_STATE_TRANSITION,
                f"检查点上下文已注册: {ctx.checkpoint_id}",
            )
        _active_contexts[ctx.checkpoint_id] = ctx
        _context_by_turn[ctx.turn_id] = ctx.checkpoint_id
    _tls.ctx = ctx
    _tls.owner = True
    return ctx


def bind_thread_for_turn(turn_id: str, *, owner: bool = False) -> bool:
    """按全局唯一 turn_id 绑定活动检查点（子代理共享父回合）。"""
    ctx = find_context_by_turn(turn_id)
    if ctx is None:
        return bind_thread("", owner=owner)
    return bind_thread(ctx.checkpoint_id, owner=owner)


def find_context_by_turn(turn_id: str) -> Optional[RecorderContext]:
    with _context_lock:
        checkpoint_id = _context_by_turn.get(str(turn_id or ""))
        if checkpoint_id is None:
            return None
        return _active_contexts.get(checkpoint_id)


def bind_thread(checkpoint_id: str, *, owner: bool = False) -> bool:
    """子代理线程绑定到活动检查点；owner=False 只借记录权，不可提交。"""
    with _context_lock:
        ctx = _active_contexts.get(checkpoint_id)
    if ctx is None:
        _tls.ctx = None
        _tls.owner = False
        return False
    _tls.ctx = ctx
    _tls.owner = owner
    return True


def unbind_thread() -> None:
    _tls.ctx = None
    _tls.owner = False


def current_context() -> Optional[RecorderContext]:
    return getattr(_tls, "ctx", None)


def is_context_active(ctx: Optional[RecorderContext]) -> bool:
    """TLS 指向的上下文是否仍是注册中的活动检查点。

    消息自动关联（memory.add_messages）用它过滤陈旧绑定：绑定对象已被
    注销（回合封闭）时不再写 message_turn_links；prepare/verify 路径不用
    此过滤——迟到写入必须显式失败（H2）。
    """
    if ctx is None:
        return False
    with _context_lock:
        return _active_contexts.get(ctx.checkpoint_id) is ctx


def release_context(checkpoint_id: str) -> None:
    """回合封闭后注销上下文；绑定它的线程随后触盘前会失败。"""
    with _context_lock:
        ctx = _active_contexts.pop(checkpoint_id, None)
        if ctx is not None:
            _context_by_turn.pop(ctx.turn_id, None)
    if ctx is not None:
        # 回合结束兜底：清掉本回合遗留的文件级写入占用（异常路径没走
        # verify/rollback 时也不会把这个文件永久标成"正在被写入"）
        try:
            from core.turn_undo import coordinator as coordinator_mod

            coordinator_mod.get_coordinator().release_turn_paths(
                ctx.workspace_root, ctx.turn_id,
            )
        except Exception:
            logger.debug("清理回合文件写入占用失败", exc_info=True)
    if getattr(_tls, "ctx", None) is not None and _tls.ctx.checkpoint_id == checkpoint_id:
        unbind_thread()


# ---------------------------------------------------------------------------
# 记录与验证
# ---------------------------------------------------------------------------

def _capture_before_with_blob(storage: TurnUndoStorage, canonical_path: str) -> tuple[NodeState, Optional[str]]:
    """捕获最初 before 状态并流式写入内容仓库；返回 (状态, blob_sha)。"""
    node = capture_node_state(canonical_path)
    if node.kind is not NodeKind.REGULAR_FILE:
        return node, None
    digest = hashlib.sha256()
    with open(canonical_path, "rb") as handle:
        while True:
            chunk = handle.read(_CAPTURE_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        handle.seek(0)
        blob_sha = storage.blob_store.put_stream(handle)
    if digest.hexdigest() != blob_sha:
        raise TurnUndoError(
            UndoErrorCode.SNAPSHOT_CORRUPT,
            f"修改前快照哈希不一致: {canonical_path}",
        )
    return NodeState(
        kind=node.kind,
        exists=True,
        size=node.size,
        sha256=blob_sha,
        mode=node.mode,
        identity=node.identity,
        link_count=node.link_count,
        symlink_target=node.symlink_target,
        blob_sha256=blob_sha,
    ), blob_sha


def _require_active_context(*, need_owner: bool) -> RecorderContext:
    ctx = current_context()
    if ctx is None:
        # 严格区分（H2）：线程完全没有检查点上下文 = 回合外调用，
        # 由工具层降级为不可撤销写入；绝不能与"上下文存在但检查点已封闭"
        # 混用同一错误码，否则迟到写入会被降级短路。
        raise TurnUndoError(
            UndoErrorCode.NO_ACTIVE_CHECKPOINT,
            "当前线程没有绑定的活动回合检查点（回合外调用）",
        )
    if need_owner and not ctx.owner:
        # 子代理只借记录权；prepare/verify 都允许，但封闭检查点由 registry 判定。
        pass
    return ctx

def _claim_path_for_write(ctx: RecorderContext, canonical_path: str) -> None:
    """同一文件同一时刻只允许一个前台回合在写（规格 9.2 并发保护）。

    互斥粒度是文件，不是目录：同目录下不同文件的两个任务互不影响（一个会话改
    前端、另一个改后端本来就该并行；目录级独占会让一次长回合把整个工作区写死）。
    只有真正同时写同一个文件，才会在 prepare→verify 这段写入窗口内快速失败，
    让调用方等待重试。后台任务检查点按设计允许与父回合并存，不参与互斥。
    """
    if ctx.owner_kind == OwnerKind.BACKGROUND_TASK.value:
        return
    from core.turn_undo import coordinator as coordinator_mod

    coordinator = coordinator_mod.get_coordinator()
    owner = coordinator.claim_path(ctx.workspace_root, canonical_path, ctx.turn_id)
    if owner and owner != ctx.turn_id:
        raise TurnUndoError(
            UndoErrorCode.WORKSPACE_BUSY,
            f"该文件正在被另一个回合写入（回合 {owner[:8]}），"
            f"请等待其完成后再重试: {canonical_path}",
            detail={
                "active_turn_id": owner,
                "path": canonical_path,
            },
        )


def _release_path_claim(ctx: Optional[RecorderContext], canonical_path: str) -> None:
    """释放本回合对该文件的写入占用；重复调用无副作用。"""
    if ctx is None or not canonical_path:
        return
    try:
        from core.turn_undo import coordinator as coordinator_mod

        coordinator_mod.get_coordinator().release_path(
            ctx.workspace_root, canonical_path, ctx.turn_id,
        )
    except Exception:  # 释放失败只影响重试延迟，不能掩盖写入结果
        logger.debug("释放文件写入占用失败: %s", canonical_path, exc_info=True)

def prepare_mutation(
    *,
    display_path: str,
    after_kind: NodeKind,
    after_content: Optional[bytes],
    tool_name: str,
    tool_call_id: str,
    source_kind: Optional[SourceKind] = None,
    source_id: Optional[str] = None,
) -> MutationHandle:
    """写入前调用：捕获 before、持久化计划 after、签发句柄。

    抛出任何 TurnUndoError 时目标文件尚未被本调用触碰，工具必须失败。
    """
    if not isinstance(after_kind, NodeKind):
        raise TurnUndoError(
            UndoErrorCode.INVALID_INPUT,
            f"after_kind 必须是 NodeKind: {after_kind!r}",
        )
    if after_kind not in (NodeKind.MISSING, NodeKind.REGULAR_FILE):
        raise TurnUndoError(
            UndoErrorCode.UNSUPPORTED_NODE_KIND,
            f"严格可撤销节点仅限 missing/regular_file: {after_kind!r}",
        )
    if after_kind is NodeKind.MISSING and after_content is not None:
        raise TurnUndoError(
            UndoErrorCode.INVALID_INPUT,
            "计划 after 为 missing 时不得携带内容字节",
        )
    if after_kind is NodeKind.REGULAR_FILE and after_content is None:
        raise TurnUndoError(
            UndoErrorCode.INVALID_INPUT,
            "计划 after 为 regular_file 时必须携带完整内容字节",
        )

    ctx = _require_active_context(need_owner=True)
    storage = _storage_for_ctx()
    checkpoint = storage.get_checkpoint(ctx.checkpoint_id)
    if checkpoint.status_enum is not TurnUndoStatus.RECORDING:
        reason = (checkpoint.last_error_message or "").strip()
        raise TurnUndoError(
            UndoErrorCode.CHECKPOINT_CLOSED,
            f"检查点已封闭（{checkpoint.status_enum.value}），拒绝新写入句柄: "
            f"{ctx.checkpoint_id}"
            + (f"；原因：{reason}" if reason else ""),
            detail={
                "checkpoint_id": ctx.checkpoint_id,
                "status": checkpoint.status_enum.value,
                "reason": reason,
            },
        )

    canonical_path = canonicalize_path(display_path)
    # 触盘前的文件级占用：同目录其它回合改别的文件完全不受影响。占用在
    # verify_after_mutation / rollback_prepared_mutation 释放，回合封闭时兜底清理。
    _claim_path_for_write(ctx, canonical_path)
    if source_kind is None:
        # 来源默认值（规格 5.4）：借记录权的线程（前台子代理，线程绑定 owner=False）
        # 与后台任务检查点都是子代理修改；只有线程 own 的主回合线程记为主代理
        thread_is_owner = bool(getattr(_tls, "owner", False))
        if not thread_is_owner or ctx.owner_kind == OwnerKind.BACKGROUND_TASK.value:
            source_kind = SourceKind.SUBAGENT
        else:
            source_kind = SourceKind.MAIN_AGENT
    if source_id is None:
        source_id = ctx.turn_id
    tls_tool, tls_call = _current_tool()
    tool_name = tool_name or tls_tool
    tool_call_id = tool_call_id or tls_call

    with _record_lock:
        # 1. 捕获最初 before（同一回合多次修改保留第一次的 before）
        existing = storage.get_file_by_path(ctx.checkpoint_id, canonical_path)
        before_node, before_blob = _capture_before_with_blob(storage, canonical_path)
        _reject_unsupported(before_node, canonical_path)

        # 中途外部改动检测：当前磁盘状态必须仍等于上一计划 after（首改除外）。
        # 命中冲突时只让该文件退出本回合撤销范围，不再把整个检查点判死：
        # 长回合里任何一次外部写入都不应让后续全部写入失效。
        file_skipped = False
        if existing is not None:
            planned = _planned_after_state(existing)
            if not _state_matches_disk(planned, canonical_path):
                file_skipped = True
                logger.warning(
                    "按回合撤销：文件在回合内被外部修改，退出撤销范围: %s",
                    canonical_path,
                )

        # 2. 计划 after 字节写入内容仓库
        after_sha = None
        after_blob = None
        if after_content is not None:
            after_sha = hashlib.sha256(after_content).hexdigest()
            after_blob = storage.blob_store.put_bytes(after_content)

        planned_after = NodeState(
            kind=after_kind,
            exists=after_kind is not NodeKind.MISSING,
            size=len(after_content) if after_content is not None else None,
            sha256=after_sha,
            mode=None,  # 由 verify_after_mutation 捕获实际权限位
            blob_sha256=after_blob,
        )

        # 3. 短事务持久化记录
        mutated_at = now_ms()
        values = _file_values_from_states(
            canonical_path=canonical_path,
            display_path=display_path_for(ctx.workspace_root, canonical_path),
            before=before_node if existing is None else None,
            after=planned_after,
            mutated_at_ms=mutated_at,
            source_kind=source_kind.value,
            source_id=source_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
        previous_after = None
        if existing is not None:
            previous_after = _planned_after_state(existing)
        with storage._manager._lock:
            cursor = storage.conn.cursor()
            try:
                if existing is None:
                    file_id = storage.insert_file_locked(
                        cursor, checkpoint_id=ctx.checkpoint_id, values=values,
                    )
                    first = True
                else:
                    file_id = existing.file_id
                    storage.update_after_plan_locked(
                        cursor, file_id=file_id,
                        checkpoint_id=ctx.checkpoint_id, values=values,
                    )
                    first = False
                if file_skipped:
                    # 同事务标记：该文件退出本回合撤销范围，但 after 计划仍更新，
                    # 保证后续写入不会重复命中同一条冲突
                    storage.set_file_undo_result_locked(
                        cursor,
                        file_id=file_id,
                        undo_result=UNDO_RESULT_SKIPPED,
                        undo_error_code=UndoErrorCode.FILE_CONFLICT,
                    )
                storage.conn.commit()
            except Exception:
                storage.conn.rollback()
                raise

    return MutationHandle(
        handle_id=uuid.uuid4().hex,
        checkpoint_id=ctx.checkpoint_id,
        file_id=file_id,
        canonical_path=canonical_path,
        planned_after=planned_after,
        is_first_mutation=first,
        previous_after=previous_after,
    )


def rollback_prepared_mutation(handle: MutationHandle) -> None:
    """prepare 之后、实际写入失败时回滚本次计划记录。

    - 首次修改：整行删除（blob 失去引用，由 GC 回收）。
    - 后续修改：恢复被替换前的已验证 after 状态（写入未发生，磁盘未变；
      若磁盘已与上一计划不一致则显式失败——诚实暴露外部改动）。
    """
    # 写入没成功：先放开文件占用，别的回合可立即重试同一文件
    _release_path_claim(current_context(), handle.canonical_path)
    storage = _storage_for_ctx()
    if handle.is_first_mutation or handle.previous_after is None:
        with storage._manager._lock:
            cursor = storage.conn.cursor()
            try:
                cursor.execute(
                    "DELETE FROM turn_undo_files WHERE file_id = ? AND checkpoint_id = ? "
                    "AND verified_at_ms IS NULL",
                    (handle.file_id, handle.checkpoint_id),
                )
                cursor.execute(
                    "UPDATE turn_undo_checkpoints SET record_version = record_version + 1 "
                    "WHERE checkpoint_id = ?",
                    (handle.checkpoint_id,),
                )
                storage.conn.commit()
            except Exception:
                storage.conn.rollback()
                raise
        return
    previous = handle.previous_after
    with _record_lock:
        with storage._manager._lock:
            cursor = storage.conn.cursor()
            try:
                storage.update_file_verified_locked(
                    cursor,
                    file_id=handle.file_id,
                    actual={
                        "after_kind": previous.kind.value,
                        "after_exists": 1 if previous.exists else 0,
                        "after_size": previous.size,
                        "after_sha256": previous.sha256,
                        "after_blob_sha256": previous.blob_sha256,
                        "after_mode": previous.mode,
                        "after_file_identity": previous.identity,
                        "after_link_count": previous.link_count,
                        "after_symlink_target": previous.symlink_target,
                    },
                )
                storage.conn.commit()
            except Exception:
                storage.conn.rollback()
                raise


def _planned_after_state(row) -> NodeState:
    from core.turn_undo.models import SnapshotAvailability  # noqa: F401  (类型提示用途)

    kind = NodeKind(row.after_kind) if row.after_kind else NodeKind.MISSING
    return NodeState(
        kind=kind,
        exists=bool(row.after_exists) if row.after_exists is not None else False,
        size=row.after_size,
        sha256=row.after_sha256,
        mode=row.after_mode,
        identity=row.after_file_identity,
        link_count=row.after_link_count,
        symlink_target=row.after_symlink_target,
        blob_sha256=row.after_blob_sha256,
    )


def _state_matches_disk(state: NodeState, canonical_path: str) -> bool:
    node = capture_node_state(canonical_path)
    if node.kind is not state.kind:
        return False
    if node.kind is NodeKind.MISSING:
        return True
    if node.size != state.size:
        return False
    if state.sha256 is not None:
        # 内容哈希是预检/中途检测的核心依据：捕获失败（None）按不一致处理，
        # 宁可拒绝也不把无法核实的外部改动当成未变化（L6）
        return node.sha256 == state.sha256
    return True


def _file_values_from_states(
    *,
    canonical_path: str,
    display_path: str,
    before: Optional[NodeState],
    after: NodeState,
    mutated_at_ms: int,
    source_kind: str,
    source_id: str,
    tool_name: str,
    tool_call_id: str,
) -> dict:
    if before is None:
        # update_after_plan 路径不使用 before 字段
        before = NodeState(kind=NodeKind.MISSING, exists=False)
    return {
        "canonical_path": canonical_path,
        "display_path": display_path,
        "before_kind": before.kind.value,
        "before_exists": 1 if before.exists else 0,
        "before_size": before.size,
        "before_sha256": before.sha256,
        "before_blob_sha256": before.blob_sha256,
        "before_mode": before.mode,
        "before_file_identity": before.identity,
        "before_link_count": before.link_count,
        "before_symlink_target": before.symlink_target,
        "after_kind": after.kind.value,
        "after_exists": 1 if after.exists else 0,
        "after_size": after.size,
        "after_sha256": after.sha256,
        "after_blob_sha256": after.blob_sha256,
        "after_mode": after.mode,
        "after_file_identity": after.identity,
        "after_link_count": after.link_count,
        "after_symlink_target": after.symlink_target,
        "diff_kind": "text",
        "mutated_at_ms": mutated_at_ms,
        "source_kind": source_kind,
        "source_id": source_id,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
    }


def _mark_unavailable(
    storage: TurnUndoStorage,
    ctx: RecorderContext,
    *,
    reason: str,
    code: str = UndoErrorCode.CHECKPOINT_UNAVAILABLE,
) -> None:
    try:
        checkpoint = storage.get_checkpoint(ctx.checkpoint_id)
        if checkpoint.status_enum is TurnUndoStatus.RECORDING:
            storage.transition_checkpoint(
                ctx.checkpoint_id,
                TurnUndoStatus.UNAVAILABLE,
                unavailable_code=code,
                last_error_code=code,
                last_error_message=reason,
                completed_at_ms=now_ms(),
            )
    except TurnUndoError:
        raise
    except Exception as exc:  # pragma: no cover - 状态推进失败必须暴露
        raise TurnUndoError(
            UndoErrorCode.IO_ERROR,
            f"标记检查点不可撤销失败: {exc}",
        ) from exc


def verify_after_mutation(handle: MutationHandle) -> NodeState:
    """写入后立即验证实际状态与计划 after 精确一致；失败显式报错。"""
    ctx = _require_active_context(need_owner=False)
    if handle.checkpoint_id != ctx.checkpoint_id:
        raise TurnUndoError(
            UndoErrorCode.OPERATION_SCOPE_MISMATCH,
            "写入句柄不属于当前检查点",
        )
    storage = _storage_for_ctx()
    checkpoint = storage.get_checkpoint(ctx.checkpoint_id)
    if checkpoint.status_enum is not TurnUndoStatus.RECORDING:
        raise TurnUndoError(
            UndoErrorCode.CHECKPOINT_CLOSED,
            f"检查点已封闭（{checkpoint.status_enum.value}），无法验证写入",
        )

    canonical_path = handle.canonical_path
    actual = capture_node_state(canonical_path)
    planned = handle.planned_after

    def _fail(reason: str):
        # 单文件验证失败：该文件退出撤销范围，回合其余写入继续可用。
        with _record_lock:
            with storage._manager._lock:
                cursor = storage.conn.cursor()
                try:
                    storage.set_file_undo_result_locked(
                        cursor,
                        file_id=handle.file_id,
                        undo_result=UNDO_RESULT_SKIPPED,
                        undo_error_code=UndoErrorCode.FILE_CONFLICT,
                    )
                    storage.conn.commit()
                except Exception:
                    storage.conn.rollback()
                    raise
        logger.warning(
            "按回合撤销：写入验证失败，文件退出撤销范围: %s (%s)",
            canonical_path, reason,
        )
        _release_path_claim(ctx, canonical_path)
        raise TurnUndoError(
            UndoErrorCode.FILE_CONFLICT,
            f"写入后实际状态与计划不一致，该文件已退出本回合撤销范围: {canonical_path}",
            detail={
                "reason": reason,
                "file": canonical_path,
                "undo_result": UNDO_RESULT_SKIPPED,
            },
        )

    if planned.kind is NodeKind.MISSING:
        if actual.kind is not NodeKind.MISSING:
            _fail("计划删除的路径仍然存在")
    else:
        if actual.kind is not NodeKind.REGULAR_FILE:
            _fail(f"计划写入普通文件，实际为 {actual.kind.value}")
        if actual.link_count is not None and actual.link_count > 1:
            _fail(f"写入后出现 {actual.link_count} 个硬链接")
        if actual.size != planned.size:
            _fail(f"大小不一致: 计划 {planned.size} 实际 {actual.size}")
        actual_sha = _sha256_file(canonical_path)
        if actual_sha != planned.sha256:
            _fail("内容哈希不一致（可能被外部并发修改）")

    actual_state = NodeState(
        kind=actual.kind,
        exists=actual.exists,
        size=actual.size,
        sha256=planned.sha256 if planned.kind is NodeKind.REGULAR_FILE else None,
        mode=actual.mode,
        identity=actual.identity,
        link_count=actual.link_count,
        symlink_target=actual.symlink_target,
        blob_sha256=planned.blob_sha256,
    )
    with _record_lock:
        with storage._manager._lock:
            cursor = storage.conn.cursor()
            try:
                storage.update_file_verified_locked(
                    cursor,
                    file_id=handle.file_id,
                    actual={
                        "after_kind": actual_state.kind.value,
                        "after_exists": 1 if actual_state.exists else 0,
                        "after_size": actual_state.size,
                        "after_sha256": actual_state.sha256,
                        "after_blob_sha256": actual_state.blob_sha256,
                        "after_mode": actual_state.mode,
                        "after_file_identity": actual_state.identity,
                        "after_link_count": actual_state.link_count,
                        "after_symlink_target": actual_state.symlink_target,
                    },
                )
                storage.conn.commit()
            except Exception:
                storage.conn.rollback()
                _mark_unavailable(
                    storage, ctx,
                    reason="验证结果落库失败，检查点不可安全撤销",
                )
                raise
    _release_path_claim(ctx, canonical_path)
    return actual_state


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CAPTURE_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def state_from_row_side(row, side: str) -> NodeState:
    """从 turn_undo_files 行的 before_*/after_* 侧构造 NodeState。"""
    if side not in ("before", "after"):
        raise TurnUndoError(
            UndoErrorCode.INVALID_INPUT,
            f"未知文件状态侧: {side!r}",
        )
    kind_value = getattr(row, f"{side}_kind")
    kind = NodeKind(kind_value) if kind_value else NodeKind.MISSING
    exists = bool(getattr(row, f"{side}_exists"))
    return NodeState(
        kind=kind,
        exists=exists,
        size=getattr(row, f"{side}_size"),
        sha256=getattr(row, f"{side}_sha256"),
        mode=getattr(row, f"{side}_mode"),
        identity=getattr(row, f"{side}_file_identity"),
        link_count=getattr(row, f"{side}_link_count"),
        symlink_target=getattr(row, f"{side}_symlink_target"),
        blob_sha256=getattr(row, f"{side}_blob_sha256"),
    )


def verify_target_state(canonical_path: str, expected_blob_sha: str, expected_mode) -> Optional[str]:
    """恢复后校验：文件内容哈希必须等于源快照；返回错误描述或 None。"""
    try:
        actual_sha = _sha256_file(canonical_path)
    except OSError as exc:
        return f"无法读取恢复结果: {exc}"
    if actual_sha != expected_blob_sha:
        return "恢复后内容与快照不一致"
    if expected_mode is not None:
        try:
            actual_mode = stat.S_IMODE(os.lstat(canonical_path).st_mode)
        except OSError:
            actual_mode = None
        if actual_mode is not None and actual_mode != expected_mode:
            # 权限位恢复是尽力而为语义（Windows 大多无 POSIX mode），记录但不阻断
            logger.debug(
                "恢复后权限位与快照不同（平台可能不支持）: %s %s != %s",
                canonical_path, actual_mode, expected_mode,
            )
    return None


def mark_uncovered_command_effects(tool_name: str) -> None:
    """通用终端命令：回合检查点持久化未覆盖副作用事实（非受控、不可撤销）。"""
    ctx = current_context()
    if ctx is None:
        return
    if tool_name != "execute_command":
        return
    storage = _storage_for_ctx()
    try:
        checkpoint = storage.get_checkpoint(ctx.checkpoint_id)
    except TurnUndoError:
        return
    if checkpoint.status_enum is not TurnUndoStatus.RECORDING:
        return
    storage.mark_uncovered_command_effects(ctx.checkpoint_id)


# ---------------------------------------------------------------------------
# 回合封闭：净 diff 与状态收敛
# ---------------------------------------------------------------------------

def compute_net_diff(
    storage: TurnUndoStorage,
    before_blob: Optional[str],
    after_blob: Optional[str],
) -> tuple[int, int, str]:
    """从最初 before 与最终 after blob 生成权威净增删统计。

    返回 (net_added, net_removed, diff_kind)；二进制文件返回 (0, 0, 'binary')。
    """
    if before_blob is None and after_blob is None:
        return 0, 0, "empty"

    def _load_text(blob_sha: Optional[str]) -> Optional[list[str]]:
        if blob_sha is None:
            return None
        try:
            raw = storage.blob_store.read_bytes(blob_sha)
        except TurnUndoError:
            raise
        try:
            return raw.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return None

    before_lines = _load_text(before_blob)
    after_lines = _load_text(after_blob)
    if before_lines is None or after_lines is None:
        return 0, 0, "binary"

    import difflib

    added = 0
    removed = 0
    for line in difflib.unified_diff(before_lines, after_lines, lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed, "text"


def close_turn(
    ctx: RecorderContext,
    *,
    unavailable_reason: Optional[str] = None,
    unavailable_code: str = UndoErrorCode.CHECKPOINT_UNAVAILABLE,
) -> TurnUndoStatus:
    """回合封闭：校验记录完整性、生成净 diff、推进检查点到终态并注销上下文。"""
    storage = _storage_for_ctx()
    checkpoint = storage.get_checkpoint(ctx.checkpoint_id)
    if checkpoint.status_enum is not TurnUndoStatus.RECORDING:
        # 已被标记 unavailable 等：只注销上下文，保持幂等。
        release_context(ctx.checkpoint_id)
        return checkpoint.status_enum

    files = storage.list_files(ctx.checkpoint_id)
    # 退出撤销范围的文件（回合内被外部修改）不参与完整性校验与净 diff；
    # 记录仍保留，供撤销预检列出 skipped 清单。
    active_files = [row for row in files if row.undo_result != UNDO_RESULT_SKIPPED]
    final_status = TurnUndoStatus.READY
    availability = SnapshotAvailability.AVAILABLE
    reason: Optional[str] = unavailable_reason

    if not files or not active_files:
        # 没有任何受控修改，或全部文件都退出了撤销范围
        final_status = TurnUndoStatus.NO_CHANGES
        availability = SnapshotAvailability.NOT_REQUIRED
    else:
        stats: dict[str, tuple[int, int, str]] = {}
        with _record_lock:
            for row in active_files:
                if row.verified_at_ms is None:
                    final_status = TurnUndoStatus.UNAVAILABLE
                    reason = reason or "存在未通过写入后验证的文件记录"
                    break
                # 快照引用完整性：blob 缺失的回合不可撤销
                for ref in (row.before_blob_sha256, row.after_blob_sha256):
                    if ref and not storage.blob_store.exists(ref):
                        final_status = TurnUndoStatus.UNAVAILABLE
                        availability = SnapshotAvailability.MISSING
                        reason = reason or "修改前/后快照缺失"
                        break
                if final_status is TurnUndoStatus.UNAVAILABLE:
                    break
                stats[row.file_id] = compute_net_diff(
                    storage, row.before_blob_sha256, row.after_blob_sha256,
                )
            if final_status is not TurnUndoStatus.UNAVAILABLE:
                with storage._manager._lock:
                    cursor = storage.conn.cursor()
                    try:
                        for file_id, (added, removed, kind) in stats.items():
                            storage.finalize_file_stats_locked(
                                cursor, file_id=file_id,
                                net_added=added, net_removed=removed,
                                diff_kind=kind,
                            )
                        storage.conn.commit()
                    except Exception:
                        storage.conn.rollback()
                        raise

    storage.transition_checkpoint(
        ctx.checkpoint_id,
        final_status,
        unavailable_code=unavailable_code if final_status is TurnUndoStatus.UNAVAILABLE else None,
        last_error_code=unavailable_code if final_status is TurnUndoStatus.UNAVAILABLE else None,
        last_error_message=reason if final_status is TurnUndoStatus.UNAVAILABLE else None,
        completed_at_ms=now_ms(),
        availability=availability,
    )
    release_context(ctx.checkpoint_id)
    return final_status
