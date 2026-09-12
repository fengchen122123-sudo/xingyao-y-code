"""turn_undo 崩溃中断与显式恢复测试：启动扫描、冻结、continue/cancel、recovery_required。"""

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
    """取该检查点最近的 revert 操作（恢复预检/执行的对象）。"""
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
    """执行撤销并在第二个文件应用前注入失败 → applying 中断。

    毒化只作用于该路径的第二次捕获（第一次是 CAS 前的整组指纹复核，
    必须保持真实），模拟"应用前发现文件被外部修改"。
    """
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


# ---------------------------------------------------------------------------
# 中断 → 冻结 → continue_revert
# ---------------------------------------------------------------------------

def test_interrupt_freezes_paths_and_continue_revert_completes(env, monkeypatch):
    monkeypatch.chdir(env["ws"])
    checkpoint, f1, f2, token = _two_file_turn(env)
    _interrupt_mid_apply(env, checkpoint, token, f2)

    row = env["storage"].get_checkpoint(checkpoint.checkpoint_id)
    assert row.status_enum is TurnUndoStatus.INTERRUPTED
    # 第一个文件已恢复到 old，第二个文件未触碰
    assert f1.read_bytes() == b"old-1"
    assert f2.read_bytes() == b"new-2"
    # 影响路径被冻结（含父/子判定）
    coordinator = env["coordinator"]
    assert coordinator.is_path_frozen_any(str(f1))
    assert coordinator.is_path_frozen_any(str(f2))
    sub = env["ws"] / "subdir" / "x.txt"
    assert coordinator.is_path_frozen_any(str(sub)) is False  # 无关路径不受影响

    # 冻结期间：文件工具拒绝、同工作区通用命令拒绝
    result = write_file(str(f1), "sneaky")
    assert "冻结" in result
    assert f1.read_bytes() == b"old-1"

    # 恢复预检：continue_revert
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
    # 冻结解除
    assert not coordinator.is_path_frozen_any(str(f1))
    # 父操作被原子解决
    parent_status = _parent_status(env, checkpoint)
    assert parent_status == OperationStatus.COMMITTED.value
    # 纠正记录存在
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
    # 取消撤销：回到用户点击撤销之前（两文件都是 new-*）
    assert result.checkpoint_status is TurnUndoStatus.READY
    assert f1.read_bytes() == b"new-1"
    assert f2.read_bytes() == b"new-2"
    assert _parent_status(env, checkpoint) == OperationStatus.COMPENSATED.value
    # 取消撤销不产生纠正记录
    assert env["storage"].list_corrections(checkpoint.workspace_root) == []
    # 检查点回到 ready，可重新正常撤销
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
    # 中断后外部修改了"未处理"文件 → 未知状态
    f2.write_bytes(b"external-after-crash")

    preview, rtoken = turn_undo_service.preview_turn_undo_recovery(
        env["session_id"], op_id, RecoveryAction.CONTINUE_REVERT,
    )
    assert preview.recovery_required is True
    assert "r2.py" in preview.unknown_files
    # 未知状态下不会签发恢复令牌，任何令牌都无法执行
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
    # 另一个会话尝试恢复 → operation_scope_mismatch
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
    # execute_command 在冻结工作区被拒（workdir 路由到冻结工作区）
    from core.workspace_context import set_workdir, clear_workdir

    set_workdir(str(env["ws"]))
    try:
        output = execute_command("echo hello")
        assert "撤销中断" in output or "拒绝" in output
    finally:
        clear_workdir()


# ---------------------------------------------------------------------------
# 启动扫描
# ---------------------------------------------------------------------------

def test_startup_scan_marks_interrupted_without_replay(env):
    checkpoint, f1, f2, token = _two_file_turn(env)
    _interrupt_mid_apply(env, checkpoint, token, f2)
    # 手动解冻模拟"进程重启后协调器状态丢失"
    env["coordinator"].unfreeze_paths(
        checkpoint.workspace_root, [str(f1), str(f2)]
    )
    # 重启扫描：已 interrupted 的操作保持，不重放写入
    report = turn_undo_service.startup_recovery_scan()
    row = env["storage"].get_checkpoint(checkpoint.checkpoint_id)
    assert row.status_enum is TurnUndoStatus.INTERRUPTED
    assert f2.read_bytes() == b"new-2"  # 不自动重放
    # 冻结按持久化状态重建
    assert env["coordinator"].is_path_frozen_any(str(f2))


def test_startup_scan_converts_stale_previewed_to_cancelled(env):
    checkpoint, f1, f2, token = _two_file_turn(env)
    # 预检后进程"重启"：previewed 操作的令牌跨 boot 失效 → 扫描收敛为 cancelled
    report = turn_undo_service.startup_recovery_scan()
    assert len(report) == 1
    assert report[0]["to_status"] == OperationStatus.CANCELLED.value
    assert report[0]["operation_id"] == preview_operation_id(env, checkpoint)
    # 检查点从未进入 reverting，保持 ready
    assert env["storage"].get_checkpoint(checkpoint.checkpoint_id).status_enum \
        is TurnUndoStatus.READY
    assert f1.read_bytes() == b"new-1"
    assert f2.read_bytes() == b"new-2"


def test_startup_scan_converting_boundaries(env):
    """状态边界崩溃注入的启动收敛：
    - preparing/prepared（未触盘）→ 操作 failed + 检查点 failed；
    - applying/verifying（磁盘不确定）→ 操作 interrupted + 检查点 interrupted + 冻结。
    """
    from core.turn_undo.models import OperationStatus as OS
    from core.turn_undo.models import OperationKind as OKind

    # --- preparing 边界：CAS 已提交但阶段化未完成 ---
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
    assert not env["coordinator"].is_path_frozen_any(str(f1))  # 未触盘不冻结
    # failed 检查点可重新预检恢复（failed -> ready）
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
    # 磁盘不确定 → 冻结重建，不自动重放
    assert env["coordinator"].is_path_frozen_any(str(f1))
    assert f1.read_bytes() == b"new-1"


def test_different_workspaces_not_serialized(env):
    """不同工作区的协调锁互不阻塞：可真实并行（规格 21）。"""
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
    # b 在 a 持锁期间就能进入自己的工作区锁
    assert order == ["a-enter", "b-enter", "a-exit"] or order == [
        "a-enter", "b-enter", "b-enter", "a-exit"] or order[0:2] == ["a-enter", "b-enter"]


def test_subdir_write_serialized_with_undo_lock(env, tmp_path, monkeypatch):
    """H1 回归：子目录文件的写入与撤销事务共用同一把工作区根锁。

    持有工作区根锁期间，子目录文件的 write_file 必须被阻塞；
    释放后才能完成（撤销应用区间与受控写入互斥，规格 21）。
    """
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
        # 持锁期间写入未完成（互斥生效）
        assert not done.is_set(), "工作区根锁未阻塞子目录写入"
    t.join(timeout=10)
    assert done.is_set(), "释放锁后写入应完成"
    assert "写入成功" in (holder.get("result") or "")
    assert target.read_bytes() == b"new-content"


def test_startup_scan_wires_retention_expiry(env):
    """M3 回归：启动扫描必须触发保留策略——超过每会话配额的旧检查点快照过期。"""
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
    # 生命周期事实不被覆盖
    for r in expired:
        assert r["status"] in ("ready", "reverted", "failed", "conflicted")
