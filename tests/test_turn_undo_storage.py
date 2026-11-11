"""turn_undo 存储层测试：状态机、DAO、内容寻址快照仓库、令牌、纠正与 GC。"""

import hashlib
import time
import os
import threading

import pytest

from core.memory import MemoryManager
from core.turn_undo import storage as tu_storage
from core.turn_undo.models import (
    FileStageStatus,
    NodeKind,
    OperationKind,
    OperationStatus,
    OwnerKind,
    PositionKind,
    RollbackKind,
    SnapshotAvailability,
    TurnUndoError,
    TurnUndoStatus,
    UndoErrorCode,
    require_checkpoint_transition,
    require_operation_transition,
)
from core.turn_undo.storage import (
    BlobStore,
    TurnUndoStorage,
    insert_message_turn_links_locked,
    now_ms,
)


@pytest.fixture()
def manager(tmp_path):
    return MemoryManager(str(tmp_path / "ycode.db"))


@pytest.fixture()
def store(manager, tmp_path):
    return TurnUndoStorage(manager, blob_root=tmp_path / "blobs")


def _make_session(manager, name="sess-undo"):
    manager.add_messages(name, [("user", "hello")])
    return manager


def _make_checkpoint(store, manager, session="sess-undo", turn=None, root=None, **kwargs):
    _make_session(manager, session)
    session_id = store.resolve_session_id(session)
    return store.create_checkpoint(
        session_id=session_id,
        turn_id=turn or f"turn-{session_id}-{now_ms()}",
        workspace_root=root or str(kwargs.pop("root_path", "/ws")),
        owner_kind=kwargs.pop("owner_kind", OwnerKind.FOREGROUND_TURN),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 状态机迁移表
# ---------------------------------------------------------------------------

def test_checkpoint_transition_table_accepts_legal_paths():
    require_checkpoint_transition(TurnUndoStatus.RECORDING, TurnUndoStatus.READY)
    require_checkpoint_transition(TurnUndoStatus.READY, TurnUndoStatus.REVERTING)
    require_checkpoint_transition(TurnUndoStatus.CONFLICTED, TurnUndoStatus.READY)
    require_checkpoint_transition(TurnUndoStatus.REVERTING, TurnUndoStatus.INTERRUPTED)
    require_checkpoint_transition(TurnUndoStatus.INTERRUPTED, TurnUndoStatus.RECOVERING)
    require_checkpoint_transition(TurnUndoStatus.RECOVERING, TurnUndoStatus.REVERTED)


def test_checkpoint_transition_rejects_illegal_and_unknown():
    with pytest.raises(TurnUndoError) as exc:
        require_checkpoint_transition(TurnUndoStatus.RECORDING, TurnUndoStatus.REVERTED)
    assert exc.value.code == UndoErrorCode.INVALID_STATE_TRANSITION
    with pytest.raises(TurnUndoError):
        require_checkpoint_transition(TurnUndoStatus.REVERTED, TurnUndoStatus.READY)
    with pytest.raises(TurnUndoError):
        require_checkpoint_transition(TurnUndoStatus.NO_CHANGES, TurnUndoStatus.READY)


def test_operation_transition_rejects_skip_states():
    require_operation_transition(OperationStatus.PREVIEWED, OperationStatus.PREPARING)
    with pytest.raises(TurnUndoError):
        require_operation_transition(OperationStatus.PREVIEWED, OperationStatus.APPLYING)
    with pytest.raises(TurnUndoError):
        require_operation_transition(OperationStatus.COMMITTED, OperationStatus.PREPARING)


# ---------------------------------------------------------------------------
# 内容寻址快照仓库
# ---------------------------------------------------------------------------

def test_blob_store_put_read_dedupe(tmp_path):
    blob = BlobStore(tmp_path / "blobs")
    data = b"hello undo \x00 binary \xff"
    sha = blob.put_bytes(data)
    assert sha == hashlib.sha256(data).hexdigest()
    assert blob.blob_path(sha).is_file()
    assert blob.read_bytes(sha) == data
    # 去重：同内容再写不产生第二个文件
    before_count = sum(1 for _ in (tmp_path / "blobs").rglob("*") if _.is_file())
    sha2 = blob.put_bytes(data)
    after_count = sum(1 for _ in (tmp_path / "blobs").rglob("*") if _.is_file())
    assert sha2 == sha
    assert before_count == after_count


def test_blob_store_rejects_missing_and_corrupt(tmp_path):
    blob = BlobStore(tmp_path / "blobs")
    sha = blob.put_bytes(b"payload")
    with pytest.raises(TurnUndoError) as exc:
        blob.read_bytes("0" * 64)
    assert exc.value.code == UndoErrorCode.SNAPSHOT_MISSING
    blob.blob_path(sha).write_bytes(b"tampered")
    with pytest.raises(TurnUndoError) as exc:
        blob.read_bytes(sha)
    assert exc.value.code == UndoErrorCode.SNAPSHOT_CORRUPT


def test_blob_store_streaming_write(tmp_path):
    blob = BlobStore(tmp_path / "blobs")
    import io

    data = b"x" * (3 * 1024 * 1024 + 17)
    sha = blob.put_stream(io.BytesIO(data))
    assert blob.read_bytes(sha) == data


# ---------------------------------------------------------------------------
# 检查点 DAO 与 record_version
# ---------------------------------------------------------------------------

def test_create_and_get_checkpoint_roundtrip(store, manager):
    cp = _make_checkpoint(store, manager, turn="t-roundtrip")
    assert cp.status_enum is TurnUndoStatus.RECORDING
    assert cp.record_version == 1
    fetched = store.get_checkpoint_by_turn(cp.session_id, "t-roundtrip")
    assert fetched is not None and fetched.checkpoint_id == cp.checkpoint_id


def test_turn_id_globally_unique(store, manager):
    cp = _make_checkpoint(store, manager, turn="t-dup")
    with pytest.raises(Exception):
        store.create_checkpoint(
            session_id=cp.session_id + 1000000,
            turn_id="t-dup",
            workspace_root="/ws",
            owner_kind=OwnerKind.FOREGROUND_TURN,
        )


def test_transition_bumps_record_version(store, manager):
    cp = _make_checkpoint(store, manager)
    row = store.transition_checkpoint(cp.checkpoint_id, TurnUndoStatus.READY)
    assert row.status_enum is TurnUndoStatus.READY
    assert row.record_version == cp.record_version + 1
    with pytest.raises(TurnUndoError) as exc:
        store.transition_checkpoint(cp.checkpoint_id, TurnUndoStatus.REVERTED)
    assert exc.value.code == UndoErrorCode.INVALID_STATE_TRANSITION


def test_cas_reverting_requires_version_and_ready(store, manager):
    cp = _make_checkpoint(store, manager)
    with pytest.raises(TurnUndoError):
        store.compare_and_swap_reverting(cp.checkpoint_id, expected_record_version=999)
    store.transition_checkpoint(cp.checkpoint_id, TurnUndoStatus.READY)
    row = store.compare_and_swap_reverting(
        cp.checkpoint_id, expected_record_version=cp.record_version + 1
    )
    assert row.status_enum is TurnUndoStatus.REVERTING


def test_parent_checkpoint_must_match_session_and_workspace(store, manager):
    cp = _make_checkpoint(store, manager, turn="t-parent", root="/ws-a")
    _make_session(manager, "sess-other")
    other_id = store.resolve_session_id("sess-other")
    with pytest.raises(TurnUndoError) as exc:
        store.create_checkpoint(
            session_id=other_id,
            turn_id="t-child",
            workspace_root="/ws-a",
            owner_kind=OwnerKind.BACKGROUND_TASK,
            parent_checkpoint_id=cp.checkpoint_id,
        )
    assert exc.value.code == UndoErrorCode.OPERATION_SCOPE_MISMATCH
    child = store.create_checkpoint(
        session_id=cp.session_id,
        turn_id="t-child",
        workspace_root="/ws-a",
        owner_kind=OwnerKind.BACKGROUND_TASK,
        parent_checkpoint_id=cp.checkpoint_id,
    )
    assert child.parent_checkpoint_id == cp.checkpoint_id


# ---------------------------------------------------------------------------
# 文件记录：最初 before 保留、最终 after 替换
# ---------------------------------------------------------------------------

def _file_values(after_kind=NodeKind.REGULAR_FILE, after_sha="a" * 64, blob="b" * 64, **overrides):
    values = {
        "canonical_path": "/ws/core/tools.py",
        "display_path": "core/tools.py",
        "before_kind": NodeKind.REGULAR_FILE.value,
        "before_exists": 1,
        "before_size": 10,
        "before_sha256": "0" * 64,
        "before_blob_sha256": "1" * 64,
        "before_mode": 0o644,
        "before_file_identity": "2:100",
        "before_link_count": 1,
        "before_symlink_target": None,
        "after_kind": after_kind.value,
        "after_exists": 0 if after_kind is NodeKind.MISSING else 1,
        "after_size": 20,
        "after_sha256": after_sha,
        "after_blob_sha256": blob,
        "after_mode": 0o644,
        "after_file_identity": None,
        "after_link_count": None,
        "after_symlink_target": None,
        "diff_kind": "text",
        "mutated_at_ms": now_ms(),
        "source_kind": "main_agent",
        "source_id": "turn-x",
        "tool_name": "write_file",
        "tool_call_id": "call-1",
    }
    values.update(overrides)
    return values


def test_file_upsert_keeps_original_before(store, manager):
    cp = _make_checkpoint(store, manager)
    with manager._lock:
        cursor = manager.conn.cursor()
        file_id = store.insert_file_locked(cursor, checkpoint_id=cp.checkpoint_id,
                                           values=_file_values(after_sha="a" * 64))
        manager.conn.commit()
    row = store.get_file(file_id)
    assert row.before_sha256 == "0" * 64
    assert row.after_sha256 == "a" * 64
    # 同一回合第二次修改：只替换 after
    with manager._lock:
        cursor = manager.conn.cursor()
        store.update_after_plan_locked(
            cursor, file_id=file_id, checkpoint_id=cp.checkpoint_id,
            values=_file_values(after_sha="c" * 64, blob="d" * 64),
        )
        manager.conn.commit()
    row = store.get_file(file_id)
    assert row.before_sha256 == "0" * 64
    assert row.after_sha256 == "c" * 64
    assert row.after_blob_sha256 == "d" * 64
    # record_version 因恢复集合变化递增
    assert store.get_checkpoint(cp.checkpoint_id).record_version > cp.record_version + 1


def test_list_files_and_uniqueness(store, manager):
    cp = _make_checkpoint(store, manager)
    with manager._lock:
        cursor = manager.conn.cursor()
        store.insert_file_locked(cursor, checkpoint_id=cp.checkpoint_id,
                                 values=_file_values())
        manager.conn.commit()
        with pytest.raises(Exception):
            store.insert_file_locked(cursor, checkpoint_id=cp.checkpoint_id,
                                     values=_file_values())
        manager.conn.rollback()
    assert len(store.list_files(cp.checkpoint_id)) == 1


# ---------------------------------------------------------------------------
# 撤销操作与恢复链
# ---------------------------------------------------------------------------

def test_operation_lifecycle_and_parent_resolution(store, manager):
    cp = _make_checkpoint(store, manager)
    op = store.create_operation(checkpoint_id=cp.checkpoint_id, kind=OperationKind.REVERT)
    assert op.status_enum is OperationStatus.PREVIEWED
    store.transition_operation(op.operation_id, OperationStatus.PREPARING,
                               confirmed_at_ms=now_ms())
    store.transition_operation(op.operation_id, OperationStatus.PREPARED)
    store.transition_operation(op.operation_id, OperationStatus.APPLYING)
    store.transition_operation(op.operation_id, OperationStatus.INTERRUPTED)
    # 恢复子操作必须引用未解决父操作且同检查点
    child = store.create_operation(
        checkpoint_id=cp.checkpoint_id, kind=OperationKind.CONTINUE_REVERT,
        parent_operation_id=op.operation_id,
    )
    with manager._lock:
        cursor = manager.conn.cursor()
        store.resolve_interrupted_parent_locked(
            cursor, parent_operation_id=op.operation_id,
            resolved_by_operation_id=child.operation_id,
            target=OperationStatus.COMMITTED,
        )
        manager.conn.commit()
    parent = store.get_operation(op.operation_id)
    assert parent.status_enum is OperationStatus.COMMITTED
    assert parent.resolved_by_operation_id == child.operation_id


def test_recovery_rejects_cross_checkpoint_and_unresolved_parent(store, manager):
    cp1 = _make_checkpoint(store, manager, turn="t-a")
    cp2 = _make_checkpoint(store, manager, turn="t-b")
    op = store.create_operation(checkpoint_id=cp1.checkpoint_id, kind=OperationKind.REVERT)
    store.transition_operation(op.operation_id, OperationStatus.PREPARING)
    # 父操作不是 interrupted/recovery_required → 拒绝
    with pytest.raises(TurnUndoError) as exc:
        store.create_operation(
            checkpoint_id=cp1.checkpoint_id, kind=OperationKind.CONTINUE_REVERT,
            parent_operation_id=op.operation_id,
        )
    assert exc.value.code == UndoErrorCode.OPERATION_SCOPE_MISMATCH
    store.transition_operation(op.operation_id, OperationStatus.PREPARED)
    store.transition_operation(op.operation_id, OperationStatus.APPLYING)
    store.transition_operation(op.operation_id, OperationStatus.INTERRUPTED)
    # 跨检查点父子关系拒绝
    with pytest.raises(TurnUndoError) as exc:
        store.create_operation(
            checkpoint_id=cp2.checkpoint_id, kind=OperationKind.CANCEL_REVERT,
            parent_operation_id=op.operation_id,
        )
    assert exc.value.code == UndoErrorCode.OPERATION_SCOPE_MISMATCH


def test_recovery_cycle_rejected(store, manager):
    cp = _make_checkpoint(store, manager)
    op = store.create_operation(checkpoint_id=cp.checkpoint_id, kind=OperationKind.REVERT)
    store.transition_operation(op.operation_id, OperationStatus.PREPARING)
    store.transition_operation(op.operation_id, OperationStatus.PREPARED)
    store.transition_operation(op.operation_id, OperationStatus.APPLYING)
    store.transition_operation(op.operation_id, OperationStatus.INTERRUPTED)
    child = store.create_operation(
        checkpoint_id=cp.checkpoint_id, kind=OperationKind.CONTINUE_REVERT,
        parent_operation_id=op.operation_id,
    )
    # 子操作未终态时不能成为新的父（不处于 interrupted）
    with pytest.raises(TurnUndoError):
        store.create_operation(
            checkpoint_id=cp.checkpoint_id, kind=OperationKind.CANCEL_REVERT,
            parent_operation_id=child.operation_id,
        )


def test_list_nonterminal_operations(store, manager):
    cp = _make_checkpoint(store, manager)
    op = store.create_operation(checkpoint_id=cp.checkpoint_id, kind=OperationKind.REVERT)
    store.transition_operation(op.operation_id, OperationStatus.PREPARING)
    assert [o.operation_id for o in store.list_nonterminal_operations()] == [op.operation_id]
    store.transition_operation(op.operation_id, OperationStatus.FAILED,
                               last_error_code="io_error")
    assert store.list_nonterminal_operations() == []


# ---------------------------------------------------------------------------
# 确认令牌
# ---------------------------------------------------------------------------

def test_confirmation_single_consumption_and_expiry(store, manager):
    cp = _make_checkpoint(store, manager)
    op = store.create_operation(checkpoint_id=cp.checkpoint_id, kind=OperationKind.REVERT)
    token = store.create_confirmation(
        operation_id=op.operation_id, checkpoint_id=cp.checkpoint_id,
        record_version=cp.record_version, workspace_identity="/ws",
        fingerprint_set_sha256="f" * 64, token="secret-token",
    )
    assert token == "secret-token"
    record = store.find_confirmation_by_token("secret-token")
    assert record is not None
    # 数据库只保存哈希
    assert record["token_hash"] == hashlib.sha256(b"secret-token").hexdigest()
    with manager._lock:
        cursor = manager.conn.cursor()
        store.consume_confirmation_locked(cursor, token_hash=record["token_hash"])
        manager.conn.commit()
    with manager._lock:
        cursor = manager.conn.cursor()
        with pytest.raises(TurnUndoError) as exc:
            store.consume_confirmation_locked(cursor, token_hash=record["token_hash"])
            manager.conn.rollback()
    assert exc.value.code == UndoErrorCode.TOKEN_ALREADY_USED
    # 未知令牌
    with manager._lock:
        cursor = manager.conn.cursor()
        with pytest.raises(TurnUndoError) as exc:
            store.consume_confirmation_locked(cursor, token_hash="f" * 64)
    assert exc.value.code == UndoErrorCode.TOKEN_SCOPE_MISMATCH


def test_confirmation_rejects_cross_boot(store, manager):
    cp = _make_checkpoint(store, manager)
    op = store.create_operation(checkpoint_id=cp.checkpoint_id, kind=OperationKind.REVERT)
    store.create_confirmation(
        operation_id=op.operation_id, checkpoint_id=cp.checkpoint_id,
        record_version=1, workspace_identity="/ws", fingerprint_set_sha256="f" * 64,
        token="tok",
    )
    record = store.find_confirmation_by_token("tok")
    assert record["boot_id"] == tu_storage.BOOT_ID
    # 模拟重启：BOOT_ID 变化后旧令牌拒绝消费
    old_boot = tu_storage.BOOT_ID
    try:
        tu_storage.BOOT_ID = "different-boot"
        with manager._lock:
            cursor = manager.conn.cursor()
            with pytest.raises(TurnUndoError) as exc:
                store.consume_confirmation_locked(cursor, token_hash=record["token_hash"])
        assert exc.value.code == UndoErrorCode.TOKEN_EXPIRED
        assert store.delete_stale_confirmations() >= 1
        assert store.find_confirmation_by_token("tok") is None
    finally:
        tu_storage.BOOT_ID = old_boot


# ---------------------------------------------------------------------------
# 纠正记录与后续回合计数
# ---------------------------------------------------------------------------

def test_correction_idempotent_by_operation(store, manager):
    cp = _make_checkpoint(store, manager, root="/ws-corr")
    corr1 = store.insert_correction(
        workspace_root="/ws-corr", session_id=cp.session_id, turn_id=cp.turn_id,
        operation_id="op-1", files_summary='["a.py"]', affected_count=1,
        has_uncovered_command_effects=False, subsequent_completed_turns=0,
    )
    corr2 = store.insert_correction(
        workspace_root="/ws-corr", session_id=cp.session_id, turn_id=cp.turn_id,
        operation_id="op-1", files_summary='["a.py"]', affected_count=1,
        has_uncovered_command_effects=False, subsequent_completed_turns=0,
    )
    assert corr1 == corr2
    rows = store.list_corrections("/ws-corr")
    assert len(rows) == 1 and rows[0]["turn_id"] == cp.turn_id
    # 不同工作区不串
    assert store.list_corrections("/other-ws") == []


def test_count_subsequent_ready_turns(store, manager):
    cp1 = _make_checkpoint(store, manager, turn="t-1", root="/ws-n")
    store.transition_checkpoint(cp1.checkpoint_id, TurnUndoStatus.READY)
    time.sleep(0.003)
    cp2 = _make_checkpoint(store, manager, turn="t-2", root="/ws-n")
    store.transition_checkpoint(cp2.checkpoint_id, TurnUndoStatus.READY)
    # 早回合视角：之后有 1 个完成回合；晚回合视角：0
    assert store.count_subsequent_ready_turns(
        cp1.session_id, "/ws-n", cp1.started_at_ms, cp1.turn_id) == 1
    assert store.count_subsequent_ready_turns(
        cp2.session_id, "/ws-n", cp2.started_at_ms, cp2.turn_id) == 0


# ---------------------------------------------------------------------------
# message_turn_links
# ---------------------------------------------------------------------------

def test_message_turn_links_same_transaction(store, manager):
    cp = _make_checkpoint(store, manager)
    manager.add_messages("sess-undo", [("user", "hi"), ("assistant", "yo")])
    with manager._lock:
        cursor = manager.conn.cursor()
        cursor.execute(
            "SELECT id FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 2",
            (cp.session_id,),
        )
        message_ids = [int(r[0]) for r in cursor.fetchall()]
        message_ids.reverse()
    assert len(message_ids) == 2
    with manager._lock:
        cursor = manager.conn.cursor()
        insert_message_turn_links_locked(cursor, [
            (message_ids[0], cp.checkpoint_id, PositionKind.USER.value),
            (message_ids[1], cp.checkpoint_id, PositionKind.ASSISTANT_FINAL.value),
        ])
        manager.conn.commit()
        cursor.execute(
            "SELECT message_id, position_kind FROM message_turn_links "
            "WHERE checkpoint_id = ? ORDER BY message_id",
            (cp.checkpoint_id,),
        )
        rows = cursor.fetchall()
    assert rows == [
        (message_ids[0], PositionKind.USER.value),
        (message_ids[1], PositionKind.ASSISTANT_FINAL.value),
    ]


def test_message_turn_links_unknown_kind_and_missing_checkpoint(store, manager):
    cp = _make_checkpoint(store, manager)
    manager.add_messages("sess-undo", [("user", "hi")])
    with manager._lock:
        cursor = manager.conn.cursor()
        cursor.execute(
            "SELECT id FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (cp.session_id,),
        )
        message_id = int(cursor.fetchone()[0])
    with manager._lock:
        cursor = manager.conn.cursor()
        with pytest.raises(TurnUndoError):
            insert_message_turn_links_locked(
                cursor, [(message_id, cp.checkpoint_id, "mystery")])
        manager.conn.rollback()
        with pytest.raises(TurnUndoError):
            insert_message_turn_links_locked(
                cursor, [(message_id, "ckpt-missing", PositionKind.USER.value)])
        manager.conn.rollback()


# ---------------------------------------------------------------------------
# 保留策略与 GC
# ---------------------------------------------------------------------------

def test_retention_expires_old_checkpoints_but_keeps_lifecycle(store, manager):
    # 最旧的已撤销检查点：验证超限过期只动快照保留状态、不覆盖 reverted 事实
    revoked = _make_checkpoint(store, manager, turn="t-rev2", root="/ws-ret")
    store.transition_checkpoint(revoked.checkpoint_id, TurnUndoStatus.READY)
    store.transition_checkpoint(revoked.checkpoint_id, TurnUndoStatus.REVERTING)
    store.transition_checkpoint(revoked.checkpoint_id, TurnUndoStatus.REVERTED,
                                reverted_at_ms=now_ms())
    time.sleep(0.003)
    for i in range(55):
        cp = _make_checkpoint(store, manager, turn=f"t-ret-{i}", root="/ws-ret")
        store.transition_checkpoint(cp.checkpoint_id, TurnUndoStatus.READY)
        with manager._lock:
            cursor = manager.conn.cursor()
            file_id = store.insert_file_locked(
                cursor, checkpoint_id=cp.checkpoint_id,
                values=_file_values(blob=f"{i:064x}"),
            )
            manager.conn.commit()
        time.sleep(0.001)

    expired = store.expire_retained_checkpoints(store.resolve_session_id("sess-undo"), keep=50)
    assert len(expired) >= 5
    # 最新 50 个保持 available；被撤销回合生命周期不被覆盖，仅快照过期
    revoked_row = store.get_checkpoint(revoked.checkpoint_id)
    assert revoked_row.status_enum is TurnUndoStatus.REVERTED
    assert revoked_row.availability_enum is SnapshotAvailability.EXPIRED
    # recording 检查点永不过期
    active = _make_checkpoint(store, manager, turn="t-active", root="/ws-ret")
    store.expire_retained_checkpoints(store.resolve_session_id("sess-undo"), keep=50)
    assert store.get_checkpoint(active.checkpoint_id).availability_enum \
        is SnapshotAvailability.NOT_REQUIRED


def test_gc_removes_only_unreferenced_blobs(store, manager, tmp_path):
    blob = store.blob_store
    keep_sha = blob.put_bytes(b"kept")
    drop_sha = blob.put_bytes(b"dropped")
    # keep_sha 先建立数据库引用，drop_sha 无引用
    cp = _make_checkpoint(store, manager, root="/ws-gc")
    store.transition_checkpoint(cp.checkpoint_id, TurnUndoStatus.READY)
    with manager._lock:
        cursor = manager.conn.cursor()
        store.insert_file_locked(
            cursor, checkpoint_id=cp.checkpoint_id,
            values=_file_values(before_blob_sha256=keep_sha, blob=keep_sha),
        )
        manager.conn.commit()
    assert store.collect_blob_garbage() >= 1
    assert blob.blob_path(drop_sha).exists() is False
    assert blob.blob_path(keep_sha).exists()
    # 过期检查点的 blob 不再受保护
    store.expire_retained_checkpoints(store.resolve_session_id("sess-undo"), keep=0)
    store.collect_blob_garbage()
    assert not blob.blob_path(keep_sha).exists()


def test_thread_safety_of_checkpoint_writes(store, manager):
    cp = _make_checkpoint(store, manager)
    errors = []

    def worker(n):
        try:
            for _ in range(5):
                store.mark_uncovered_command_effects(cp.checkpoint_id)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store.get_checkpoint(cp.checkpoint_id).has_uncovered_command_effects == 1
