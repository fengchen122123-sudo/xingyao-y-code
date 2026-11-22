import threading

import time

import pytest

from core.memory import MemoryManager
from core.turn_undo import coordinator as coordinator_mod
from core.turn_undo import recorder as tu_recorder
from core.turn_undo import service as turn_undo_service
from core.turn_undo.models import (
    FileStageStatus,
    NodeKind,
    OperationStatus,
    OwnerKind,
    RecoveryAction,
    TurnUndoError,
    TurnUndoStatus,
    UndoErrorCode,
)
from core.tools import execute_command, write_file


@pytest.fixture()
def env(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / "data"))
    manager = MemoryManager(str(tmp_path / "ycode.db"))
    storage = turn_undo_service.configure_default_manager(
        manager, blob_root=tmp_path / "undo_blobs"
    )
    manager.add_messages("sess-rec", [("user", "hi")])
    session_id = storage.resolve_session_id("sess-rec")
    return {
        "ws": ws, "manager": manager, "storage": storage,
        "session_id": session_id, "coordinator": coordinator_mod.get_coordinator(),
        "tmp": tmp_path,
    }


def _begin_turn(env, turn="turn-rec-1"):
    checkpoint, ctx = turn_undo_service.begin_turn(
        session_id=env["session_id"],
        turn_id=turn,
        workspace_root=str(env["ws"]),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )
    return checkpoint, ctx


def _record_write(env, ctx, path, content, call="call-1"):
    handle = tu_recorder.prepare_mutation(
        display_path=str(path),
        after_kind=NodeKind.REGULAR_FILE,
        after_content=content.encode("utf-8"),
        tool_name="write_file",
        tool_call_id=call,
    )
    path.write_text(content, encoding="utf-8", newline="")
    tu_recorder.verify_after_mutation(handle)


def preview_operation_id(env, checkpoint):
    ops = [
        o for o in env["storage"].list_operations_for_checkpoint(checkpoint.checkpoint_id)
        if o.kind == "revert"
    ]
    assert ops, "no revert operation for checkpoint"
    return ops[-1].operation_id


def _parent_status(env, checkpoint):
    ops = [
        o for o in env["storage"].list_operations_for_checkpoint(checkpoint.checkpoint_id)
        if o.kind == "revert"
    ]
    assert ops
    return ops[-1].status


def _interrupt_mid_apply(env, checkpoint, token, fail_path):
    original_capture = tu_recorder.capture_node_state
    fail_canonical = tu_recorder.canonicalize_path(str(fail_path))
    counter = {"n": 0}

    def poisoned(path):
        state = original_capture(path)
        if tu_recorder.canonicalize_path(str(path)) == fail_canonical:
            counter["n"] += 1
            if counter["n"] == 2:
                from core.turn_undo.models import NodeState

                return NodeState(
                    kind=NodeKind.REGULAR_FILE, exists=True, size=state.size,
                    sha256="f" * 64, mode=state.mode, identity=state.identity,
                    link_count=state.link_count,
                )
        return state

    from core.turn_undo import transaction

    tu_recorder.capture_node_state = poisoned
    transaction.recorder_mod.capture_node_state = poisoned
    try:
        with pytest.raises(TurnUndoError) as exc:
            turn_undo_service.execute_turn_undo(
                env["session_id"], checkpoint.turn_id, token
            )
    finally:
        tu_recorder.capture_node_state = original_capture
        transaction.recorder_mod.capture_node_state = original_capture
    assert exc.value.code == UndoErrorCode.FILE_CONFLICT
    return exc.value


def _two_file_turn(env):
    ws = env["ws"]
    f1 = ws / "r1.py"
    f2 = ws / "r2.py"
    f1.write_bytes(b"old-1")
    f2.write_bytes(b"old-2")
    checkpoint, ctx = _begin_turn(env)
    _record_write(env, ctx, f1, "new-1", call="c1")
    _record_write(env, ctx, f2, "new-2", call="c2")
    turn_undo_service.finish_turn(ctx)
    _, token = turn_undo_service.preview_turn_undo(env["session_id"], checkpoint.turn_id)
    return checkpoint, f1, f2, token


def test_interrupt_freezes_paths_and_continue_revert_completes(env, monkeypatch):
    monkeypatch.chdir(env["ws"])
    checkpoint, f1, f2, token = _two_file_turn(env)
    _interrupt_mid_apply(env, checkpoint, token, f2)

    row = env["storage"].get_checkpoint(checkpoint.checkpoint_id)
    assert row.status_enum is TurnUndoStatus.INTERRUPTED
    assert f1.read_bytes() == b"old-1"
    assert f2.read_bytes() == b"new-2"
    coordinator = env["coordinator"]
    assert coordinator.is_path_frozen_any(str(f1))
    assert coordinator.is_path_frozen_any(str(f2))
    sub = env["ws"] / "subdir" / "x.txt"
    assert coordinator.is_path_frozen_any(str(sub)) is False

    result = write_file(str(f1), "sneaky")
    assert "冻结" in result
    assert f1.read_bytes() == b"old-1"

    preview, rtoken = turn_undo_service.preview_turn_undo_recovery(
        env["session_id"], preview_operation_id(env, checkpoint),
        RecoveryAction.CONTINUE_REVERT,
    )
    assert preview.recovery_required is False
    assert set(preview.processed_files) == {"r1.py"}
    assert set(preview.remaining_files) == {"r2.py"}

    result = turn_undo_service.execute_turn_undo_recovery(
        env["session_id"], preview_operation_id(env, checkpoint),
        RecoveryAction.CONTINUE_REVERT, rtoken,
    )
    assert result.checkpoint_status is TurnUndoStatus.REVERTED
    assert f2.read_bytes() == b"old-2"
    assert not coordinator.is_path_frozen_any(str(f1))
    parent_status = _parent_status(env, checkpoint)
    assert parent_status == OperationStatus.COMMITTED.value
    corrections = env["storage"].list_corrections(checkpoint.workspace_root)
    assert len(corrections) == 1


def test_cancel_revert_restores_pre_undo_state(env):
    checkpoint, f1, f2, token = _two_file_turn(env)
    _interrupt_mid_apply(env, checkpoint, token, f2)
    op_id = preview_operation_id(env, checkpoint)

    preview, rtoken = turn_undo_service.preview_turn_undo_recovery(
        env["session_id"], op_id, RecoveryAction.CANCEL_REVERT,
    )
    assert preview.recovery_required is False
    result = turn_undo_service.execute_turn_undo_recovery(
        env["session_id"], op_id, RecoveryAction.CANCEL_REVERT, rtoken,
    )
    assert result.checkpoint_status is TurnUndoStatus.READY
    assert f1.read_bytes() == b"new-1"
    assert f2.read_bytes() == b"new-2"
    assert _parent_status(env, checkpoint) == OperationStatus.COMPENSATED.value
    assert env["storage"].list_corrections(checkpoint.workspace_root) == []
    _, token2 = turn_undo_service.preview_turn_undo(env["session_id"], checkpoint.turn_id)
    assert token2
    res = turn_undo_service.execute_turn_undo(env["session_id"], checkpoint.turn_id, token2)
    assert res.status is TurnUndoStatus.REVERTED
    assert f1.read_bytes() == b"old-1"
    assert f2.read_bytes() == b"old-2"


def test_unknown_state_requires_recovery_required(env):
    checkpoint, f1, f2, token = _two_file_turn(env)
    _interrupt_mid_apply(env, checkpoint, token, f2)
    op_id = preview_operation_id(env, checkpoint)
    f2.write_bytes(b"external-after-crash")

    preview, rtoken = turn_undo_service.preview_turn_undo_recovery(
        env["session_id"], op_id, RecoveryAction.CONTINUE_REVERT,
    )
    assert preview.recovery_required is True
    assert "r2.py" in preview.unknown_files
    with pytest.raises(TurnUndoError) as exc:
        turn_undo_service.execute_turn_undo_recovery(
            env["session_id"], op_id, RecoveryAction.CONTINUE_REVERT,
            rtoken or "any",
        )
    assert exc.value.code in (
        UndoErrorCode.TOKEN_SCOPE_MISMATCH, UndoErrorCode.RECOVERY_REQUIRED,
    )
    assert f2.read_bytes() == b"external-after-crash"


def test_recovery_scope_mismatch(env, tmp_path):
    checkpoint, f1, f2, token = _two_file_turn(env)
    _interrupt_mid_apply(env, checkpoint, token, f2)
    op_id = preview_operation_id(env, checkpoint)
    env["manager"].add_messages("other-session", [("user", "hi")])
    other_id = env["storage"].resolve_session_id("other-session")
    with pytest.raises(TurnUndoError) as exc:
        turn_undo_service.preview_turn_undo_recovery(
            other_id, op_id, RecoveryAction.CONTINUE_REVERT,
        )
    assert exc.value.code == UndoErrorCode.OPERATION_SCOPE_MISMATCH


def test_generic_command_rejected_during_freeze(env):
    checkpoint, f1, f2, token = _two_file_turn(env)
    _interrupt_mid_apply(env, checkpoint, token, f2)
    from core.workspace_context import set_workdir, clear_workdir

    set_workdir(str(env["ws"]))
    try:
        output = execute_command("echo hello")
        assert "撤销中断" in output or "拒绝" in output
    finally:
        clear_workdir()


def test_startup_scan_marks_interrupted_without_replay(env):
    checkpoint, f1, f2, token = _two_file_turn(env)
    _interrupt_mid_apply(env, checkpoint, token, f2)
    env["coordinator"].unfreeze_paths(
        checkpoint.workspace_root, [str(f1), str(f2)]
    )
    report = turn_undo_service.startup_recovery_scan()
    row = env["storage"].get_checkpoint(checkpoint.checkpoint_id)
    assert row.status_enum is TurnUndoStatus.INTERRUPTED
    assert f2.read_bytes() == b"new-2"
    assert env["coordinator"].is_path_frozen_any(str(f2))


def test_startup_scan_converts_stale_previewed_to_cancelled(env):
    checkpoint, f1, f2, token = _two_file_turn(env)
    report = turn_undo_service.startup_recovery_scan()
    assert len(report) == 1
    assert report[0]["to_status"] == OperationStatus.CANCELLED.value
    assert report[0]["operation_id"] == preview_operation_id(env, checkpoint)
    assert env["storage"].get_checkpoint(checkpoint.checkpoint_id).status_enum \
        is TurnUndoStatus.READY
    assert f1.read_bytes() == b"new-1"
    assert f2.read_bytes() == b"new-2"


def test_startup_scan_converting_boundaries(env):
    from core.turn_undo.models import OperationStatus as OS
    from core.turn_undo.models import OperationKind as OKind

    checkpoint, f1, f2, token = _two_file_turn(env)
    storage = env["storage"]
    op = storage.create_operation(
        checkpoint_id=checkpoint.checkpoint_id, kind=OKind.REVERT)
    storage.transition_checkpoint(checkpoint.checkpoint_id, TurnUndoStatus.REVERTING)
    storage.transition_operation(op.operation_id, OS.PREPARING)
    report = turn_undo_service.startup_recovery_scan()
    entry = [r for r in report if r["operation_id"] == op.operation_id]
    assert entry and entry[0]["to_status"] == OS.FAILED.value
    assert storage.get_checkpoint(checkpoint.checkpoint_id).status_enum \
        is TurnUndoStatus.FAILED
    assert not env["coordinator"].is_path_frozen_any(str(f1))
    preview, token2 = turn_undo_service.preview_turn_undo(
        env["session_id"], checkpoint.turn_id)
    assert token2
    result = turn_undo_service.execute_turn_undo(
        env["session_id"], checkpoint.turn_id, token2)
    assert result.status is TurnUndoStatus.REVERTED
    assert f1.read_bytes() == b"old-1" and f2.read_bytes() == b"old-2"


def test_startup_scan_verifying_boundary_marks_interrupted(env):
    from core.turn_undo.models import OperationStatus as OS
    from core.turn_undo.models import OperationKind as OKind

    checkpoint, f1, f2, token = _two_file_turn(env)
    storage = env["storage"]
    op = storage.create_operation(
        checkpoint_id=checkpoint.checkpoint_id, kind=OKind.REVERT)
    storage.transition_checkpoint(checkpoint.checkpoint_id, TurnUndoStatus.REVERTING)
    storage.transition_operation(op.operation_id, OS.PREPARING)
    storage.transition_operation(op.operation_id, OS.PREPARED)
    storage.transition_operation(op.operation_id, OS.APPLYING)
    storage.transition_operation(op.operation_id, OS.VERIFYING)
    report = turn_undo_service.startup_recovery_scan()
    entry = [r for r in report if r["operation_id"] == op.operation_id]
    assert entry and entry[0]["to_status"] == OS.INTERRUPTED.value
    assert storage.get_checkpoint(checkpoint.checkpoint_id).status_enum \
        is TurnUndoStatus.INTERRUPTED
    assert env["coordinator"].is_path_frozen_any(str(f1))
    assert f1.read_bytes() == b"new-1"


def test_different_workspaces_not_serialized(env):
    ws_a = env["tmp"] / "par-a"
    ws_b = env["tmp"] / "par-b"
    ws_a.mkdir()
    ws_b.mkdir()
    coordinator = env["coordinator"]
    gate_a = threading.Event()
    gate_b = threading.Event()
    order = []

    def hold_a():
        with coordinator.workspace_lock(str(ws_a)):
            order.append("a-enter")
            gate_a.set()
            gate_b.wait(timeout=10)
            order.append("a-exit")

    def hold_b():
        gate_a.wait(timeout=10)
        with coordinator.workspace_lock(str(ws_b)):
            order.append("b-enter")
            gate_b.set()

    ta = threading.Thread(target=hold_a)
    tb = threading.Thread(target=hold_b)
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)
    assert order == ["a-enter", "b-enter", "a-exit"] or order == [
        "a-enter", "b-enter", "b-enter", "a-exit"] or order[0:2] == ["a-enter", "b-enter"]


def test_subdir_write_serialized_with_undo_lock(env, tmp_path, monkeypatch):
    import threading as _threading
    from core.workspace_context import workspace_scope
    from core.tools import write_file

    monkeypatch.chdir(env["ws"])
    ws = env["ws"]
    sub = ws / "core"
    sub.mkdir(exist_ok=True)
    target = sub / "deep.py"
    target.write_bytes(b"old")

    coordinator = env["coordinator"]
    done = _threading.Event()
    holder = {}

    def _writer():
        with workspace_scope(str(ws)):
            holder["result"] = write_file(str(target), "new-content")
        done.set()

    with coordinator.workspace_lock(str(env["ws"])):
        t = _threading.Thread(target=_writer, daemon=True)
        t.start()
        t.join(timeout=0.8)
        assert not done.is_set(), "工作区根锁未阻塞子目录写入"
    t.join(timeout=10)
    assert done.is_set(), "释放锁后写入应完成"
    assert "写入成功" in (holder.get("result") or "")
    assert target.read_bytes() == b"new-content"


def test_startup_scan_wires_retention_expiry(env):
    from core.turn_undo.models import SnapshotAvailability

    for i in range(53):
        cp, ctx = _begin_turn(env, turn=f"t-ret-wire-{i}")
        env["storage"].transition_checkpoint(cp.checkpoint_id, TurnUndoStatus.READY)
        turn_undo_service.finish_turn(ctx)
        time.sleep(0.001)
    turn_undo_service.startup_recovery_scan()
    rows = env["storage"].list_checkpoints_for_session(env["session_id"])
    expired = [r for r in rows if r["snapshot_availability"] == SnapshotAvailability.EXPIRED.value]
    assert len(expired) >= 3
    for r in expired:
        assert r["status"] in ("ready", "reverted", "failed", "conflicted")


def test_startup_scan_reaps_stale_recording_checkpoint(env):
    from core.turn_undo.storage import now_ms

    cp, ctx = _begin_turn(env, turn="turn-crashed")
    with env["storage"]._manager._lock:
        env["storage"].conn.execute(
            "UPDATE turn_undo_checkpoints SET started_at_ms = ? WHERE checkpoint_id = ?",
            (now_ms() - 7 * 60 * 60 * 1000, cp.checkpoint_id),
        )
        env["storage"].conn.commit()
    tu_recorder.release_context(cp.checkpoint_id)
    env["coordinator"].unregister_active_turn(ctx.workspace_root, "turn-crashed")

    report = turn_undo_service.startup_recovery_scan()

    row = env["storage"].get_checkpoint(cp.checkpoint_id)
    assert row.status_enum is TurnUndoStatus.UNAVAILABLE
    assert row.unavailable_code == "stale_recording"
    assert any(
        item.get("reason") == "stale_recording"
        and item.get("checkpoint_id") == cp.checkpoint_id
        for item in report
    )


def test_startup_scan_keeps_fresh_recording_checkpoint(env):
    cp, ctx = _begin_turn(env, turn="turn-fresh")
    try:
        turn_undo_service.startup_recovery_scan()
        row = env["storage"].get_checkpoint(cp.checkpoint_id)
        assert row.status_enum is TurnUndoStatus.RECORDING
    finally:
        tu_recorder.release_context(cp.checkpoint_id)
        env["coordinator"].unregister_active_turn(ctx.workspace_root, "turn-fresh")
