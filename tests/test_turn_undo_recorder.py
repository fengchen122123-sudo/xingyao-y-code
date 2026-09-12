"""turn_undo FileMutationRecorder 测试：记录契约、验证、节点拒绝与回合封闭。"""

import hashlib
import os
import stat

import pytest

from core.memory import MemoryManager
from core.turn_undo import recorder
from core.turn_undo.models import (
    NodeKind,
    OwnerKind,
    SourceKind,
    TurnUndoError,
    TurnUndoStatus,
    UndoErrorCode,
)
from core.turn_undo.service import configure_default_manager


@pytest.fixture()
def env(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    manager = MemoryManager(str(tmp_path / "ycode.db"))
    storage = configure_default_manager(manager, blob_root=tmp_path / "blobs")
    monkeypatch.setattr(storage, "blob_store", storage.blob_store)
    return {"ws": ws, "manager": manager, "storage": storage, "tmp": tmp_path}


def _skip_without_symlink_privilege(exc):
    import pytest

    pytest.skip(f"当前环境无法创建符号链接: {exc}")


def _begin_turn(env, session="sess-rec", turn="turn-rec-1"):
    manager = env["manager"]
    manager.add_messages(session, [("user", "hi")])
    session_id = env["storage"].resolve_session_id(session)
    cp = env["storage"].create_checkpoint(
        session_id=session_id,
        turn_id=turn,
        workspace_root=str(env["ws"]),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )
    ctx = recorder.begin_recording(env["storage"], cp)
    return cp, ctx


def _prepare(env, path, content, tool="write_file", call="call-1", after_kind=None,
             source_kind=None, source_id=None):
    return recorder.prepare_mutation(
        display_path=str(path),
        after_kind=after_kind or NodeKind.REGULAR_FILE,
        after_content=content if (after_kind or NodeKind.REGULAR_FILE)
        is not NodeKind.MISSING else None,
        tool_name=tool,
        tool_call_id=call,
        source_kind=source_kind,
        source_id=source_id,
    )


# ---------------------------------------------------------------------------
# 无检查点上下文 / 封闭检查点
# ---------------------------------------------------------------------------

def test_prepare_without_context_fails_closed(env):
    """线程无检查点上下文（回合外调用）：no_active_checkpoint，由工具层降级。"""
    with pytest.raises(TurnUndoError) as exc:
        _prepare(env, env["ws"] / "a.txt", b"x")
    assert exc.value.code == UndoErrorCode.NO_ACTIVE_CHECKPOINT
    assert not (env["ws"] / "a.txt").exists()


def test_late_write_with_bound_thread_refused_before_disk(env):
    """H2 核心：子代理线程仍绑定已封闭检查点时，写入在触盘前拒绝。

    模拟协作式取消场景——父回合已 finish_turn，子代理线程 TLS 仍持有
    旧上下文对象；write 触盘前必须失败，文件不变。
    """
    import threading as _threading

    target = env["ws"] / "late-bound.txt"
    cp, ctx = _begin_turn(env, turn="turn-late-bound")

    bound = _threading.Event()
    parent_closed = _threading.Event()
    result = {}

    def _stuck_subagent():
        # 模拟前台子代理线程：绑定父回合后仍卡在执行中
        assert recorder.bind_thread(cp.checkpoint_id, owner=False)
        bound.set()
        assert parent_closed.wait(timeout=10)
        try:
            recorder.prepare_mutation(
                display_path=str(target),
                after_kind=NodeKind.REGULAR_FILE,
                after_content=b"late",
                tool_name="write_file",
                tool_call_id="late",
            )
            result["unexpected"] = True
        except TurnUndoError as error:
            result["code"] = error.code
        finally:
            recorder.unbind_thread()

    t = _threading.Thread(target=_stuck_subagent, daemon=True)
    t.start()
    assert bound.wait(timeout=10)
    recorder.close_turn(ctx)  # 父回合封闭（不触碰 target）
    parent_closed.set()
    t.join(timeout=10)

    assert result.get("code") == UndoErrorCode.CHECKPOINT_CLOSED
    assert not target.exists(), "迟到写入必须在触盘前失败"


def test_late_write_after_close_rejected_before_disk(env):
    """上下文被注销后（正常收尾路径）：no_active_checkpoint，工具层降级为
    不可撤销写入；而 TLS仍绑定的迟到线程走 CHECKPOINT_CLOSED 拒绝（见上）。"""
    cp, ctx = _begin_turn(env)
    recorder.close_turn(ctx)  # no_changes
    with pytest.raises(TurnUndoError) as exc:
        _prepare(env, env["ws"] / "late.txt", b"x")
    assert exc.value.code == UndoErrorCode.NO_ACTIVE_CHECKPOINT
    assert not (env["ws"] / "late.txt").exists()


def test_bind_thread_allows_subagent_recording(env):
    cp, ctx = _begin_turn(env)
    assert recorder.bind_thread(cp.checkpoint_id, owner=False) is True
    handle = _prepare(env, env["ws"] / "sub.txt", b"from-sub",
                      source_kind=SourceKind.SUBAGENT, source_id="task-1")
    assert handle.checkpoint_id == cp.checkpoint_id
    recorder.unbind_thread()
    assert recorder.current_context() is None
    recorder.release_context(cp.checkpoint_id)


def test_bind_thread_unknown_checkpoint(env):
    assert recorder.bind_thread("ckpt-none", owner=False) is False


# ---------------------------------------------------------------------------
# prepare/verify 契约
# ---------------------------------------------------------------------------

def test_prepare_persists_blobs_and_row_before_write(env):
    target = env["ws"] / "new.txt"
    cp, ctx = _begin_turn(env)
    handle = _prepare(env, target, b"hello undo")
    # blob 已持久化
    after_sha = hashlib.sha256(b"hello undo").hexdigest()
    assert env["storage"].blob_store.exists(after_sha)
    row = env["storage"].get_file(handle.file_id)
    assert row.canonical_path == recorder.canonicalize_path(str(target))
    assert row.display_path == "new.txt"
    assert row.before_kind == NodeKind.MISSING.value
    assert row.after_sha256 == after_sha
    assert row.verified_at_ms is None
    # 目标文件在 prepare 阶段还没被写
    assert not target.exists()
    # 写入 + verify
    target.write_bytes(b"hello undo")
    actual = recorder.verify_after_mutation(handle)
    assert actual.sha256 == after_sha
    row = env["storage"].get_file(handle.file_id)
    assert row.verified_at_ms is not None
    assert row.after_file_identity is not None
    recorder.release_context(cp.checkpoint_id)


def test_prepare_captures_original_before_bytes(env):
    target = env["ws"] / "edit.txt"
    target.write_bytes(b"original-bytes-\xff\xfe")
    cp, ctx = _begin_turn(env)
    handle = _prepare(env, target, b"replaced")
    row = env["storage"].get_file(handle.file_id)
    before_sha = hashlib.sha256(b"original-bytes-\xff\xfe").hexdigest()
    assert row.before_sha256 == before_sha
    assert row.before_blob_sha256 == before_sha
    assert row.before_file_identity is not None
    assert row.before_link_count == 1
    recorder.release_context(cp.checkpoint_id)


def test_second_mutation_keeps_before_replaces_after(env):
    target = env["ws"] / "multi.txt"
    target.write_bytes(b"v0")
    cp, ctx = _begin_turn(env)
    h1 = _prepare(env, target, b"v1", call="call-1")
    target.write_bytes(b"v1")
    recorder.verify_after_mutation(h1)
    h2 = _prepare(env, target, b"v2", call="call-2")
    assert h2.file_id == h1.file_id
    assert h2.is_first_mutation is False
    target.write_bytes(b"v2")
    recorder.verify_after_mutation(h2)
    row = env["storage"].get_file(h2.file_id)
    assert row.before_sha256 == hashlib.sha256(b"v0").hexdigest()
    assert row.after_sha256 == hashlib.sha256(b"v2").hexdigest()
    assert row.tool_call_id == "call-2"
    recorder.release_context(cp.checkpoint_id)


def test_verify_mismatch_marks_unavailable_and_raises(env):
    target = env["ws"] / "race.txt"
    cp, ctx = _begin_turn(env)
    handle = _prepare(env, target, b"planned")
    # 验证前被外部改变
    target.write_bytes(b"external!")
    with pytest.raises(TurnUndoError) as exc:
        recorder.verify_after_mutation(handle)
    assert exc.value.code == UndoErrorCode.FILE_CONFLICT
    cp_row = env["storage"].get_checkpoint(cp.checkpoint_id)
    assert cp_row.status_enum is TurnUndoStatus.UNAVAILABLE
    # 目标文件未被记录器触碰（仍是外部内容）
    assert target.read_bytes() == b"external!"
    recorder.release_context(cp.checkpoint_id)


def test_verify_missing_target_for_delete_plan(env):
    target = env["ws"] / "gone.txt"
    target.write_bytes(b"to-be-deleted")
    cp, ctx = _begin_turn(env)
    handle = _prepare(env, target, None, after_kind=NodeKind.MISSING)
    target.unlink()
    recorder.verify_after_mutation(handle)
    row = env["storage"].get_file(handle.file_id)
    assert row.after_kind == NodeKind.MISSING.value
    assert row.after_exists == 0
    assert row.verified_at_ms is not None
    recorder.release_context(cp.checkpoint_id)


def test_delete_plan_but_file_still_exists_fails(env):
    target = env["ws"] / "still.txt"
    target.write_bytes(b"data")
    cp, ctx = _begin_turn(env)
    handle = _prepare(env, target, None, after_kind=NodeKind.MISSING)
    with pytest.raises(TurnUndoError) as exc:
        recorder.verify_after_mutation(handle)
    assert exc.value.code == UndoErrorCode.FILE_CONFLICT
    recorder.release_context(cp.checkpoint_id)


def test_prepare_with_snapshot_failure_blocks_mutation(env):
    target = env["ws"] / "snap-fail.txt"
    target.write_bytes(b"keep me")
    cp, ctx = _begin_turn(env)

    def boom(data=None):
        raise TurnUndoError(UndoErrorCode.IO_ERROR, "磁盘已满（模拟）")

    import core.turn_undo.storage as storage_mod

    original = env["storage"].blob_store.put_bytes
    env["storage"].blob_store.put_bytes = lambda data: boom(data)
    try:
        with pytest.raises(TurnUndoError):
            _prepare(env, target, b"new content")
    finally:
        env["storage"].blob_store.put_bytes = original
    # 快照失败：文件内容不变
    assert target.read_bytes() == b"keep me"
    assert env["storage"].list_files(cp.checkpoint_id) == []
    recorder.release_context(cp.checkpoint_id)


# ---------------------------------------------------------------------------
# 严格节点边界
# ---------------------------------------------------------------------------

def test_symlink_target_rejected(env):
    real = env["ws"] / "real.txt"
    real.write_bytes(b"real")
    link = env["ws"] / "link.txt"
    try:
        os.symlink(real, link)
    except OSError as _e:
        _skip_without_symlink_privilege(_e)
    cp, ctx = _begin_turn(env)
    with pytest.raises(TurnUndoError) as exc:
        _prepare(env, link, b"x")
    assert exc.value.code == UndoErrorCode.UNSUPPORTED_NODE_KIND
    recorder.release_context(cp.checkpoint_id)


def test_directory_target_rejected(env):
    d = env["ws"] / "adir"
    d.mkdir()
    cp, ctx = _begin_turn(env)
    with pytest.raises(TurnUndoError) as exc:
        _prepare(env, d, b"x")
    assert exc.value.code == UndoErrorCode.UNSUPPORTED_NODE_KIND
    recorder.release_context(cp.checkpoint_id)


def test_hardlink_rejected(env):
    base = env["ws"] / "base.txt"
    base.write_bytes(b"shared")
    link = env["ws"] / "hard.txt"
    os.link(base, link)
    assert stat.S_ISREG(os.lstat(str(link)).st_mode)
    cp, ctx = _begin_turn(env)
    with pytest.raises(TurnUndoError) as exc:
        _prepare(env, link, b"x")
    assert exc.value.code == UndoErrorCode.UNSUPPORTED_NODE_KIND
    recorder.release_context(cp.checkpoint_id)


def test_intermediate_symlink_directories_are_resolved(env):
    real_dir = env["ws"] / "realdir"
    real_dir.mkdir()
    link_dir = env["ws"] / "linkdir"
    try:
        os.symlink(real_dir, link_dir)
    except OSError as _e:
        _skip_without_symlink_privilege(_e)
    cp, ctx = _begin_turn(env)
    handle = _prepare(env, link_dir / "file.txt", b"through-link")
    row = env["storage"].get_file(handle.file_id)
    assert row.canonical_path == recorder.canonicalize_path(str(real_dir / "file.txt"))
    recorder.release_context(cp.checkpoint_id)


def test_prepare_rejects_directory_after_kind(env):
    cp, ctx = _begin_turn(env)
    with pytest.raises(TurnUndoError) as exc:
        recorder.prepare_mutation(
            display_path=str(env["ws"] / "x"),
            after_kind=NodeKind.DIRECTORY,
            after_content=None,
            tool_name="write_file",
            tool_call_id="c",
        )
    assert exc.value.code == UndoErrorCode.UNSUPPORTED_NODE_KIND
    recorder.release_context(cp.checkpoint_id)


# ---------------------------------------------------------------------------
# 回合封闭
# ---------------------------------------------------------------------------

def test_close_turn_ready_with_net_stats(env):
    target = env["ws"] / "stats.txt"
    target.write_bytes(b"line1\nline2\n")
    cp, ctx = _begin_turn(env)
    h = _prepare(env, target, b"line1\nline2\nline3\nline4\n")
    target.write_bytes(b"line1\nline2\nline3\nline4\n")
    recorder.verify_after_mutation(h)
    status = recorder.close_turn(ctx)
    assert status is TurnUndoStatus.READY
    cp_row = env["storage"].get_checkpoint(cp.checkpoint_id)
    assert cp_row.completed_at_ms is not None
    row = env["storage"].list_files(cp.checkpoint_id)[0]
    assert row.net_added == 2 and row.net_removed == 0
    assert row.diff_kind == "text"
    assert recorder.current_context() is None


def test_close_turn_no_changes(env):
    cp, ctx = _begin_turn(env)
    status = recorder.close_turn(ctx)
    assert status is TurnUndoStatus.NO_CHANGES
    cp_row = env["storage"].get_checkpoint(cp.checkpoint_id)
    from core.turn_undo.models import SnapshotAvailability

    assert cp_row.availability_enum is SnapshotAvailability.NOT_REQUIRED


def test_close_turn_binary_diff(env):
    target = env["ws"] / "blob.bin"
    target.write_bytes(b"\x00\x01")
    cp, ctx = _begin_turn(env)
    h = _prepare(env, target, b"\x00\x01\x02\xff")
    target.write_bytes(b"\x00\x01\x02\xff")
    recorder.verify_after_mutation(h)
    recorder.close_turn(ctx)
    row = env["storage"].list_files(cp.checkpoint_id)[0]
    assert row.diff_kind == "binary"
    assert row.net_added == 0 and row.net_removed == 0


def test_unverified_record_makes_turn_unavailable(env):
    target = env["ws"] / "unver.txt"
    cp, ctx = _begin_turn(env)
    _prepare(env, target, b"data")  # prepare 后未写入也未 verify
    status = recorder.close_turn(ctx)
    assert status is TurnUndoStatus.UNAVAILABLE
    cp_row = env["storage"].get_checkpoint(cp.checkpoint_id)
    assert cp_row.unavailable_code == UndoErrorCode.CHECKPOINT_UNAVAILABLE


def test_mid_turn_external_change_detected_at_prepare(env):
    target = env["ws"] / "ext.txt"
    target.write_bytes(b"v0")
    cp, ctx = _begin_turn(env)
    h1 = _prepare(env, target, b"v1", call="call-1")
    target.write_bytes(b"v1")
    recorder.verify_after_mutation(h1)
    # 外部修改后再 prepare 同一文件 → 拒绝并标记不可撤销
    target.write_bytes(b"external")
    with pytest.raises(TurnUndoError) as exc:
        _prepare(env, target, b"v2", call="call-2")
    assert exc.value.code == UndoErrorCode.FILE_CONFLICT
    assert env["storage"].get_checkpoint(cp.checkpoint_id).status_enum \
        is TurnUndoStatus.UNAVAILABLE
    recorder.release_context(cp.checkpoint_id)


# ---------------------------------------------------------------------------
# 通用命令副作用标记
# ---------------------------------------------------------------------------

def test_mark_uncovered_command_effects(env):
    cp, ctx = _begin_turn(env)
    recorder.mark_uncovered_command_effects("execute_command")
    assert env["storage"].get_checkpoint(cp.checkpoint_id).has_uncovered_command_effects == 1
    recorder.mark_uncovered_command_effects("write_file")  # 非命令不重复标记
    recorder.close_turn(ctx)


def test_mark_uncovered_without_context_noop(env):
    recorder.mark_uncovered_command_effects("execute_command")  # 不应抛异常


# ---------------------------------------------------------------------------
# 符号链接拒绝逻辑（纯逻辑层，不依赖 Windows 符号链接特权）
# ---------------------------------------------------------------------------

def test_reject_unsupported_rejects_symlink_node_kind():
    from core.turn_undo.models import NodeState

    with pytest.raises(TurnUndoError) as exc:
        recorder._reject_unsupported(
            NodeState(kind=NodeKind.SYMLINK, exists=True, symlink_target="C:/x"),
            "/ws/link.txt",
        )
    assert exc.value.code == UndoErrorCode.UNSUPPORTED_NODE_KIND


def test_reject_unsupported_rejects_directory_and_special():
    from core.turn_undo.models import NodeState

    for kind in (NodeKind.DIRECTORY, NodeKind.SPECIAL):
        with pytest.raises(TurnUndoError) as exc:
            recorder._reject_unsupported(
                NodeState(kind=kind, exists=True), "/ws/node",
            )
        assert exc.value.code == UndoErrorCode.UNSUPPORTED_NODE_KIND


def test_reject_unsupported_allows_missing_and_regular_and_multi_hardlink():
    from core.turn_undo.models import NodeState

    # missing 与普通文件放行
    recorder._reject_unsupported(NodeState(kind=NodeKind.MISSING, exists=False), "/ws/a")
    recorder._reject_unsupported(
        NodeState(kind=NodeKind.REGULAR_FILE, exists=True, link_count=1), "/ws/a",
    )
    # 多硬链接拒绝（真实 os.link路径已由 test_hardlink_rejected 覆盖，
    # 此处验证 nlink>1 判定本身）
    with pytest.raises(TurnUndoError) as exc:
        recorder._reject_unsupported(
            NodeState(kind=NodeKind.REGULAR_FILE, exists=True, link_count=3),
            "/ws/a",
        )
    assert exc.value.code == UndoErrorCode.UNSUPPORTED_NODE_KIND


def test_prepare_rejects_via_capture_poisoned_symlink(env, monkeypatch):
    """prepare_mutation 对"捕获结果为符号链接"的路径拒绝写入（无真实 symlink 环境）。"""
    target = env["ws"] / "poison.txt"

    original = recorder.capture_node_state

    def poisoned(path):
        from core.turn_undo.models import NodeState

        if recorder.canonicalize_path(str(path)) == recorder.canonicalize_path(str(target)):
            return NodeState(
                kind=NodeKind.SYMLINK, exists=True, symlink_target="C:/elsewhere",
            )
        return original(path)

    cp, ctx = _begin_turn(env, turn="turn-symlink-poison")
    monkeypatch.setattr(recorder, "capture_node_state", poisoned)
    try:
        with pytest.raises(TurnUndoError) as exc:
            recorder.prepare_mutation(
                display_path=str(target),
                after_kind=NodeKind.REGULAR_FILE,
                after_content=b"x",
                tool_name="write_file",
                tool_call_id="c",
            )
        assert exc.value.code == UndoErrorCode.UNSUPPORTED_NODE_KIND
    finally:
        monkeypatch.undo()
        recorder.release_context(cp.checkpoint_id)
