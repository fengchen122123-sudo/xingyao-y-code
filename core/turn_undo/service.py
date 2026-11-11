"""星瑶按回合撤销：GUI、CLI、模型工具共用的编排入口（TurnUndoService）。

本模块提供进程级存储注册、默认存储访问与回合生命周期编排原语；
preview/execute/recovery 编排在 transaction.py / recovery.py 完成后由
service 暴露为规格 19 的 API。
"""

from __future__ import annotations

import threading
import uuid
from typing import Optional

from core.turn_undo import coordinator as _coordinator_mod
from core.turn_undo import recorder as _recorder
from core.turn_undo.models import OwnerKind, TurnUndoError, TurnUndoStatus, UndoErrorCode
from core.turn_undo.storage import TurnUndoStorage, now_ms

_default_manager = None
_default_storage: Optional[TurnUndoStorage] = None
_service_lock = threading.RLock()


def configure_default_manager(manager, *, blob_root=None) -> TurnUndoStorage:
    """应用启动时注册进程级 MemoryManager；返回绑定的存储。

    GUI（gui_bridge.ApiBridge）与 CLI（main.py）各自创建 MemoryManager 后
    调用本函数；重复调用以最后一次为准（单进程单连接语义不变）。
    启动校验：mutates_files 工具未接入记录器时立即失败（规格 9.5）。
    """
    global _default_manager, _default_storage
    with _service_lock:
        _default_manager = manager
        _default_storage = TurnUndoStorage(manager, blob_root=blob_root)
        try:
            from core.tool_registry import build_default_registry

            uncovered = validate_recorder_coverage(build_default_registry())
            if uncovered:
                raise RuntimeError(
                    "以下工具声明 mutates_files 但未接入按回合撤销记录器: "
                    + ", ".join(sorted(uncovered))
                )
        except RuntimeError:
            raise
        except Exception:
            # 注册表不可用（测试替身/嵌入方）：记录器接入校验延迟到可用时
            pass
        return _default_storage


def get_default_storage() -> TurnUndoStorage:
    """记录器与事务层使用；未配置时显式失败。"""
    with _service_lock:
        if _default_storage is None:
            raise RuntimeError(
                "turn_undo service 尚未配置：请先调用 configure_default_manager(manager)"
            )
        return _default_storage


def has_default_storage() -> bool:
    with _service_lock:
        return _default_storage is not None


# 已接入 FileMutationRecorder 的受控工具（mutates_files=True 声明者）。
# 新增声明 mutates_files 的工具时必须同步接入记录器，否则启动校验失败。
_RECORDED_FIRST_PARTY_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "fetch_image",
    "hunyuan_generate_image",     # core/hunyuan_image.py 保存点已接入
    "tokenrhythm_generate_image", # 插件 _save_image_bytes 保存点已接入
    "tokenrhythm_edit_image",     # 同上（edit_image 复用同一落盘路径）
    "novel_init",                 # core/novel_service.py _write_text 落盘点已接入
    "novel_track",                # 同上：快照重放经 _write_text 记录 before/after
    "office_write",               # office plugin _save_with_turn_undo
})


def validate_recorder_coverage(registry) -> list[str]:
    """校验 mutates_files=True 的工具均已接入记录器（规格 9.5）。

    返回未接入的工具名列表；非空即启动/测试校验失败。插件工具经
    core.hunyuan_image / tokenrhythm 插件接入记录器，同样受此约束。
    """
    uncovered = []
    try:
        specs = registry.all()
    except Exception:
        specs = []
    for spec in specs:
        if getattr(spec, "mutates_files", False) and \
                spec.name not in _RECORDED_FIRST_PARTY_TOOLS:
            uncovered.append(spec.name)
    return uncovered


# ---------------------------------------------------------------------------
# 回合生命周期编排
# ---------------------------------------------------------------------------

def begin_turn(
    *,
    session_id: int,
    turn_id: str,
    workspace_root: str,
    owner_kind: OwnerKind = OwnerKind.FOREGROUND_TURN,
    parent_checkpoint_id: Optional[str] = None,
    description: str = "",
):
    """创建持久化检查点并注册记录器上下文（绑定当前线程为 owner）。

    供 GUI/CLI 回合 worker 在"首条用户消息落库之前"调用；后台写文件任务
    在任务启动时以 owner_kind=BACKGROUND_TASK 调用。
    """
    storage = get_default_storage()
    root = _coordinator_mod.canonicalize_workspace_root(workspace_root)
    checkpoint = storage.create_checkpoint(
        session_id=session_id,
        turn_id=turn_id,
        workspace_root=root,
        owner_kind=owner_kind,
        parent_checkpoint_id=parent_checkpoint_id,
        description=description,
    )
    ctx = _recorder.begin_recording(storage, checkpoint)
    coordinator = _coordinator_mod.get_coordinator()
    # 后台任务检查点单独登记：它自身写入由 recorder._ensure_workspace_writable
    # 按 owner_kind 直接放行（不参与前台互斥），若占用前台唯一槽位，就会把同工作区
    # 的前台回合全部挡成 workspace_busy（实测一个跑 16 分钟的子代理足以让整个
    # 工作区无法写入）。单独登记只用于撤销/恢复阶段的并发判定。
    if owner_kind == OwnerKind.BACKGROUND_TASK:
        coordinator.register_background_turn(root, turn_id)
    else:
        coordinator.register_active_turn(root, turn_id)
    return checkpoint, ctx


def finish_turn(ctx, *, aborted: bool = False, reason: str = "") -> TurnUndoStatus:
    """回合封闭：净 diff、完整性校验、状态收敛、注销活跃登记。

    注销必须在 finally 中执行（M4）：close_turn 抛错（如 DB 故障）时若不注销，
    该工作区后续所有撤销/恢复会被活跃登记永久阻塞为 workspace_busy。
    """
    storage = get_default_storage()
    coordinator = _coordinator_mod.get_coordinator()
    try:
        if aborted:
            status = _recorder.close_turn(ctx, unavailable_reason=reason or "回合异常终止")
        else:
            status = _recorder.close_turn(ctx)
    finally:
        # 前台/后台两张登记表都注销；两个方法都按 turn_id 精确匹配，互不误删
        coordinator.unregister_active_turn(ctx.workspace_root, ctx.turn_id)
        coordinator.unregister_background_turn(ctx.workspace_root, ctx.turn_id)
    # 超限检查点快照过期（不触发生命周期事实，不扫描磁盘；GC 在启动扫描时执行）
    try:
        storage.expire_retained_checkpoints(ctx.session_id)
    except TurnUndoError:
        raise
    except Exception as exc:  # 保留策略失败不阻断回合收尾
        import logging

        logging.getLogger("py.warnings").warning(
            "按回合撤销保留策略执行失败: %s", exc
        )
    return status


def begin_background_task_turn(
    *,
    parent_turn_id: str,
    description: str = "",
):
    """后台写文件任务启动时调用：以独立检查点 + 独立 turn_id 开始记录。

    与父回合通过 parent_checkpoint_id 关联（同 session_id、同规范化工作区由
    存储层校验）；父检查点上下文缺失时按 turn_id 查库兜底——父回合已封闭
    （后台任务在并发队列等待期间父回合结束）也必须建立独立检查点而非裸奔
    （规格 9.2，H2）；连持久化检查点都不存在（旧协议回合）时返回 None。
    成功返回 (checkpoint, ctx)；当前线程被绑定为 owner。
    """
    parent_ctx = _recorder.find_context_by_turn(parent_turn_id)
    if parent_ctx is not None:
        checkpoint, ctx = begin_turn(
            session_id=parent_ctx.session_id,
            turn_id=uuid.uuid4().hex,
            workspace_root=parent_ctx.workspace_root,
            owner_kind=OwnerKind.BACKGROUND_TASK,
            parent_checkpoint_id=parent_ctx.checkpoint_id,
            description=description,
        )
        return checkpoint, ctx
    # 父 ctx 已注销：查库取父检查点（可能已封闭），仍建立归属明确的子检查点
    storage = get_default_storage()
    parent = storage.get_checkpoint_by_turn_global(parent_turn_id)
    if parent is None:
        return None
    checkpoint, ctx = begin_turn(
        session_id=parent.session_id,
        turn_id=uuid.uuid4().hex,
        workspace_root=parent.workspace_root,
        owner_kind=OwnerKind.BACKGROUND_TASK,
        parent_checkpoint_id=parent.checkpoint_id,
        description=description,
    )
    return checkpoint, ctx


def finish_background_task_turn(ctx, *, reason: str = "") -> Optional[TurnUndoStatus]:
    """后台任务结束：封闭其独立检查点并注销活跃登记。"""
    if ctx is None:
        return None
    return finish_turn(ctx, aborted=bool(reason), reason=reason)


# ---------------------------------------------------------------------------
# 规格 19 公共 API（GUI / CLI / 模型工具共用）
# ---------------------------------------------------------------------------

def preview_turn_undo(session_id: int, turn_id: str, *, token_ttl_ms: int | None = None):
    """预检并返回 (TurnUndoPreview, token)；冲突时 token 为空串。"""
    from core.turn_undo import transaction

    storage = get_default_storage()
    coordinator = _coordinator_mod.get_coordinator()
    return transaction.preview_revert(
        storage, coordinator, session_id=session_id, turn_id=turn_id,
        token_ttl_ms=token_ttl_ms,
    )


def execute_turn_undo(session_id: int, turn_id: str, preview_token: str):
    """执行撤销；已撤销回合返回幂等结果，不再修改文件。"""
    from core.turn_undo import transaction

    storage = get_default_storage()
    coordinator = _coordinator_mod.get_coordinator()
    checkpoint = storage.get_checkpoint_by_turn(session_id, turn_id)
    if checkpoint is None:
        raise TurnUndoError(
            UndoErrorCode.CHECKPOINT_NOT_FOUND,
            f"该会话下不存在回合 {turn_id} 的撤销检查点",
        )
    if checkpoint.status_enum is TurnUndoStatus.REVERTED:
        return transaction.idempotent_reverted_result(storage, checkpoint)
    return transaction.execute_revert(
        storage, coordinator,
        session_id=session_id, turn_id=turn_id, preview_token=preview_token,
    )


def preview_turn_undo_recovery(session_id: int, operation_id: str, action):
    from core.turn_undo import recovery
    from core.turn_undo.models import RecoveryAction

    if not isinstance(action, RecoveryAction):
        action = RecoveryAction(str(action))
    storage = get_default_storage()
    coordinator = _coordinator_mod.get_coordinator()
    return recovery.preview_recovery(
        storage, coordinator,
        session_id=session_id, operation_id=operation_id, action=action,
    )


def execute_turn_undo_recovery(session_id: int, operation_id: str, action, preview_token: str):
    from core.turn_undo import recovery
    from core.turn_undo.models import RecoveryAction

    if not isinstance(action, RecoveryAction):
        action = RecoveryAction(str(action))
    storage = get_default_storage()
    coordinator = _coordinator_mod.get_coordinator()
    return recovery.execute_recovery(
        storage, coordinator,
        session_id=session_id, operation_id=operation_id,
        action=action, preview_token=preview_token,
    )


def list_turn_undo_states(session_id: int, limit: int = 100) -> list[dict]:
    """批量返回会话检查点状态与净变更摘要（历史加载用，不含大块 diff）。"""
    storage = get_default_storage()
    return storage.list_checkpoints_for_session(session_id, limit=limit)


def get_turn_undo_status(session_id: int, turn_id: str) -> dict | None:
    storage = get_default_storage()
    checkpoint = storage.get_checkpoint_by_turn(session_id, turn_id)
    if checkpoint is None:
        return None
    files = storage.list_files(checkpoint.checkpoint_id)
    operations = storage.list_operations_for_checkpoint(checkpoint.checkpoint_id)
    last_operation_id = operations[-1].operation_id if operations else None
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "session_id": checkpoint.session_id,
        "turn_id": checkpoint.turn_id,
        "workspace_root": checkpoint.workspace_root,
        "status": checkpoint.status_enum.value,
        "snapshot_availability": checkpoint.availability_enum.value,
        "record_version": checkpoint.record_version,
        "owner_kind": checkpoint.owner_kind,
        "parent_checkpoint_id": checkpoint.parent_checkpoint_id,
        "description": checkpoint.description,
        "started_at_ms": checkpoint.started_at_ms,
        "completed_at_ms": checkpoint.completed_at_ms,
        "reverted_at_ms": checkpoint.reverted_at_ms,
        "has_uncovered_command_effects": bool(checkpoint.has_uncovered_command_effects),
        "unavailable_code": checkpoint.unavailable_code,
        "last_error_code": checkpoint.last_error_code,
        "last_error_message": checkpoint.last_error_message,
        "last_operation_id": last_operation_id,
        "files": [
            {
                "file_id": row.file_id,
                "display_path": row.display_path,
                "before_kind": row.before_kind,
                "after_kind": row.after_kind,
                "net_added": row.net_added,
                "net_removed": row.net_removed,
                "diff_kind": row.diff_kind,
            }
            for row in files
        ],
    }


_DIFF_MAX_LINES = 400


def get_turn_undo_file_diff(session_id: int, turn_id: str, file_id: str) -> dict:
    """按归属检查点的 file_id 惰性生成有上限的统一 diff；二进制只给摘要。"""
    import difflib

    storage = get_default_storage()
    checkpoint = storage.get_checkpoint_by_turn(session_id, turn_id)
    if checkpoint is None:
        raise TurnUndoError(
            UndoErrorCode.CHECKPOINT_NOT_FOUND,
            f"该会话下不存在回合 {turn_id} 的撤销检查点",
        )
    row = storage.get_file(file_id)
    if row.checkpoint_id != checkpoint.checkpoint_id:
        raise TurnUndoError(
            UndoErrorCode.OPERATION_SCOPE_MISMATCH,
            "文件记录不属于该回合检查点",
        )
    net_added, net_removed, diff_kind = _recorder.compute_net_diff(
        storage, row.before_blob_sha256, row.after_blob_sha256,
    )

    def _decode(blob_sha):
        if not blob_sha:
            return None
        try:
            return storage.blob_store.read_bytes(blob_sha).decode("utf-8").splitlines()
        except TurnUndoError:
            raise
        except UnicodeDecodeError:
            return None

    diff_text = None
    truncated = False
    before_lines = _decode(row.before_blob_sha256)
    after_lines = _decode(row.after_blob_sha256)
    if row.diff_kind == "text" and before_lines is not None and after_lines is not None:
        diff_lines = list(difflib.unified_diff(
            before_lines, after_lines,
            fromfile=f"a/{row.display_path}", tofile=f"b/{row.display_path}",
            lineterm="",
        ))
        if len(diff_lines) > _DIFF_MAX_LINES:
            diff_lines = diff_lines[:_DIFF_MAX_LINES]
            truncated = True
        diff_text = "\n".join(diff_lines)
    return {
        "file_id": row.file_id,
        "display_path": row.display_path,
        "diff_kind": row.diff_kind or diff_kind,
        "diff_text": diff_text,
        "net_added": row.net_added or net_added,
        "net_removed": row.net_removed or net_removed,
        "before_size": row.before_size,
        "after_size": row.after_size,
        "truncated": truncated,
        "undo_result": row.undo_result,
    }


def startup_recovery_scan() -> list[dict]:
    """应用启动时调用：收敛中断事务并冻结影响路径（不自动重放写入）。"""
    from core.turn_undo import recovery

    storage = get_default_storage()
    coordinator = _coordinator_mod.get_coordinator()
    report = recovery.scan_and_mark_interrupted_on_startup(storage, coordinator)
    storage.delete_stale_confirmations()
    # 保留策略与 GC 接线（M3/规格 8.1）：逐会话过期超限检查点快照，
    # 再回收已无任何有效数据库引用的内容块（中断/待恢复快照不回收）
    try:
        for session_id in storage.distinct_checkpoint_sessions():
            storage.expire_retained_checkpoints(session_id)
        storage.collect_blob_garbage()
    except Exception:
        import logging

        logging.getLogger("py.warnings").warning(
            "按回合撤销：启动 GC 执行失败", exc_info=True
        )
    return report


def resolve_session_id(session_name: str) -> int:
    return get_default_storage().resolve_session_id(session_name)


def resolve_turn_id_in_session(session_id: int, turn_id: str = "") -> str:
    """把模型/用户提供的（可能为短）turn_id 解析为本会话唯一回合。

    空值 → 最近一个 ready 前台回合；短 ID 在会话内唯一时接受，歧义显式失败。
    """
    storage = get_default_storage()
    value = str(turn_id or "").strip()
    if not value:
        # 无参数撤销最近 ready 前台回合：按 session_id 限定
        with storage._manager._lock:
            cursor = storage.conn.cursor()
            cursor.execute(
                "SELECT turn_id FROM turn_undo_checkpoints "
                "WHERE session_id = ? AND status = ? AND owner_kind = ? "
                "ORDER BY started_at_ms DESC LIMIT 1",
                (
                    session_id,
                    TurnUndoStatus.READY.value,
                    OwnerKind.FOREGROUND_TURN.value,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            raise TurnUndoError(
                UndoErrorCode.CHECKPOINT_NOT_FOUND,
                "当前会话没有可撤销的回合",
            )
        return row[0]
    checkpoint = storage.get_checkpoint_by_turn(session_id, value)
    if checkpoint is not None:
        return value
    like_pattern = (
        value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    )
    with storage._manager._lock:
        cursor = storage.conn.cursor()
        cursor.execute(
            "SELECT turn_id FROM turn_undo_checkpoints "
            "WHERE session_id = ? AND turn_id LIKE ? ESCAPE '\\' "
            "ORDER BY started_at_ms",
            (session_id, like_pattern),
        )
        matches = [r[0] for r in cursor.fetchall()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise TurnUndoError(
            UndoErrorCode.INVALID_INPUT,
            f"回合 ID 前缀 {value} 匹配到 {len(matches)} 个回合，请使用完整 ID",
        )
    raise TurnUndoError(
        UndoErrorCode.CHECKPOINT_NOT_FOUND,
        f"当前会话不存在回合: {value}",
    )


def undo_turn_permission_impact(arguments: dict) -> dict:
    """undo_turn 权限卡富化：预检并返回影响摘要（只读，不签发令牌）。

    供 CLI 权限卡与 GUI 权限事件在展示"影响摘要"时调用；服务未配置或
    预检失败时返回 {"available": False}，卡片退回通用展示。
    """
    try:
        from core import app_context

        session_name = app_context.get_thread_session() or ""
        if not session_name:
            return {"available": False, "message": "无法确定当前会话"}
        session_id = resolve_session_id(session_name)
        resolved = resolve_turn_id_in_session(session_id, str(arguments.get("turn_id") or ""))
        preview, _token = preview_turn_undo(session_id, resolved)
        conflicts = [
            {"display_path": c.display_path, "reasons": list(c.reasons)}
            for c in preview.conflicts
        ]
        return {
            "available": True,
            "turn_id": resolved,
            "short_id": resolved[:8],
            "status": preview.status.value,
            "file_count": preview.file_count,
            "restore_content_count": preview.restore_content_count,
            "delete_created_count": preview.delete_created_count,
            "restore_deleted_count": preview.restore_deleted_count,
            "created_files": list(preview.created_files),
            "has_uncovered_command_effects": preview.has_uncovered_command_effects,
            "subsequent_completed_turns": preview.subsequent_completed_turns,
            "conflicts": conflicts,
        }
    except Exception as exc:
        code = getattr(exc, "code", "")
        return {"available": False, "code": code, "message": str(exc)}

