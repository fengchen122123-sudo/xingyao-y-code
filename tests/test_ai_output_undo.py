import os
from pathlib import Path

import pytest

from core import ai_output_paths as aop
from core.memory import MemoryManager
from core.turn_undo import service as turn_undo_service
from core.turn_undo.models import NodeKind, OwnerKind, TurnUndoStatus


@pytest.fixture()
def env(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    ai_dir = tmp_path / "ai_out"
    monkeypatch.setattr(aop, "_read_setting", lambda: str(ai_dir))
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / "data"))
    manager = MemoryManager(str(tmp_path / "ycode.db"))
    storage = turn_undo_service.configure_default_manager(
        manager, blob_root=tmp_path / "undo_blobs"
    )
    manager.add_messages("sess-ai", [("user", "帮我生成一张图")])
    return {
        "project": project,
        "ai_dir": ai_dir,
        "storage": storage,
        "session_id": storage.resolve_session_id("sess-ai"),
        "tmp": tmp_path,
    }


def _begin_turn(env, turn="turn-ai-1"):
    checkpoint, ctx = turn_undo_service.begin_turn(
        session_id=env["session_id"],
        turn_id=turn,
        workspace_root=str(env["project"]),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )
    return checkpoint, ctx


def _record_write(path: Path, content: bytes, call="call-1"):
    from core.turn_undo import recorder as tu_recorder

    handle = tu_recorder.prepare_mutation(
        display_path=str(path),
        after_kind=NodeKind.REGULAR_FILE,
        after_content=content,
        tool_name="write_file",
        tool_call_id=call,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    tu_recorder.verify_after_mutation(handle)


def test_ai_output_file_is_tracked_by_turn_undo(env):
    from core.turn_undo.recorder import canonicalize_path, display_path_for

    ai_file = env["ai_dir"] / "hunyuan_20260920_120000.jpg"
    ai_file.parent.mkdir(parents=True, exist_ok=True)
    ai_file.write_bytes(b"\xff\xd8\xff")

    canonical = canonicalize_path(str(ai_file))
    display = display_path_for(str(env["project"]), canonical)

    assert Path(canonical).is_absolute(), canonical
    assert Path(display).is_absolute(), display
    assert os.path.normcase(display) == canonical, "工作区外路径应原样返回规范化绝对路径"
    assert canonicalize_path(display) == canonical, "规范化必须可无损往返"


def test_ai_output_dir_is_outside_workspace(env):
    ai_file = env["ai_dir"] / "probe.png"
    ai_file.parent.mkdir(parents=True, exist_ok=True)
    ai_file.write_bytes(b"x")

    from core.turn_undo.recorder import canonicalize_path, display_path_for

    canonical = canonicalize_path(str(ai_file))
    display = display_path_for(str(env["project"]), canonical)
    assert display == canonical, "位于工作区内时这里会是相对路径，本测试失去意义"


def test_ai_output_artifact_created_in_turn_is_deleted_on_undo(env):
    ai_file = env["ai_dir"] / "download_20260920_120000.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    checkpoint, ctx = _begin_turn(env)
    _record_write(ai_file, payload)
    assert turn_undo_service.finish_turn(ctx) is TurnUndoStatus.READY

    preview, token = turn_undo_service.preview_turn_undo(
        env["session_id"], checkpoint.turn_id
    )
    assert token
    assert preview.file_count == 1
    assert preview.delete_created_count == 1
    assert len(preview.created_files) == 1
    shown = preview.created_files[0]
    assert Path(shown).is_absolute(), f"工作区外产物必须显示为绝对路径: {shown}"
    assert os.path.normcase(shown) == os.path.normcase(str(ai_file))

    result = turn_undo_service.execute_turn_undo(
        env["session_id"], checkpoint.turn_id, token
    )
    assert result.status is TurnUndoStatus.REVERTED
    assert not ai_file.exists(), "撤销后 AI 目录内的本回合产物必须被删除"


def test_ai_output_artifact_overwritten_in_turn_is_restored_on_undo(env):
    ai_file = env["ai_dir"] / "hunyuan_20260920_120000.jpg"
    ai_file.parent.mkdir(parents=True, exist_ok=True)
    ai_file.write_bytes(b"old-image")

    checkpoint, ctx = _begin_turn(env)
    _record_write(ai_file, b"new-image")
    assert turn_undo_service.finish_turn(ctx) is TurnUndoStatus.READY

    preview, token = turn_undo_service.preview_turn_undo(
        env["session_id"], checkpoint.turn_id
    )
    assert token
    assert preview.restore_content_count == 1

    result = turn_undo_service.execute_turn_undo(
        env["session_id"], checkpoint.turn_id, token
    )
    assert result.status is TurnUndoStatus.REVERTED
    assert ai_file.read_bytes() == b"old-image"


def test_ai_output_artifact_tracked_alongside_workspace_file(env):
    ws_file = env["project"] / "notes.md"
    ai_file = env["ai_dir"] / "download_20260920_130000.png"

    checkpoint, ctx = _begin_turn(env, turn="turn-ai-mixed")
    _record_write(ws_file, b"project-note", call="c1")
    _record_write(ai_file, b"\x89PNG\r\n\x1a\n", call="c2")
    assert turn_undo_service.finish_turn(ctx) is TurnUndoStatus.READY

    preview, token = turn_undo_service.preview_turn_undo(
        env["session_id"], checkpoint.turn_id
    )
    assert token
    assert preview.file_count == 2

    result = turn_undo_service.execute_turn_undo(
        env["session_id"], checkpoint.turn_id, token
    )
    assert result.status is TurnUndoStatus.REVERTED
    assert not ws_file.exists()
    assert not ai_file.exists()
