"""workspace_correction_ledger 动态上下文测试（规格 15.3 / 25.4）。"""

import pytest

from core.memory import MemoryManager
from core.turn_undo import prompt_context
from core.turn_undo import service as turn_undo_service
from core.turn_undo.models import OwnerKind, TurnUndoStatus


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / "data"))
    manager = MemoryManager(str(tmp_path / "ycode.db"))
    storage = turn_undo_service.configure_default_manager(
        manager, blob_root=tmp_path / "blobs"
    )
    manager.add_messages("pc-sess", [("user", "hi")])
    return {
        "manager": manager, "storage": storage,
        "session_id": storage.resolve_session_id("pc-sess"),
        "ws": tmp_path / "ws", "tmp": tmp_path,
    }


def _revert_turn(env, turn):
    from core.turn_undo import recorder as tu_recorder
    from core.turn_undo.models import NodeKind

    env["ws"].mkdir(parents=True, exist_ok=True)
    target = env["ws"] / "pc.txt"
    target.write_bytes(b"old")
    checkpoint, ctx = turn_undo_service.begin_turn(
        session_id=env["session_id"], turn_id=turn,
        workspace_root=str(env["ws"]), owner_kind=OwnerKind.FOREGROUND_TURN,
    )
    handle = tu_recorder.prepare_mutation(
        display_path=str(target), after_kind=NodeKind.REGULAR_FILE,
        after_content=b"new", tool_name="write_file", tool_call_id="c",
    )
    target.write_bytes(b"new")
    tu_recorder.verify_after_mutation(handle)
    turn_undo_service.finish_turn(ctx)
    _, token = turn_undo_service.preview_turn_undo(env["session_id"], turn)
    return turn_undo_service.execute_turn_undo(env["session_id"], turn, token)


def test_no_corrections_empty_section(env):
    assert prompt_context.build_workspace_correction_section(env["ws"]) == ""


def test_correction_ledger_content_and_isolation(env):
    _revert_turn(env, "turn-pc-1")
    section = prompt_context.build_workspace_correction_section(env["ws"])
    assert "<workspace_correction_ledger>" in section
    assert "turn-pc-1" in section
    assert "文件修改已由用户撤销" in section
    assert "必须重新读取磁盘文件" in section
    # 不同工作区不串
    other = env["tmp"] / "other-ws"
    assert prompt_context.build_workspace_correction_section(other) == ""
    # 同一规范化工作区的其他会话（相对路径/别名）也能看到
    assert prompt_context.build_workspace_correction_section(str(env["ws"]) + "/.") == section or \
        "turn-pc-1" in prompt_context.build_workspace_correction_section(str(env["ws"]) + "/.")


def test_interrupted_checkpoint_warns_not_reverted(env):
    from core.turn_undo.models import NodeKind

    env["ws"].mkdir(parents=True, exist_ok=True)
    target = env["ws"] / "x.py"
    target.write_bytes(b"old")
    from core.turn_undo import recorder as tu_recorder

    checkpoint, ctx = turn_undo_service.begin_turn(
        session_id=env["session_id"], turn_id="turn-pc-2",
        workspace_root=str(env["ws"]), owner_kind=OwnerKind.FOREGROUND_TURN,
    )
    handle = tu_recorder.prepare_mutation(
        display_path=str(target), after_kind=NodeKind.REGULAR_FILE,
        after_content=b"new", tool_name="write_file", tool_call_id="c",
    )
    target.write_bytes(b"new")
    tu_recorder.verify_after_mutation(handle)
    turn_undo_service.finish_turn(ctx)
    # 模拟撤销中断（直接置 interrupted）
    env["storage"].transition_checkpoint(checkpoint.checkpoint_id, TurnUndoStatus.REVERTING)
    env["storage"].transition_checkpoint(
        checkpoint.checkpoint_id, TurnUndoStatus.INTERRUPTED,
        last_error_message="应用阶段中断",
    )
    section = prompt_context.build_workspace_correction_section(env["ws"])
    assert "部分恢复状态" in section
    assert "已由用户撤销" not in section or "可能处于部分恢复" in section
