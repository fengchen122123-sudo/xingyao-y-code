"""星瑶按回合撤销：预检、一次性确认令牌与多文件恢复事务。

规格 11/12 的实现。核心不变式：
- 预检只通过 session_id + turn_id 定位权威恢复集合；任一文件冲突不生成
  可执行令牌。
- 令牌绑定会话/回合/工作区/操作种类/检查点版本/实际指纹集与当前 boot_id，
  数据库只存哈希；执行时在工作区锁内重算指纹后 CAS 消费。
- 多文件恢复是"整体预检 + 每文件原子替换 + 每步持久化日志 + 全部验证后才
  宣告成功"；不声称跨文件操作系统级单次原子提交。
- preparing 阶段失败是确定性 failed（未触碰目标）；开始应用后任何不确定性
  进入 interrupted。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from pathlib import Path

from core.turn_undo import coordinator as coordinator_mod
from core.turn_undo import recorder as recorder_mod
from core.turn_undo.models import (
    FileConflict,
    FileStageStatus,
    NodeKind,
    OperationKind,
    OperationStatus,
    RecoveryAction,
    RollbackKind,
    SnapshotAvailability,
    TurnUndoError,
    TurnUndoPreview,
    TurnUndoResult,
    TurnUndoStatus,
    UndoErrorCode,
    UNDO_RESULT_SKIPPED,
)
from core.turn_undo.storage import (
    BlobStore,
    TurnUndoStorage,
    now_ms,
)

# 阶段文件前缀：写在目标同目录，保证 os.replace 原子性（不跨文件系统）。
_STAGE_PREFIX = ".ycode-undo-stage-"


# ---------------------------------------------------------------------------
# 指纹与工作区身份
# ---------------------------------------------------------------------------

def fingerprint_line(canonical_path: str, state) -> str:
    return "|".join((
        canonical_path,
        state.kind.value,
        "1" if state.exists else "0",
        str(state.size) if state.size is not None else "-",
        state.sha256 or "-",
        state.identity or "-",
        str(state.link_count) if state.link_count is not None else "-",
        oct(state.mode) if state.mode is not None else "-",
    ))


def fingerprint_set_sha256(pairs) -> str:
    digest = hashlib.sha256()
    for canonical_path, state in sorted(pairs, key=lambda item: item[0]):
        digest.update(fingerprint_line(canonical_path, state).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 撤销动作推导
# ---------------------------------------------------------------------------

def classify_undo_action(before_kind: NodeKind, after_kind: NodeKind) -> RollbackKind:
    """按最初 before 与最终 after 推导单文件恢复动作。"""
    before_exists = before_kind is not NodeKind.MISSING
    after_exists = after_kind is not NodeKind.MISSING
    if not before_exists and after_exists:
        return RollbackKind.DELETE_CREATED
    if before_exists and not after_exists:
        return RollbackKind.RESTORE_DELETED
    if before_exists and after_exists:
        return RollbackKind.RESTORE_CONTENT
    # before 与 after 都是 missing：记录不应存在（写入前拒绝过），显式失败
    raise TurnUndoError(
        UndoErrorCode.INVALID_STATE_TRANSITION,
        "文件记录的 before/after 均缺失，无法推导恢复动作",
    )


def _node_from_file_row_side(row, side: str):
    kind_value = getattr(row, f"{side}_kind")
    if not kind_value:
        return None
    return recorder_mod.state_from_row_side(row, side)


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------

def preview_revert(
    storage: TurnUndoStorage,
    coordinator: coordinator_mod.WorkspaceCoordinator,
    *,
    session_id: int,
    turn_id: str,
    token_ttl_ms: int | None = None,
) -> tuple[TurnUndoPreview, str]:
    """预检并生成一次性确认令牌（权限拒绝或超时后令牌自然作废）。

    返回 (preview, token)；冲突时 token 为空串。
    """
    checkpoint = storage.get_checkpoint_by_turn(session_id, turn_id)
    if checkpoint is None:
        raise TurnUndoError(
            UndoErrorCode.CHECKPOINT_NOT_FOUND,
            f"该会话下不存在回合 {turn_id} 的撤销检查点",
        )
    status = checkpoint.status_enum

    if status is TurnUndoStatus.RECORDING:
        raise TurnUndoError(
            UndoErrorCode.WORKSPACE_BUSY,
            "该回合仍在进行中，暂不可撤销",
        )
    if status is TurnUndoStatus.REVERTING:
        raise TurnUndoError(
            UndoErrorCode.REVERT_INTERRUPTED,
            "该回合撤销正在进行中",
        )
    if status is TurnUndoStatus.INTERRUPTED:
        raise TurnUndoError(
            UndoErrorCode.REVERT_INTERRUPTED,
            "该回合撤销已中断，请使用恢复操作（/undo recover）",
        )
    if status is TurnUndoStatus.RECOVERING:
        raise TurnUndoError(
            UndoErrorCode.REVERT_INTERRUPTED,
            "该回合撤销恢复正在执行",
        )
    if status is TurnUndoStatus.NO_CHANGES:
        raise TurnUndoError(
            UndoErrorCode.CHECKPOINT_UNAVAILABLE,
            "该回合没有受控文件修改，无需撤销",
        )
    if status is TurnUndoStatus.REVERTED:
        raise TurnUndoError(
            UndoErrorCode.CHECKPOINT_UNAVAILABLE,
            "该回合已经撤销过",
        )
    if status is TurnUndoStatus.UNAVAILABLE:
        raise TurnUndoError(
            UndoErrorCode.CHECKPOINT_UNAVAILABLE,
            checkpoint.last_error_message or "该回合的撤销记录不可用",
        )
    # ready / conflicted / failed：允许（重新）预检

    if checkpoint.availability_enum is SnapshotAvailability.EXPIRED:
        raise TurnUndoError(
            UndoErrorCode.CHECKPOINT_EXPIRED,
            "撤销记录已过期（快照按保留策略回收），无法撤销",
        )
    if checkpoint.availability_enum is SnapshotAvailability.CORRUPT:
        raise TurnUndoError(
            UndoErrorCode.SNAPSHOT_CORRUPT,
            "撤销快照损坏，无法撤销",
        )
    if checkpoint.availability_enum is SnapshotAvailability.MISSING:
        raise TurnUndoError(
            UndoErrorCode.SNAPSHOT_MISSING,
            "撤销快照缺失，无法撤销",
        )

    files = storage.list_files(checkpoint.checkpoint_id)
    if not files:
        raise TurnUndoError(
            UndoErrorCode.CHECKPOINT_UNAVAILABLE,
            "该回合没有受控文件修改记录",
        )

    # 逐文件重查实际状态（规格 11.2）
    # 因回合内被外部修改而退出撤销范围的文件不参与冲突判定，只在预检里列出。
    conflicts: list[FileConflict] = []
    skipped_files: list[str] = []
    pairs = []
    restore_content = 0
    delete_created = 0
    restore_deleted = 0
    restore_path_state = 0
    created_files: list[str] = []
    for row in files:
        if row.undo_result == UNDO_RESULT_SKIPPED:
            skipped_files.append(row.display_path)
            continue
        expected = recorder_mod.state_from_row_side(row, "after")
        actual = recorder_mod.capture_node_state(row.canonical_path)
        pairs.append((row.canonical_path, actual))
        reasons = _compare_states(expected, actual)
        if reasons:
            conflicts.append(FileConflict(
                file_id=row.file_id,
                canonical_path=row.canonical_path,
                display_path=row.display_path,
                expected=expected,
                actual=actual,
                reasons=tuple(reasons),
            ))
            continue
        action = classify_undo_action(
            NodeKind(row.before_kind),
            expected.kind,
        )
        if action is RollbackKind.DELETE_CREATED:
            delete_created += 1
            created_files.append(row.display_path)
        elif action is RollbackKind.RESTORE_DELETED:
            restore_deleted += 1
        elif action is RollbackKind.RESTORE_PATH_STATE:
            restore_path_state += 1
        else:
            restore_content += 1

    if not pairs:
        # 所有文件都因回合内外部修改退出撤销范围：没有可执行的还原动作
        raise TurnUndoError(
            UndoErrorCode.CHECKPOINT_UNAVAILABLE,
            "该回合没有可撤销的文件（全部因回合内外部修改被跳过）",
        )

    if conflicts:
        # 结构化冲突：不生成令牌；状态转 conflicted（可重新预检）
        if status is not TurnUndoStatus.CONFLICTED:
            storage.transition_checkpoint(
                checkpoint.checkpoint_id,
                TurnUndoStatus.CONFLICTED,
                last_error_code=UndoErrorCode.FILE_CONFLICT,
                last_error_message=f"{len(conflicts)} 个文件与回合结束时的状态不一致",
            )
        preview = TurnUndoPreview(
            checkpoint_id=checkpoint.checkpoint_id,
            turn_id=checkpoint.turn_id,
            status=TurnUndoStatus.CONFLICTED,
            snapshot_availability=checkpoint.availability_enum,
            record_version=storage.get_checkpoint(checkpoint.checkpoint_id).record_version,
            file_count=len(pairs),
            restore_content_count=0,
            delete_created_count=0,
            restore_deleted_count=0,
            restore_path_state_count=0,
            created_files=(),
            conflicts=tuple(conflicts),
            skipped_files=tuple(skipped_files),
            has_uncovered_command_effects=bool(checkpoint.has_uncovered_command_effects),
            subsequent_completed_turns=storage.count_subsequent_ready_turns(
                checkpoint.session_id, checkpoint.workspace_root,
                checkpoint.started_at_ms, checkpoint.turn_id,
            ),
            workspace_root=checkpoint.workspace_root,
        )
        return preview, ""

    # 无冲突：conflicted/failed -> ready，然后签发 previewed 操作 + 令牌
    if status in (TurnUndoStatus.CONFLICTED, TurnUndoStatus.FAILED):
        storage.transition_checkpoint(checkpoint.checkpoint_id, TurnUndoStatus.READY)
        checkpoint = storage.get_checkpoint(checkpoint.checkpoint_id)

    operation = storage.create_operation(
        checkpoint_id=checkpoint.checkpoint_id,
        kind=OperationKind.REVERT,
        preview_version=checkpoint.record_version,
    )
    token = secrets.token_urlsafe(32)
    storage.create_confirmation(
        operation_id=operation.operation_id,
        checkpoint_id=checkpoint.checkpoint_id,
        record_version=checkpoint.record_version,
        workspace_identity=checkpoint.workspace_root,
        fingerprint_set_sha256=fingerprint_set_sha256(pairs),
        token=token,
        ttl_ms=token_ttl_ms or 5 * 60 * 1000,
    )

    preview = TurnUndoPreview(
        checkpoint_id=checkpoint.checkpoint_id,
        turn_id=checkpoint.turn_id,
        status=checkpoint.status_enum,
        snapshot_availability=checkpoint.availability_enum,
        record_version=checkpoint.record_version,
        file_count=len(pairs),
        restore_content_count=restore_content,
        delete_created_count=delete_created,
        restore_deleted_count=restore_deleted,
        restore_path_state_count=restore_path_state,
        created_files=tuple(created_files),
        skipped_files=tuple(skipped_files),
        has_uncovered_command_effects=bool(checkpoint.has_uncovered_command_effects),
        subsequent_completed_turns=storage.count_subsequent_ready_turns(
            checkpoint.session_id, checkpoint.workspace_root,
            checkpoint.started_at_ms, checkpoint.turn_id,
        ),
        workspace_root=checkpoint.workspace_root,
    )
    return preview, token


def _compare_states(expected, actual) -> list[str]:
    """精确比较计划 after 与实际状态；返回结构化原因列表。"""
    reasons: list[str] = []
    if expected.kind is not actual.kind:
        reasons.append(f"节点类型不一致: 预期 {expected.kind.value} 实际 {actual.kind.value}")
        return reasons
    if expected.kind is NodeKind.MISSING:
        return reasons
    if expected.size is not None and actual.size != expected.size:
        reasons.append(f"大小不一致: 预期 {expected.size} 实际 {actual.size}")
    if expected.sha256 and actual.sha256:
        if actual.sha256 != expected.sha256:
            reasons.append("内容哈希不一致")
    if expected.identity and actual.identity and actual.identity != expected.identity:
        reasons.append("文件身份（卷/File ID）变化")
    if expected.link_count is not None and actual.link_count is not None \
            and actual.link_count != expected.link_count:
        reasons.append(f"硬链接计数变化: 预期 {expected.link_count} 实际 {actual.link_count}")
    if expected.symlink_target is not None and actual.symlink_target is not None \
            and expected.symlink_target != actual.symlink_target:
        reasons.append("符号链接目标变化")
    if expected.mode is not None and actual.mode is not None \
            and actual.mode != expected.mode:
        reasons.append(f"权限位变化: 预期 {oct(expected.mode)} 实际 {oct(actual.mode)}")
    return reasons


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def _operation_dir(storage: TurnUndoStorage, operation_id: str) -> Path:
    from core.turn_undo.storage import _default_data_dir

    directory = _default_data_dir() / "undo_transactions" / operation_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _rollback_plan_for_row(row) -> tuple[RollbackKind, str | None, int | None, NodeKind]:
    """推导 (undo 动作, 撤销前回滚 blob, 撤销前 mode, 目标状态)。"""
    before_kind = NodeKind(row.before_kind)
    after_kind = NodeKind(row.after_kind) if row.after_kind else NodeKind.MISSING
    action = classify_undo_action(before_kind, after_kind)
    # 回滚（cancel_revert 用）= 用户点击撤销之前的状态 = 已验证 after
    rollback_blob = row.after_blob_sha256 if after_kind is not NodeKind.MISSING else None
    rollback_mode = row.after_mode
    return action, rollback_blob, rollback_mode, before_kind


def _write_stage_file(storage: TurnUndoStorage, operation_id: str, ordinal: int,
                      row, blob_sha: str) -> Path:
    """从源 blob 生成阶段文件（目标同目录），刷新并校验。"""
    target = Path(row.canonical_path)
    data = storage.blob_store.read_bytes(blob_sha)
    stage_path = target.parent / f"{_STAGE_PREFIX}{operation_id}-{ordinal}{target.suffix}"
    try:
        with open(stage_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if row.before_mode is not None:
            try:
                os.chmod(stage_path, row.before_mode)
            except OSError:
                pass  # 平台不支持完整 chmod（Windows）时按尽力而为语义
        if hashlib.sha256(stage_path.read_bytes()).hexdigest() != blob_sha:
            raise TurnUndoError(
                UndoErrorCode.SNAPSHOT_CORRUPT,
                f"阶段文件校验失败: {row.display_path}",
            )
    except TurnUndoError:
        stage_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        stage_path.unlink(missing_ok=True)
        raise TurnUndoError(
            UndoErrorCode.IO_ERROR,
            f"生成阶段文件失败 {row.display_path}: {exc}",
        ) from exc
    return stage_path


def _undo_action_quiet(row) -> RollbackKind:
    """排序用：推导恢复动作，不抛异常（both-missing 理论上不存在）。"""
    try:
        return classify_undo_action(
            NodeKind(row.before_kind),
            NodeKind(row.after_kind) if row.after_kind else NodeKind.MISSING,
        )
    except TurnUndoError:
        return RollbackKind.RESTORE_PATH_STATE


def _insert_operation_files(
    storage: TurnUndoStorage,
    operation: "object",
    files: list,
) -> list[tuple[object, object]]:
    """构建恢复集合：每文件独立记录 expected 指纹、回滚材料与应用顺序。"""
    rows = []
    entries = []
    ordered = sorted(
        files,
        key=lambda r: (0 if _undo_action_quiet(r) is RollbackKind.DELETE_CREATED else 1,
                       r.canonical_path),
    )
    for ordinal, row in enumerate(ordered):
        action, rollback_blob, rollback_mode, target_kind = _rollback_plan_for_row(row)
        expected = recorder_mod.state_from_row_side(row, "after")
        entries.append((row, action, rollback_blob, rollback_mode, expected))
        rows.append({
            "file_id": row.file_id,
            "apply_ordinal": ordinal,
            "expected_current_fingerprint": fingerprint_line(
                row.canonical_path, expected,
            ),
            "rollback_kind": action,
            "rollback_blob_sha256": rollback_blob,
            "rollback_mode": rollback_mode,
            "target_kind": target_kind,
            "target_blob_sha256": row.before_blob_sha256,
            "target_mode": row.before_mode,
            "stage_status": FileStageStatus.PENDING,
            "apply_status": FileStageStatus.PENDING,
            "verify_status": FileStageStatus.PENDING,
            "last_error_code": None,
        })
    with storage._manager._lock:
        cursor = storage.conn.cursor()
        try:
            for values in rows:
                storage.insert_operation_file_locked(
                    cursor, operation_id=operation.operation_id, values=values,
                )
            storage.conn.commit()
        except Exception:
            storage.conn.rollback()
            raise
    return entries


def execute_revert(
    storage: TurnUndoStorage,
    coordinator: coordinator_mod.WorkspaceCoordinator,
    *,
    session_id: int,
    turn_id: str,
    preview_token: str,
) -> TurnUndoResult:
    """两段式执行：锁内复核指纹 → CAS 消费令牌 → 阶段化 → 逐文件应用 → 验证。"""
    checkpoint = storage.get_checkpoint_by_turn(session_id, turn_id)
    if checkpoint is None:
        raise TurnUndoError(
            UndoErrorCode.CHECKPOINT_NOT_FOUND,
            f"该会话下不存在回合 {turn_id} 的撤销检查点",
        )
    confirmation = storage.find_confirmation_by_token(preview_token or "")
    if confirmation is None or confirmation["checkpoint_id"] != checkpoint.checkpoint_id:
        raise TurnUndoError(
            UndoErrorCode.TOKEN_SCOPE_MISMATCH,
            "确认令牌与目标回合不匹配",
        )
    if confirmation["record_version"] != checkpoint.record_version:
        raise TurnUndoError(
            UndoErrorCode.TOKEN_SCOPE_MISMATCH,
            "检查点已变化，令牌失效，请重新预检",
        )

    root = checkpoint.workspace_root
    operation_id = confirmation["operation_id"]
    operation = storage.get_operation(operation_id)
    if operation.status_enum is not OperationStatus.PREVIEWED:
        raise TurnUndoError(
            UndoErrorCode.TOKEN_ALREADY_USED,
            f"该预检操作已不可执行（{operation.status_enum.value}），请重新预检",
        )

    with coordinator.workspace_lock(root):
        # 锁内复核：活跃回合互斥（规格 21）。后台任务不参与前台写入互斥，
        # 但它此刻可能正在写文件，撤销同样必须避开。
        if coordinator.has_active_turn(root):
            raise TurnUndoError(
                UndoErrorCode.WORKSPACE_BUSY,
                "工作区有正在进行的回合或后台写入任务，请稍后再撤销",
            )
        if coordinator.active_undo_id(root) is not None:
            raise TurnUndoError(
                UndoErrorCode.WORKSPACE_BUSY,
                "工作区已有撤销/恢复事务正在进行",
            )

        current_checkpoint = storage.get_checkpoint(checkpoint.checkpoint_id)
        if current_checkpoint.status_enum is not TurnUndoStatus.READY:
            raise TurnUndoError(
                UndoErrorCode.INVALID_STATE_TRANSITION,
                f"检查点状态为 {current_checkpoint.status_enum.value}，无法执行撤销",
            )
        if confirmation["record_version"] != current_checkpoint.record_version:
            raise TurnUndoError(
                UndoErrorCode.TOKEN_SCOPE_MISMATCH,
                "检查点版本已变化，令牌失效，请重新预检",
            )

        # 锁内重算全部指纹（规格 11.3）；已退出撤销范围的文件不参与
        files = storage.list_files(checkpoint.checkpoint_id)
        pairs = [
            (row.canonical_path, recorder_mod.capture_node_state(row.canonical_path))
            for row in files
            if row.undo_result != UNDO_RESULT_SKIPPED
        ]
        actual_set = fingerprint_set_sha256(pairs)
        if actual_set != confirmation["fingerprint_set_sha256"]:
            storage.transition_checkpoint(
                checkpoint.checkpoint_id,
                TurnUndoStatus.CONFLICTED,
                last_error_code=UndoErrorCode.FILE_CONFLICT,
                last_error_message="执行前发现文件与预检结果不一致",
            )
            raise TurnUndoError(
                UndoErrorCode.FILE_CONFLICT,
                "执行前发现文件与预检结果不一致，本次没有修改任何文件",
            )

        # 短事务：CAS 版本 + 消费令牌 + reverting/preparing
        with storage._manager._lock:
            cursor = storage.conn.cursor()
            try:
                storage.compare_and_swap_reverting_in_tx(
                    cursor,
                    checkpoint_id=checkpoint.checkpoint_id,
                    expected_record_version=confirmation["record_version"],
                )
                storage.consume_confirmation_locked(
                    cursor, token_hash=confirmation["token_hash"],
                )
                cursor.execute(
                    "UPDATE turn_undo_operations SET status = ?, started_at_ms = ?, "
                    "confirmed_at_ms = ? WHERE operation_id = ? AND status = ?",
                    (
                        OperationStatus.PREPARING.value, now_ms(), now_ms(),
                        operation_id, OperationStatus.PREVIEWED.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise TurnUndoError(
                        UndoErrorCode.TOKEN_ALREADY_USED,
                        "预检操作已被并发消费",
                    )
                storage.conn.commit()
            except Exception:
                storage.conn.rollback()
                raise

        coordinator.register_undo(root, operation_id)
        try:
            return _run_revert_transaction(
                storage, coordinator, checkpoint, operation, files,
            )
        finally:
            coordinator.unregister_undo(root, operation_id)


def _run_revert_transaction(storage, coordinator, checkpoint, operation, files) -> TurnUndoResult:
    """prepared → applying → verifying → committed；失败进入中断恢复。"""
    operation_id = operation.operation_id
    entries = _insert_operation_files(
        storage, operation,
        [row for row in files if row.undo_result != UNDO_RESULT_SKIPPED],
    )
    op_files = {row.file_id: row for row in storage.list_operation_files(operation_id)}

    # prepared：阶段文件全部生成并校验（不触碰目标）
    stages: dict[str, Path] = {}
    try:
        for ordinal, (row, action, rollback_blob, rollback_mode, expected) in enumerate(entries):
            op_file = op_files[row.file_id]
            if action in (RollbackKind.RESTORE_CONTENT, RollbackKind.RESTORE_DELETED):
                if not row.before_blob_sha256:
                    raise TurnUndoError(
                        UndoErrorCode.SNAPSHOT_MISSING,
                        f"缺少修改前快照: {row.display_path}",
                    )
                stage = _write_stage_file(
                    storage, operation_id, ordinal, row, row.before_blob_sha256,
                )
                stages[row.file_id] = stage
        storage.transition_operation(operation_id, OperationStatus.PREPARED)
        with storage._manager._lock:
            cursor = storage.conn.cursor()
            try:
                for row, action, *_ in entries:
                    if row.file_id in stages:
                        storage.update_operation_file_stage_locked(
                            cursor, operation_file_id=op_files[row.file_id].operation_file_id,
                            stage="stage_status", status=FileStageStatus.STAGED,
                        )
                storage.conn.commit()
            except Exception:
                storage.conn.rollback()
                raise
    except TurnUndoError as error:
        # preparing 失败：确定性 failed（未触碰目标），检查点回到 failed
        storage.transition_operation(
            operation_id, OperationStatus.FAILED,
            last_error_code=error.code, last_error_message=error.message,
            finished_at_ms=now_ms(),
        )
        storage.transition_checkpoint(
            checkpoint.checkpoint_id, TurnUndoStatus.FAILED,
            last_error_code=error.code, last_error_message=error.message,
        )
        _cleanup_stages(stages)
        raise

    # applying：按确定性顺序逐文件应用
    storage.transition_operation(operation_id, OperationStatus.APPLYING)
    restored: list[str] = []
    deleted_created: list[str] = []
    restored_deleted: list[str] = []
    isolation_dir = _operation_dir(storage, operation_id)

    def _interrupt(error: TurnUndoError) -> TurnUndoError:
        storage.transition_operation(
            operation_id, OperationStatus.INTERRUPTED,
            last_error_code=error.code, last_error_message=error.message,
        )
        storage.transition_checkpoint(
            checkpoint.checkpoint_id, TurnUndoStatus.INTERRUPTED,
            last_error_code=error.code, last_error_message=error.message,
        )
        coordinator.freeze_paths(
            checkpoint.workspace_root,
            [row.canonical_path for row, *_ in entries],
        )
        return error

    try:
        for row, action, rollback_blob, rollback_mode, expected in entries:
            op_file = op_files[row.file_id]
            # 应用前再次检查当前状态 == 预期指纹
            actual = recorder_mod.capture_node_state(row.canonical_path)
            if _compare_states(expected, actual):
                raise _interrupt(TurnUndoError(
                    UndoErrorCode.FILE_CONFLICT,
                    f"应用前发现文件被外部修改: {row.display_path}",
                ))
            if action is RollbackKind.DELETE_CREATED:
                # 先移入事务隔离位置，不直接不可恢复删除
                isolated = isolation_dir / f"{op_file.operation_file_id}.isolated"
                try:
                    if isolated.exists():
                        isolated.unlink()
                    shutil.move(row.canonical_path, str(isolated))
                except OSError as exc:
                    raise _interrupt(TurnUndoError(
                        UndoErrorCode.IO_ERROR,
                        f"隔离新建文件失败 {row.display_path}: {exc}",
                    )) from exc
                after = recorder_mod.capture_node_state(row.canonical_path)
                if after.kind is not NodeKind.MISSING:
                    raise _interrupt(TurnUndoError(
                        UndoErrorCode.FILE_CONFLICT,
                        f"新建文件隔离后仍存在: {row.display_path}",
                    ))
                deleted_created.append(row.display_path)
            else:
                stage = stages.get(row.file_id)
                if stage is None:
                    raise _interrupt(TurnUndoError(
                        UndoErrorCode.SNAPSHOT_MISSING,
                        f"阶段文件缺失: {row.display_path}",
                    ))
                try:
                    Path(row.canonical_path).parent.mkdir(parents=True, exist_ok=True)
                    os.replace(stage, row.canonical_path)
                except OSError as exc:
                    stage.unlink(missing_ok=True)
                    raise _interrupt(TurnUndoError(
                        UndoErrorCode.IO_ERROR,
                        f"恢复文件失败 {row.display_path}: {exc}",
                    )) from exc
                verified = recorder_mod.verify_target_state(
                    row.canonical_path, row.before_blob_sha256, row.before_mode,
                )
                if verified is not None:
                    raise _interrupt(TurnUndoError(
                        UndoErrorCode.FILE_CONFLICT,
                        f"恢复后校验失败 {row.display_path}: {verified}",
                    ))
                if action is RollbackKind.RESTORE_DELETED:
                    restored_deleted.append(row.display_path)
                else:
                    restored.append(row.display_path)
            with storage._manager._lock:
                cursor = storage.conn.cursor()
                try:
                    storage.update_operation_file_stage_locked(
                        cursor,
                        operation_file_id=op_file.operation_file_id,
                        stage="apply_status", status=FileStageStatus.APPLIED,
                    )
                    storage.update_operation_file_stage_locked(
                        cursor,
                        operation_file_id=op_file.operation_file_id,
                        stage="verify_status", status=FileStageStatus.VERIFIED,
                    )
                    storage.set_file_undo_result_locked(
                        cursor, file_id=row.file_id,
                        undo_result=action.value, undo_error_code=None,
                    )
                    storage.conn.commit()
                except Exception:
                    storage.conn.rollback()
                    raise
    except TurnUndoError:
        _cleanup_stages(stages)
        raise

    # verifying → committed：检查点 reverted + 操作 committed + 纠正记录同事务
    storage.transition_operation(operation_id, OperationStatus.VERIFYING)
    reverted_at = now_ms()
    correction_values = {
        "workspace_root": checkpoint.workspace_root,
        "session_id": checkpoint.session_id,
        "turn_id": checkpoint.turn_id,
        "operation_id": operation_id,
        "files_summary": json.dumps(
            [row.display_path for row, *_ in entries], ensure_ascii=False,
        ),
        "affected_count": len(entries),
        "has_uncovered_command_effects": bool(checkpoint.has_uncovered_command_effects),
        "subsequent_completed_turns": storage.count_subsequent_ready_turns(
            checkpoint.session_id, checkpoint.workspace_root,
            checkpoint.started_at_ms, checkpoint.turn_id,
        ),
    }
    with storage._manager._lock:
        cursor = storage.conn.cursor()
        try:
            cursor.execute(
                "UPDATE turn_undo_operations SET status = ?, finished_at_ms = ? "
                "WHERE operation_id = ? AND status = ?",
                (OperationStatus.COMMITTED.value, reverted_at,
                 operation_id, OperationStatus.VERIFYING.value),
            )
            if cursor.rowcount != 1:
                raise TurnUndoError(
                    UndoErrorCode.INVALID_STATE_TRANSITION,
                    "撤销操作状态并发变化",
                )
            cursor.execute(
                "UPDATE turn_undo_checkpoints SET status = ?, reverted_at_ms = ?, "
                "record_version = record_version + 1 WHERE checkpoint_id = ? AND status = ?",
                (TurnUndoStatus.REVERTED.value, reverted_at,
                 checkpoint.checkpoint_id, TurnUndoStatus.REVERTING.value),
            )
            if cursor.rowcount != 1:
                raise TurnUndoError(
                    UndoErrorCode.INVALID_STATE_TRANSITION,
                    "检查点状态并发变化",
                )
            cursor.execute(
                "INSERT OR IGNORE INTO workspace_corrections "
                "(correction_id, workspace_root, session_id, turn_id, operation_id, "
                "files_summary, affected_count, has_uncovered_command_effects, "
                "subsequent_completed_turns, created_at_ms) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"corr-{operation_id}", correction_values["workspace_root"],
                    correction_values["session_id"], correction_values["turn_id"],
                    operation_id, correction_values["files_summary"],
                    correction_values["affected_count"],
                    1 if correction_values["has_uncovered_command_effects"] else 0,
                    correction_values["subsequent_completed_turns"], now_ms(),
                ),
            )
            storage.conn.commit()
        except Exception:
            storage.conn.rollback()
            raise

    _cleanup_stages(stages)
    _cleanup_transaction_dir(storage, operation_id)
    return TurnUndoResult(
        checkpoint_id=checkpoint.checkpoint_id,
        turn_id=checkpoint.turn_id,
        status=TurnUndoStatus.REVERTED,
        operation_id=operation_id,
        reverted_at_ms=reverted_at,
        restored_files=tuple(restored),
        deleted_created_files=tuple(deleted_created),
        restored_deleted_files=tuple(restored_deleted),
    )


def _cleanup_stages(stages: dict[str, Path]) -> None:
    for stage in stages.values():
        try:
            stage.unlink(missing_ok=True)
        except OSError:
            pass


def _cleanup_transaction_dir(storage: TurnUndoStorage, operation_id: str) -> None:
    from core.turn_undo.storage import _default_data_dir

    directory = _default_data_dir() / "undo_transactions" / operation_id
    shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# 幂等重复调用
# ---------------------------------------------------------------------------

def idempotent_reverted_result(storage: TurnUndoStorage, checkpoint) -> TurnUndoResult:
    """已撤销回合重复调用：返回幂等成功摘要，不再修改文件。"""
    return TurnUndoResult(
        checkpoint_id=checkpoint.checkpoint_id,
        turn_id=checkpoint.turn_id,
        status=TurnUndoStatus.REVERTED,
        operation_id="",
        reverted_at_ms=checkpoint.reverted_at_ms or 0,
        idempotent=True,
        original_reverted_at_ms=checkpoint.reverted_at_ms,
    )


# 恢复动作引用（避免循环导入，供 recovery.py 复用）
__all__ = [
    "preview_revert",
    "execute_revert",
    "idempotent_reverted_result",
    "classify_undo_action",
    "fingerprint_line",
    "fingerprint_set_sha256",
    "RecoveryAction",
]
