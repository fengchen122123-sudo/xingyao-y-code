import hashlib
import time

import pytest

from core.memory import MemoryManager
from core.turn_undo import service as turn_undo_service
from core.turn_undo.models import (
    NodeKind,
    OperationStatus,
    OwnerKind,
    SnapshotAvailability,
    TurnUndoError,
    TurnUndoStatus,
    UndoErrorCode,
)
from core.turn_undo.storage import now_ms


@pytest.fixture()
def env(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / "data"))
    manager = MemoryManager(str(tmp_path / "ycode.db"))
    storage = turn_undo_service.configure_default_manager(
        manager, blob_root=tmp_path / "undo_blobs"
    )
    manager.add_messages("sess-tx", [("user", "hi")])
    session_id = storage.resolve_session_id("sess-tx")
    return {
        "ws": ws, "manager": manager, "storage": storage,
        "session_id": session_id, "tmp": tmp_path,
    }


def _begin_turn(env, turn="turn-tx-1"):
    checkpoint, ctx = turn_undo_service.begin_turn(
        session_id=env["session_id"],
        turn_id=turn,
        workspace_root=str(env["ws"]),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )
    return checkpoint, ctx


def _record_write(env, ctx, path, content, call="call-1"):
    from core.turn_undo import recorder as tu_recorder

    handle = tu_recorder.prepare_mutation(
        display_path=str(path),
        after_kind=NodeKind.REGULAR_FILE,
        after_content=content.encode("utf-8"),
        tool_name="write_file",
        tool_call_id=call,
    )
    path.write_text(content, encoding="utf-8", newline="")
    tu_recorder.verify_after_mutation(handle)


def _record_delete(env, ctx, path):
    from core.turn_undo import recorder as tu_recorder

    handle = tu_recorder.prepare_mutation(
        display_path=str(path),
        after_kind=NodeKind.MISSING,
        after_content=None,
        tool_name="execute_command",
        tool_call_id="call-del",
    )
    path.unlink()
    tu_recorder.verify_after_mutation(handle)


def _finish(env, ctx):
    return turn_undo_service.finish_turn(ctx)


def test_multi_file_undo_restores_all_files(env):
    ws = env["ws"]
    f1 = ws / "a.py"
    f2 = ws / "sub" / "b.txt"
    f2.parent.mkdir()
    f1.write_bytes(b"old-a")
    f2.write_bytes(b"old-b")
    created = ws / "new.txt"

    checkpoint, ctx = _begin_turn(env)
    _record_write(env, ctx, f1, "new-a", call="c1")
    _record_write(env, ctx, f2, "new-b", call="c2")
    _record_write(env, ctx, created, "created", call="c3")
    status = _finish(env, ctx)
    assert status is TurnUndoStatus.READY

    preview, token = turn_undo_service.preview_turn_undo(
        env["session_id"], checkpoint.turn_id
    )
    assert token
    assert preview.file_count == 3
    assert preview.delete_created_count == 1
    assert preview.restore_content_count == 2
    assert preview.created_files == ("new.txt",)

    result = turn_undo_service.execute_turn_undo(
        env["session_id"], checkpoint.turn_id, token
    )
    assert result.status is TurnUndoStatus.REVERTED
    assert not created.exists()
    assert f1.read_bytes() == b"old-a"
    assert f2.read_bytes() == b"old-b"
    row = env["storage"].get_checkpoint(checkpoint.checkpoint_id)
    assert row.status_enum is TurnUndoStatus.REVERTED
    assert row.reverted_at_ms is not None
    corrections = env["storage"].list_corrections(row.workspace_root)
    assert len(corrections) == 1
    assert corrections[0]["turn_id"] == checkpoint.turn_id


def test_undo_restores_deleted_file_from_snapshot(env):
    ws = env["ws"]
    doomed = ws / "doomed.txt"
    doomed.write_bytes(b"precious")
    checkpoint, ctx = _begin_turn(env)
    _record_delete(env, ctx, doomed)
    _finish(env, ctx)

    preview, token = turn_undo_service.preview_turn_undo(
        env["session_id"], checkpoint.turn_id
    )
    assert preview.restore_deleted_count == 1
    result = turn_undo_service.execute_turn_undo(
        env["session_id"], checkpoint.turn_id, token
    )
    assert result.status is TurnUndoStatus.REVERTED
    assert doomed.read_bytes() == b"precious"


def test_conflict_zero_writes_and_no_token(env):
    ws = env["ws"]
    f1 = ws / "conflict.py"
    f1.write_bytes(b"old")
    checkpoint, ctx = _begin_turn(env)
    _record_write(env, ctx, f1, "new", call="c1")
    _finish(env, ctx)

    f1.write_bytes(b"external-edit")
    preview, token = turn_undo_service.preview_turn_undo(
        env["session_id"], checkpoint.turn_id
    )
    assert token == ""
    assert len(preview.conflicts) == 1
    conflict = preview.conflicts[0]
    assert conflict.expected.sha256 == hashlib.sha256(b"new").hexdigest()
    assert conflict.actual.sha256 == hashlib.sha256(b"external-edit").hexdigest()
    assert env["storage"].get_checkpoint(checkpoint.checkpoint_id).status_enum \
        is TurnUndoStatus.CONFLICTED
    assert f1.read_bytes() == b"external-edit"

    f1.write_bytes(b"new")
    preview2, token2 = turn_undo_service.preview_turn_undo(
        env["session_id"], checkpoint.turn_id
    )
    assert token2
    assert env["storage"].get_checkpoint(checkpoint.checkpoint_id).status_enum \
        is TurnUndoStatus.READY
    result = turn_undo_service.execute_turn_undo(
        env["session_id"], checkpoint.turn_id, token2
    )
    assert result.status is TurnUndoStatus.REVERTED
    assert f1.read_bytes() == b"old"


def test_change_between_preview_and_execute_rejected(env):
    ws = env["ws"]
    f1 = ws / "race.py"
    f1.write_bytes(b"old")
    checkpoint, ctx = _begin_turn(env)
    _record_write(env, ctx, f1, "new", call="c1")
    _finish(env, ctx)

    _, token = turn_undo_service.preview_turn_undo(env["session_id"], checkpoint.turn_id)
    f1.write_bytes(b"sneaky-edit")
    with pytest.raises(TurnUndoError) as exc:
        turn_undo_service.execute_turn_undo(env["session_id"], checkpoint.turn_id, token)
    assert exc.value.code == UndoErrorCode.FILE_CONFLICT
    assert f1.read_bytes() == b"sneaky-edit"
    assert env["storage"].get_checkpoint(checkpoint.checkpoint_id).status_enum \
        is TurnUndoStatus.CONFLICTED


def test_expired_token_rejected(env):
    ws = env["ws"]
    f1 = ws / "tok.py"
    f1.write_bytes(b"old")
    checkpoint, ctx = _begin_turn(env)
    _record_write(env, ctx, f1, "new")
    _finish(env, ctx)
    preview, token = turn_undo_service.preview_turn_undo(
        env["session_id"], checkpoint.turn_id, token_ttl_ms=1,
    )
    time.sleep(0.05)
    with pytest.raises(TurnUndoError) as exc:
        turn_undo_service.execute_turn_undo(env["session_id"], checkpoint.turn_id, token)
    assert exc.value.code == UndoErrorCode.TOKEN_EXPIRED


def test_token_from_other_turn_rejected(env):
    ws = env["ws"]
    f1 = ws / "x.py"
    f1.write_bytes(b"old")
    cp1, ctx1 = _begin_turn(env, turn="turn-a")
    _record_write(env, ctx1, f1, "new-a")
    _finish(env, ctx1)
    _, token_a = turn_undo_service.preview_turn_undo(env["session_id"], "turn-a")

    cp2, ctx2 = _begin_turn(env, turn="turn-b")
    _finish(env, ctx2)
    with pytest.raises(TurnUndoError):
        turn_undo_service.execute_turn_undo(env["session_id"], "turn-b", token_a)


def test_reverted_repeat_call_is_idempotent(env):
    ws = env["ws"]
    f1 = ws / "idem.py"
    f1.write_bytes(b"old")
    checkpoint, ctx = _begin_turn(env)
    _record_write(env, ctx, f1, "new")
    _finish(env, ctx)
    _, token = turn_undo_service.preview_turn_undo(env["session_id"], checkpoint.turn_id)
    first = turn_undo_service.execute_turn_undo(env["session_id"], checkpoint.turn_id, token)
    assert first.status is TurnUndoStatus.REVERTED
    second = turn_undo_service.execute_turn_undo(
        env["session_id"], checkpoint.turn_id, "stale-token"
    )
    assert second.idempotent is True
    assert second.original_reverted_at_ms == first.reverted_at_ms
    assert f1.read_bytes() == b"old"


def test_active_turn_blocks_undo(env):
    ws = env["ws"]
    f1 = ws / "busy.py"
    f1.write_bytes(b"old")
    cp_done, ctx_done = _begin_turn(env, turn="turn-done")
    _record_write(env, ctx_done, f1, "new")
    _finish(env, ctx_done)
    _, token = turn_undo_service.preview_turn_undo(env["session_id"], "turn-done")

    checkpoint, ctx = _begin_turn(env, turn="turn-running")
    try:
        with pytest.raises(TurnUndoError) as exc:
            turn_undo_service.execute_turn_undo(env["session_id"], "turn-done", token)
        assert exc.value.code == UndoErrorCode.WORKSPACE_BUSY
    finally:
        _finish(env, ctx)
    assert f1.read_bytes() == b"new"


def test_cross_workspace_isolation(env, tmp_path):
    other_ws = env["tmp"] / "other-ws"
    other_ws.mkdir()
    f1 = env["ws"] / "mine.py"
    f1.write_bytes(b"old")
    checkpoint, ctx = _begin_turn(env)
    _record_write(env, ctx, f1, "new")
    _finish(env, ctx)
    _, token = turn_undo_service.preview_turn_undo(env["session_id"], checkpoint.turn_id)
    turn_undo_service.execute_turn_undo(env["session_id"], checkpoint.turn_id, token)
    assert env["storage"].list_corrections(checkpoint.workspace_root)
    assert env["storage"].list_corrections(str(other_ws)) == []


def test_preview_counts_and_summary(env):
    ws = env["ws"]
    for name in ("p1.py", "p2.py", "p3.py", "p4.py"):
        (ws / name).write_bytes(b"old")
    created = ws / "created.bin"
    checkpoint, ctx = _begin_turn(env)
    for name in ("p1.py", "p2.py", "p3.py", "p4.py"):
        _record_write(env, ctx, ws / name, "new-" + name)
    _record_write(env, ctx, created, "data")
    _finish(env, ctx)
    preview, _ = turn_undo_service.preview_turn_undo(env["session_id"], checkpoint.turn_id)
    assert preview.file_count == 5
    assert preview.restore_content_count == 4
    assert preview.delete_created_count == 1
    assert preview.has_uncovered_command_effects is False
    assert preview.subsequent_completed_turns == 0


def test_subsequent_completed_turns_warned(env):
    ws = env["ws"]
    f1 = ws / "dep.py"
    f2 = ws / "later.py"
    f1.write_bytes(b"old")
    cp1, ctx1 = _begin_turn(env, turn="turn-early")
    _record_write(env, ctx1, f1, "v1")
    _finish(env, ctx1)
    time.sleep(0.005)
    cp2, ctx2 = _begin_turn(env, turn="turn-late")
    _record_write(env, ctx2, f2, "later-content")
    _finish(env, ctx2)
    preview, token = turn_undo_service.preview_turn_undo(env["session_id"], "turn-early")
    assert preview.subsequent_completed_turns >= 1
    result = turn_undo_service.execute_turn_undo(env["session_id"], "turn-early", token)
    assert result.status is TurnUndoStatus.REVERTED
    assert f1.read_bytes() == b"old"
    corr = env["storage"].list_corrections(cp1.workspace_root)
    assert corr and corr[0]["subsequent_completed_turns"] >= 1


def test_later_turn_same_file_blocks_early_undo(env):
    ws = env["ws"]
    f1 = ws / "same.py"
    f1.write_bytes(b"old")
    cp1, ctx1 = _begin_turn(env, turn="turn-e1")
    _record_write(env, ctx1, f1, "v1")
    _finish(env, ctx1)
    cp2, ctx2 = _begin_turn(env, turn="turn-e2")
    _record_write(env, ctx2, f1, "v2")
    _finish(env, ctx2)
    preview, token = turn_undo_service.preview_turn_undo(env["session_id"], "turn-e1")
    assert token == "" and preview.conflicts
    assert f1.read_bytes() == b"v2"


def test_preview_skips_conflicted_file_and_still_undoes_others(env):
    from core.turn_undo import recorder as tu_recorder

    ws = env["ws"]
    f1 = ws / "skip.txt"
    f2 = ws / "keep.txt"
    f1.write_bytes(b"old-skip")
    f2.write_bytes(b"old-keep")

    checkpoint, ctx = _begin_turn(env, turn="turn-tx-skip")
    _record_write(env, ctx, f1, "new-skip", call="c1")
    _record_write(env, ctx, f2, "new-keep", call="c2")
    f1.write_bytes(b"external")
    handle = tu_recorder.prepare_mutation(
        display_path=str(f1),
        after_kind=NodeKind.REGULAR_FILE,
        after_content=b"after-skip",
        tool_name="write_file",
        tool_call_id="c3",
    )
    f1.write_bytes(b"after-skip")
    tu_recorder.verify_after_mutation(handle)
    assert _finish(env, ctx) is TurnUndoStatus.READY

    preview, token = turn_undo_service.preview_turn_undo(
        env["session_id"], "turn-tx-skip"
    )
    assert token, "有可撤销文件时仍应签发令牌"
    assert preview.skipped_files == ("skip.txt",)
    assert preview.file_count == 1
    assert preview.restore_content_count == 1

    turn_undo_service.execute_turn_undo(env["session_id"], "turn-tx-skip", token)
    assert f2.read_bytes() == b"old-keep", "可撤销文件必须还原"
    assert f1.read_bytes() == b"after-skip", "skipped 文件不得被触碰"
