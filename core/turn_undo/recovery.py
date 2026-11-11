"""星瑶按回合撤销：崩溃中断检测、路径冻结与显式恢复。

规格 13/24：
- 启动扫描把未终态撤销操作按确定性收敛（不自动重放任何文件写入）：
  previewed → cancelled（令牌跨重启已失效）、preparing/prepared → failed
  （未触碰目标）、applying/verifying → interrupted（磁盘状态不确定）。
- interrupted 的检查点冻结全部影响路径（含父/子包含关系），且该工作区的
  通用终端命令也被拒绝。
- 用户显式选择 continue_revert（完成剩余撤销）或 cancel_revert（把已处理
  路径恢复到点击撤销之前）；两种恢复都先预检并使用独立一次性令牌，每次
  确认建立带 parent_operation_id 的独立操作，成功后原子解决父操作。
- 文件已进入事务未知状态时进入 recovery_required，不猜测写入。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from core.turn_undo import coordinator as coordinator_mod
from core.turn_undo import recorder as recorder_mod
from core.turn_undo.transaction import (
    _operation_dir,
    _STAGE_PREFIX,
    fingerprint_line,
)
from core.turn_undo.models import (
    FileStageStatus,
    NodeKind,
    OperationKind,
    OperationStatus,
    RecoveryAction,
    RecoveryPreview,
    RecoveryResult,
    RollbackKind,
    TurnUndoError,
    TurnUndoStatus,
    UndoErrorCode,
)
from core.turn_undo.storage import TurnUndoStorage, now_ms
from core.turn_undo.storage import _default_data_dir

import secrets


# ---------------------------------------------------------------------------
# 启动扫描
# ---------------------------------------------------------------------------

def scan_and_mark_interrupted_on_startup(
    storage: TurnUndoStorage,
    coordinator: coordinator_mod.WorkspaceCoordinator,
) -> list[dict]:
    """启动恢复：仅收敛状态与冻结路径，绝不重放文件写入。"""
    report: list[dict] = []
    for op in storage.list_nonterminal_operations():
        status = op.status_enum
        try:
            checkpoint = storage.get_checkpoint(op.checkpoint_id)
        except TurnUndoError:
            continue
        if status is OperationStatus.PREVIEWED:
            # 从未进入执行段：令牌跨 boot 已死，显式取消
            storage.transition_operation(
                op.operation_id, OperationStatus.CANCELLED,
                last_error_code=UndoErrorCode.TOKEN_EXPIRED,
                last_error_message="服务重启导致预检令牌失效",
                finished_at_ms=now_ms(),
            )
            report.append({
                "operation_id": op.operation_id,
                "checkpoint_id": op.checkpoint_id,
                "from_status": status.value,
                "to_status": OperationStatus.CANCELLED.value,
            })
            continue
        if status in (OperationStatus.PREPARING, OperationStatus.PREPARED):
            # CAS 已提交但未触碰任何目标：确定性 failed
            storage.transition_operation(
                op.operation_id, OperationStatus.FAILED,
                last_error_code=UndoErrorCode.REVERT_INTERRUPTED,
                last_error_message="服务在阶段化阶段中断，未触碰目标文件",
                finished_at_ms=now_ms(),
            )
            if checkpoint.status_enum is TurnUndoStatus.REVERTING:
                storage.transition_checkpoint(
                    checkpoint.checkpoint_id, TurnUndoStatus.FAILED,
                    last_error_code=UndoErrorCode.REVERT_INTERRUPTED,
                    last_error_message="撤销在阶段化阶段中断，未触碰目标文件",
                )
            report.append({
                "operation_id": op.operation_id,
                "checkpoint_id": op.checkpoint_id,
                "from_status": status.value,
                "to_status": OperationStatus.FAILED.value,
            })
            continue
        # APPLYING / VERIFYING：磁盘状态不确定 → interrupted + 冻结
        storage.transition_operation(
            op.operation_id, OperationStatus.INTERRUPTED,
            last_error_code=UndoErrorCode.REVERT_INTERRUPTED,
            last_error_message="服务在应用阶段中断，磁盘状态可能部分恢复",
        )
        if checkpoint.status_enum is TurnUndoStatus.REVERTING:
            files = storage.list_files(checkpoint.checkpoint_id)
            storage.transition_checkpoint(
                checkpoint.checkpoint_id, TurnUndoStatus.INTERRUPTED,
                last_error_code=UndoErrorCode.REVERT_INTERRUPTED,
                last_error_message="撤销事务中断，影响路径已冻结，等待用户显式恢复",
            )
            coordinator.freeze_paths(
                checkpoint.workspace_root,
                [row.canonical_path for row in files],
            )
        report.append({
            "operation_id": op.operation_id,
            "checkpoint_id": op.checkpoint_id,
            "from_status": status.value,
            "to_status": OperationStatus.INTERRUPTED.value,
        })

    # 重启重建：已处于 interrupted / recovering 的检查点恢复冻结
    # （协调器是进程内状态，崩溃后必须按持久化事实重新冻结，规格 24）
    for checkpoint in storage.list_checkpoints_by_status((
        TurnUndoStatus.INTERRUPTED, TurnUndoStatus.RECOVERING,
    )):
        files = storage.list_files(checkpoint.checkpoint_id)
        coordinator.freeze_paths(
            checkpoint.workspace_root,
            [row.canonical_path for row in files],
        )
    # L4：清理崩溃遗留的同目录阶段文件（含撤销前内容明文，不能留在工作区）
    from core.turn_undo.transaction import _STAGE_PREFIX as _stage_prefix
    import glob as _glob

    for checkpoint in storage.list_checkpoints_by_status(tuple(TurnUndoStatus)):
        for row in storage.list_files(checkpoint.checkpoint_id):
            try:
                parent = os.path.dirname(row.canonical_path)
                for leftover in _glob.glob(
                    os.path.join(parent, _stage_prefix + "*")
                ):
                    try:
                        os.unlink(leftover)
                    except OSError:
                        pass
            except OSError:
                pass
    # 崩溃残留的 recording 检查点：进程重启后不可能再有回合在写它们。
    # 超过阈值仍为 recording 的一律收敛为 unavailable，否则该工作区的
    # 撤销/恢复会被活跃登记永久阻塞为 workspace_busy（M4 的另一半）。
    stale_threshold_ms = 6 * 60 * 60 * 1000
    now = now_ms()
    for checkpoint in storage.list_checkpoints_by_status((TurnUndoStatus.RECORDING,)):
        started = checkpoint.started_at_ms or 0
        if started and now - started < stale_threshold_ms:
            continue
        try:
            storage.transition_checkpoint(
                checkpoint.checkpoint_id,
                TurnUndoStatus.UNAVAILABLE,
                unavailable_code="stale_recording",
                last_error_code="stale_recording",
                last_error_message="回合异常中断后残留的 recording 检查点，已回收",
                completed_at_ms=now,
            )
        except TurnUndoError:
            continue
        report.append({
            "checkpoint_id": checkpoint.checkpoint_id,
            "from_status": TurnUndoStatus.RECORDING.value,
            "to_status": TurnUndoStatus.UNAVAILABLE.value,
            "reason": "stale_recording",
        })
    return report


# ---------------------------------------------------------------------------
# 恢复预检
# ---------------------------------------------------------------------------

def _require_session_scope(storage: TurnUndoStorage, session_id: int, operation_id: str):
    operation = storage.get_operation(operation_id)
    checkpoint = storage.get_checkpoint(operation.checkpoint_id)
    if checkpoint.session_id != session_id:
        raise TurnUndoError(
            UndoErrorCode.OPERATION_SCOPE_MISMATCH,
            "恢复操作不属于该会话",
        )
    return operation, checkpoint


def _classify_recovery_files(storage: TurnUndoStorage, operation) -> tuple[list, list, list]:
    """把操作文件分为 (processed_ok, remaining, unknown)。"""
    op_files = storage.list_operation_files(operation.operation_id)
    rows = {row.file_id: row for row in storage.list_files(operation.checkpoint_id)}
    processed: list = []
    remaining: list = []
    unknown: list = []
    for op_file in op_files:
        row = rows.get(op_file.file_id)
        if row is None:
            unknown.append((op_file, None, "文件记录缺失"))
            continue
        actual = recorder_mod.capture_node_state(row.canonical_path)
        if op_file.apply_status == FileStageStatus.APPLIED.value:
            # 已处理文件：验证撤销结果仍在位
            expected_blob = row.before_blob_sha256
            if op_file.rollback_kind == RollbackKind.DELETE_CREATED.value:
                ok = actual.kind is NodeKind.MISSING
            else:
                ok = (
                    actual.kind is NodeKind.REGULAR_FILE
                    and actual.sha256 == expected_blob
                )
            if ok:
                processed.append((op_file, row, None))
            else:
                unknown.append((op_file, row, "已处理文件的撤销结果不在位"))
        elif op_file.apply_status == FileStageStatus.PENDING.value:
            expected = recorder_mod.state_from_row_side(row, "after")
            if actual.kind is expected.kind and (
                expected.kind is NodeKind.MISSING
                or actual.sha256 == expected.sha256
            ):
                remaining.append((op_file, row, None))
            else:
                unknown.append((op_file, row, "未处理文件与回合结束状态不一致"))
        else:
            unknown.append((op_file, row, "应用状态不明确"))
    return processed, remaining, unknown


def preview_recovery(
    storage: TurnUndoStorage,
    coordinator: coordinator_mod.WorkspaceCoordinator,
    *,
    session_id: int,
    operation_id: str,
    action: RecoveryAction,
    token_ttl_ms: int | None = None,
) -> tuple[RecoveryPreview, str]:
    """中断恢复预检：分类已处理/剩余/未知文件，签发独立一次性令牌。"""
    if not isinstance(action, RecoveryAction):
        raise TurnUndoError(
            UndoErrorCode.INVALID_INPUT,
            f"action 必须是 RecoveryAction: {action!r}",
        )
    operation, checkpoint = _require_session_scope(storage, session_id, operation_id)
    if operation.status_enum not in (
        OperationStatus.INTERRUPTED, OperationStatus.RECOVERY_REQUIRED,
    ):
        raise TurnUndoError(
            UndoErrorCode.OPERATION_SCOPE_MISMATCH,
            f"撤销操作不处于可恢复状态（{operation.status_enum.value}）",
        )
    if operation.kind_enum is not OperationKind.REVERT:
        raise TurnUndoError(
            UndoErrorCode.OPERATION_SCOPE_MISMATCH,
            "只有 revert 操作可以启动恢复",
        )

    processed, remaining, unknown = _classify_recovery_files(storage, operation)

    if unknown:
        # 文件已进入事务未知状态：recovery_required，不直接写入
        if operation.status_enum is OperationStatus.INTERRUPTED:
            storage.transition_operation(
                operation.operation_id, OperationStatus.RECOVERY_REQUIRED,
                last_error_code=UndoErrorCode.RECOVERY_REQUIRED,
                last_error_message="；".join(u[2] for u in unknown[:3]),
            )
        preview = RecoveryPreview(
            operation_id=operation.operation_id,
            checkpoint_id=checkpoint.checkpoint_id,
            action=action,
            processed_files=tuple(
                storage.get_file(opf.file_id).display_path for opf, _, _ in processed
            ),
            remaining_files=tuple(
                storage.get_file(opf.file_id).display_path for opf, _, _ in remaining
            ),
            unknown_files=tuple(
                storage.get_file(opf.file_id).display_path for opf, _, _ in unknown
            ),
            recovery_required=True,
        )
        return preview, ""

    # 独立恢复操作 + 一次性令牌（每次确认新建，不覆盖原中断行）
    child_kind = (
        OperationKind.CONTINUE_REVERT
        if action is RecoveryAction.CONTINUE_REVERT
        else OperationKind.CANCEL_REVERT
    )
    child = storage.create_operation(
        checkpoint_id=checkpoint.checkpoint_id,
        kind=child_kind,
        parent_operation_id=operation.operation_id,
        preview_version=checkpoint.record_version,
    )
    # 令牌绑定恢复动作将触碰的文件集合指纹
    pairs = []
    for opf, row, _ in (
        (processed + remaining) if action is RecoveryAction.CANCEL_REVERT else remaining
    ):
        pairs.append((row.canonical_path, recorder_mod.capture_node_state(row.canonical_path)))
    token = secrets.token_urlsafe(32)
    storage.create_confirmation(
        operation_id=child.operation_id,
        checkpoint_id=checkpoint.checkpoint_id,
        record_version=checkpoint.record_version,
        workspace_identity=checkpoint.workspace_root,
        fingerprint_set_sha256=_recovery_fingerprint_set(pairs),
        token=token,
        ttl_ms=token_ttl_ms or 5 * 60 * 1000,
    )
    preview = RecoveryPreview(
        operation_id=operation.operation_id,
        checkpoint_id=checkpoint.checkpoint_id,
        action=action,
        processed_files=tuple(
            storage.get_file(opf.file_id).display_path for opf, _, _ in processed
        ),
        remaining_files=tuple(
            storage.get_file(opf.file_id).display_path for opf, _, _ in remaining
        ),
        unknown_files=(),
        recovery_required=False,
        child_operation_id=child.operation_id,
    )
    return preview, token


def _recovery_fingerprint_set(pairs) -> str:
    digest = hashlib.sha256()
    for canonical_path, state in sorted(pairs, key=lambda item: item[0]):
        digest.update(fingerprint_line(canonical_path, state).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 恢复执行
# ---------------------------------------------------------------------------

def execute_recovery(
    storage: TurnUndoStorage,
    coordinator: coordinator_mod.WorkspaceCoordinator,
    *,
    session_id: int,
    operation_id: str,
    action: RecoveryAction,
    preview_token: str,
) -> RecoveryResult:
    """执行显式恢复：continue_revert 完成剩余撤销；cancel_revert 回到撤销前。"""
    if not isinstance(action, RecoveryAction):
        raise TurnUndoError(
            UndoErrorCode.INVALID_INPUT,
            f"action 必须是 RecoveryAction: {action!r}",
        )
    parent_op, checkpoint = _require_session_scope(storage, session_id, operation_id)
    confirmation = storage.find_confirmation_by_token(preview_token or "")
    if confirmation is None or confirmation["checkpoint_id"] != checkpoint.checkpoint_id:
        raise TurnUndoError(
            UndoErrorCode.TOKEN_SCOPE_MISMATCH,
            "恢复确认令牌与目标检查点不匹配",
        )
    child_op = storage.get_operation(confirmation["operation_id"])
    if child_op.parent_operation_id != parent_op.operation_id:
        raise TurnUndoError(
            UndoErrorCode.TOKEN_SCOPE_MISMATCH,
            "恢复令牌与父操作不匹配",
        )
    expected_kind = (
        OperationKind.CONTINUE_REVERT
        if action is RecoveryAction.CONTINUE_REVERT
        else OperationKind.CANCEL_REVERT
    )
    if child_op.kind_enum is not expected_kind:
        raise TurnUndoError(
            UndoErrorCode.TOKEN_SCOPE_MISMATCH,
            "恢复令牌与恢复动作不匹配",
        )
    if child_op.status_enum is not OperationStatus.PREVIEWED:
        raise TurnUndoError(
            UndoErrorCode.TOKEN_ALREADY_USED,
            f"恢复操作已不可执行（{child_op.status_enum.value}），请重新预检",
        )
    if parent_op.status_enum not in (
        OperationStatus.INTERRUPTED, OperationStatus.RECOVERY_REQUIRED,
    ):
        raise TurnUndoError(
            UndoErrorCode.OPERATION_SCOPE_MISMATCH,
            f"父操作不处于可恢复状态（{parent_op.status_enum.value}）",
        )
    if parent_op.status_enum is OperationStatus.RECOVERY_REQUIRED:
        raise TurnUndoError(
            UndoErrorCode.RECOVERY_REQUIRED,
            "文件处于事务未知状态，需要按恢复详情人工处理，拒绝自动写入",
        )

    root = checkpoint.workspace_root
    with coordinator.workspace_lock(root):
        if coordinator.has_active_turn(root):
            raise TurnUndoError(
                UndoErrorCode.WORKSPACE_BUSY,
                "工作区有正在进行的回合或后台写入任务，请稍后恢复",
            )
        if coordinator.active_undo_id(root) is not None:
            raise TurnUndoError(
                UndoErrorCode.WORKSPACE_BUSY,
                "工作区已有撤销/恢复事务正在进行",
            )
        current_checkpoint = storage.get_checkpoint(checkpoint.checkpoint_id)
        if current_checkpoint.status_enum is not TurnUndoStatus.INTERRUPTED:
            raise TurnUndoError(
                UndoErrorCode.INVALID_STATE_TRANSITION,
                f"检查点状态为 {current_checkpoint.status_enum.value}，无法恢复",
            )

        processed, remaining, unknown = _classify_recovery_files(storage, parent_op)
        if unknown:
            if parent_op.status_enum is OperationStatus.INTERRUPTED:
                storage.transition_operation(
                    parent_op.operation_id, OperationStatus.RECOVERY_REQUIRED,
                    last_error_code=UndoErrorCode.RECOVERY_REQUIRED,
                    last_error_message="执行前发现未知文件状态",
                )
            raise TurnUndoError(
                UndoErrorCode.RECOVERY_REQUIRED,
                "执行前发现未知文件状态，需要人工处理",
            )
        # 锁内指纹复核：恢复动作触碰的文件必须与预检一致
        if action is RecoveryAction.CONTINUE_REVERT:
            target_entries = remaining
        else:
            target_entries = processed + remaining
        pairs = [
            (row.canonical_path, recorder_mod.capture_node_state(row.canonical_path))
            for _, row, _ in target_entries
        ]
        if _recovery_fingerprint_set(pairs) != confirmation["fingerprint_set_sha256"]:
            raise TurnUndoError(
                UndoErrorCode.FILE_CONFLICT,
                "恢复执行前发现文件与预检结果不一致，本次没有修改任何文件",
            )

        # 短事务：消费令牌 + child preparing + 检查点 recovering
        with storage._manager._lock:
            cursor = storage.conn.cursor()
            try:
                storage.consume_confirmation_locked(
                    cursor, token_hash=confirmation["token_hash"],
                )
                cursor.execute(
                    "UPDATE turn_undo_operations SET status = ?, started_at_ms = ?, "
                    "confirmed_at_ms = ? WHERE operation_id = ? AND status = ?",
                    (OperationStatus.PREPARING.value, now_ms(), now_ms(),
                     child_op.operation_id, OperationStatus.PREVIEWED.value),
                )
                if cursor.rowcount != 1:
                    raise TurnUndoError(
                        UndoErrorCode.TOKEN_ALREADY_USED,
                        "恢复预检已被并发消费",
                    )
                cursor.execute(
                    "UPDATE turn_undo_checkpoints SET status = ?, "
                    "record_version = record_version + 1 "
                    "WHERE checkpoint_id = ? AND status = ?",
                    (TurnUndoStatus.RECOVERING.value, checkpoint.checkpoint_id,
                     TurnUndoStatus.INTERRUPTED.value),
                )
                if cursor.rowcount != 1:
                    raise TurnUndoError(
                        UndoErrorCode.INVALID_STATE_TRANSITION,
                        "检查点不处于 interrupted 状态",
                    )
                storage.conn.commit()
            except Exception:
                storage.conn.rollback()
                raise

        coordinator.register_undo(root, child_op.operation_id)
        try:
            return _run_recovery_transaction(
                storage, coordinator, checkpoint, parent_op, child_op,
                action, processed, remaining,
            )
        finally:
            coordinator.unregister_undo(root, child_op.operation_id)


def _run_recovery_transaction(
    storage, coordinator, checkpoint, parent_op, child_op, action,
    processed, remaining,
) -> RecoveryResult:
    child_id = child_op.operation_id

    def _interrupt(error: TurnUndoError) -> TurnUndoError:
        storage.transition_operation(
            child_id, OperationStatus.INTERRUPTED,
            last_error_code=error.code, last_error_message=error.message,
        )
        storage.transition_checkpoint(
            checkpoint.checkpoint_id, TurnUndoStatus.INTERRUPTED,
            last_error_code=error.code, last_error_message=error.message,
        )
        coordinator.freeze_paths(
            checkpoint.workspace_root,
            [row.canonical_path for _, row, _ in processed + remaining],
        )
        return error

    try:
        # 严格按状态机推进：preparing → prepared → applying → verifying → committed
        storage.transition_operation(child_id, OperationStatus.PREPARED)
        storage.transition_operation(child_id, OperationStatus.APPLYING)
        if action is RecoveryAction.CONTINUE_REVERT:
            affected = _apply_continue(
                storage, checkpoint, child_id, remaining,
            )
        else:
            affected = _apply_cancel(
                storage, checkpoint, child_id, processed, remaining,
            )
        storage.transition_operation(child_id, OperationStatus.VERIFYING)
    except TurnUndoError as error:
        raise _interrupt(error) from error

    # 同事务：child committed + parent resolved + 检查点终态 + （continue 时）纠正记录
    now = now_ms()
    with storage._manager._lock:
        cursor = storage.conn.cursor()
        try:
            cursor.execute(
                "UPDATE turn_undo_operations SET status = ?, finished_at_ms = ? "
                "WHERE operation_id = ? AND status = ?",
                (OperationStatus.COMMITTED.value, now, child_id,
                 OperationStatus.VERIFYING.value),
            )
            if cursor.rowcount != 1:
                raise TurnUndoError(
                    UndoErrorCode.INVALID_STATE_TRANSITION,
                    "恢复子操作状态并发变化",
                )
            parent_target = (
                OperationStatus.COMMITTED
                if action is RecoveryAction.CONTINUE_REVERT
                else OperationStatus.COMPENSATED
            )
            storage.resolve_interrupted_parent_locked(
                cursor,
                parent_operation_id=parent_op.operation_id,
                resolved_by_operation_id=child_id,
                target=parent_target,
            )
            if action is RecoveryAction.CONTINUE_REVERT:
                cursor.execute(
                    "UPDATE turn_undo_checkpoints SET status = ?, reverted_at_ms = ?, "
                    "record_version = record_version + 1 "
                    "WHERE checkpoint_id = ? AND status = ?",
                    (TurnUndoStatus.REVERTED.value, now,
                     checkpoint.checkpoint_id, TurnUndoStatus.RECOVERING.value),
                )
                if cursor.rowcount != 1:
                    raise TurnUndoError(
                        UndoErrorCode.INVALID_STATE_TRANSITION,
                        "检查点状态并发变化",
                    )
                import json as _json

                cursor.execute(
                    "INSERT OR IGNORE INTO workspace_corrections "
                    "(correction_id, workspace_root, session_id, turn_id, operation_id, "
                    "files_summary, affected_count, has_uncovered_command_effects, "
                    "subsequent_completed_turns, created_at_ms) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"corr-{parent_op.operation_id}",
                        checkpoint.workspace_root, checkpoint.session_id,
                        checkpoint.turn_id, parent_op.operation_id,
                        _json.dumps(affected, ensure_ascii=False), len(affected),
                        1 if checkpoint.has_uncovered_command_effects else 0,
                        0, now,
                    ),
                )
            else:
                cursor.execute(
                    "UPDATE turn_undo_checkpoints SET status = ?, "
                    "record_version = record_version + 1 "
                    "WHERE checkpoint_id = ? AND status = ?",
                    (TurnUndoStatus.READY.value, checkpoint.checkpoint_id,
                     TurnUndoStatus.RECOVERING.value),
                )
                if cursor.rowcount != 1:
                    raise TurnUndoError(
                        UndoErrorCode.INVALID_STATE_TRANSITION,
                        "检查点状态并发变化",
                    )
            storage.conn.commit()
        except Exception:
            storage.conn.rollback()
            raise

    coordinator.unfreeze_paths(
        checkpoint.workspace_root,
        [row.canonical_path for _, row, _ in processed + remaining],
    )
    # 成功收敛后清理事务隔离目录（中断时不清理，恢复需要其中的隔离文件）
    for done_id in (child_id, parent_op.operation_id):
        shutil.rmtree(
            _default_data_dir() / "undo_transactions" / done_id,
            ignore_errors=True,
        )
    return RecoveryResult(
        operation_id=child_id,
        parent_operation_id=parent_op.operation_id,
        action=action,
        status=OperationStatus.COMMITTED,
        checkpoint_status=(
            TurnUndoStatus.REVERTED
            if action is RecoveryAction.CONTINUE_REVERT
            else TurnUndoStatus.READY
        ),
        affected_files=tuple(affected),
    )


def _apply_continue(storage, checkpoint, child_id, remaining) -> list[str]:
    """完成剩余撤销：与主事务相同的逐文件应用语义。"""
    isolation_dir = _operation_dir(storage, child_id)
    affected: list[str] = []
    for ordinal, (op_file, row, _) in enumerate(remaining):
        action = RollbackKind(op_file.rollback_kind)
        if action is RollbackKind.DELETE_CREATED:
            isolated = isolation_dir / f"{child_id}-{ordinal}.isolated"
            if isolated.exists():
                isolated.unlink()
            shutil.move(row.canonical_path, str(isolated))
            after = recorder_mod.capture_node_state(row.canonical_path)
            if after.kind is not NodeKind.MISSING:
                raise TurnUndoError(
                    UndoErrorCode.FILE_CONFLICT,
                    f"新建文件隔离后仍存在: {row.display_path}",
                )
        else:
            if not row.before_blob_sha256:
                raise TurnUndoError(
                    UndoErrorCode.SNAPSHOT_MISSING,
                    f"缺少修改前快照: {row.display_path}",
                )
            data = storage.blob_store.read_bytes(row.before_blob_sha256)
            target = Path(row.canonical_path)
            stage = target.parent / f"{_STAGE_PREFIX}{child_id}-{ordinal}{target.suffix}"
            try:
                with open(stage, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                if row.before_mode is not None:
                    try:
                        os.chmod(stage, row.before_mode)
                    except OSError:
                        pass
                os.replace(stage, row.canonical_path)
            except OSError as exc:
                stage.unlink(missing_ok=True)
                raise TurnUndoError(
                    UndoErrorCode.IO_ERROR,
                    f"恢复文件失败 {row.display_path}: {exc}",
                ) from exc
            problem = recorder_mod.verify_target_state(
                row.canonical_path, row.before_blob_sha256, row.before_mode,
            )
            if problem is not None:
                raise TurnUndoError(
                    UndoErrorCode.FILE_CONFLICT,
                    f"恢复后校验失败 {row.display_path}: {problem}",
                )
        affected.append(row.display_path)
    return affected


def _refresh_after_identity(storage, row) -> None:
    """自身恢复后的身份刷新：内容已验证一致，File ID 变化不是外部改动。"""
    actual = recorder_mod.capture_node_state(row.canonical_path)
    with storage._manager._lock:
        cursor = storage.conn.cursor()
        try:
            storage.refresh_after_identity_locked(
                cursor, file_id=row.file_id,
                identity=actual.identity or "",
                mode=actual.mode,
                link_count=actual.link_count,
            )
            storage.conn.commit()
        except Exception:
            storage.conn.rollback()
            raise


def _apply_cancel(storage, checkpoint, child_id, processed, remaining) -> list[str]:
    """取消撤销：把已处理路径恢复到用户点击撤销之前的状态。"""
    isolation_dir = _operation_dir(storage, child_id)
    affected: list[str] = []
    for ordinal, (op_file, row, _) in enumerate(processed):
        action = RollbackKind(op_file.rollback_kind)
        if action is RollbackKind.DELETE_CREATED:
            # 撤销把新建文件移进了父操作隔离区（文件名 = 父操作文件 ID）
            isolated = (
                _default_data_dir() / "undo_transactions" / op_file.operation_id
                / f"{op_file.operation_file_id}.isolated"
            )
            if not isolated.is_file():
                raise TurnUndoError(
                    UndoErrorCode.SNAPSHOT_MISSING,
                    f"隔离文件缺失，无法取消撤销: {row.display_path}",
                )
            target = Path(row.canonical_path)
            if target.exists():
                raise TurnUndoError(
                    UndoErrorCode.FILE_CONFLICT,
                    f"取消撤销时目标路径已被占用: {row.display_path}",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(isolated), str(target))
            _refresh_after_identity(storage, row)
        elif action is RollbackKind.RESTORE_DELETED:
            # 撤销重建了被删除的文件 → 再次移除（移入隔离区，不直接不可恢复删除）
            isolated = isolation_dir / f"{child_id}-{ordinal}.restored-deleted"
            if isolated.exists():
                isolated.unlink()
            shutil.move(row.canonical_path, str(isolated))
        else:
            # restore_content：恢复点击撤销之前的内容（after blob）
            rollback_blob = op_file.rollback_blob_sha256
            if not rollback_blob:
                raise TurnUndoError(
                    UndoErrorCode.SNAPSHOT_MISSING,
                    f"缺少撤销前回滚快照: {row.display_path}",
                )
            data = storage.blob_store.read_bytes(rollback_blob)
            target = Path(row.canonical_path)
            stage = target.parent / f"{_STAGE_PREFIX}{child_id}-{ordinal}{target.suffix}"
            try:
                with open(stage, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                if op_file.rollback_mode is not None:
                    try:
                        os.chmod(stage, op_file.rollback_mode)
                    except OSError:
                        pass
                os.replace(stage, row.canonical_path)
            except OSError as exc:
                stage.unlink(missing_ok=True)
                raise TurnUndoError(
                    UndoErrorCode.IO_ERROR,
                    f"回滚文件失败 {row.display_path}: {exc}",
                ) from exc
            problem = recorder_mod.verify_target_state(
                row.canonical_path, rollback_blob, op_file.rollback_mode,
            )
            if problem is not None:
                raise TurnUndoError(
                    UndoErrorCode.FILE_CONFLICT,
                    f"回滚后校验失败 {row.display_path}: {problem}",
                )
            _refresh_after_identity(storage, row)
        affected.append(row.display_path)
    return affected
