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
    "multi_edit",
    "fetch_image",
    "hunyuan_generate_image",
    "tokenrhythm_generate_image",
    "tokenrhythm_edit_image",
    "novel_init",
    "novel_track",
    "office_write",
})


def validate_recorder_coverage(registry) -> list[str]:
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


def begin_turn(
    *,
    session_id: int,
    turn_id: str,
    workspace_root: str,
    owner_kind: OwnerKind = OwnerKind.FOREGROUND_TURN,
    parent_checkpoint_id: Optional[str] = None,
    description: str = "",
):
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
    if owner_kind == OwnerKind.BACKGROUND_TASK:
        coordinator.register_background_turn(root, turn_id)
    else:
        coordinator.register_active_turn(root, turn_id)
    return checkpoint, ctx


def finish_turn(ctx, *, aborted: bool = False, reason: str = "") -> TurnUndoStatus:
    storage = get_default_storage()
    coordinator = _coordinator_mod.get_coordinator()
    try:
        if aborted:
            status = _recorder.close_turn(ctx, unavailable_reason=reason or "回合异常终止")
        else:
            status = _recorder.close_turn(ctx)
    finally:
        coordinator.unregister_active_turn(ctx.workspace_root, ctx.turn_id)
        coordinator.unregister_background_turn(ctx.workspace_root, ctx.turn_id)
    try:
        storage.expire_retained_checkpoints(ctx.session_id)
    except TurnUndoError:
        raise
    except Exception as exc:
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
    storage = get_default_storage()
    value = str(turn_id or "").strip()
    if not value:
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
