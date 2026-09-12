"""message_turn_links 与回合生命周期集成测试（规格 7.7 / 25.3）。

- 用户消息显式携带 checkpoint；Harness 原子提交路径同事务写关联。
- 消息正文与关联行同事务提交或回滚。
- Provider 历史构造（get_history）不读取关联元数据，字节不变。
"""

import pytest

from core.memory import MemoryManager
from core.turn_undo import service as turn_undo_service
from core.turn_undo.models import OwnerKind, PositionKind
from core.turn_undo.storage import insert_message_turn_links_locked


@pytest.fixture()
def env(tmp_path):
    manager = MemoryManager(str(tmp_path / "ycode.db"))
    storage = turn_undo_service.configure_default_manager(
        manager, blob_root=tmp_path / "undo_blobs"
    )
    manager.ensure_session("link-sess")
    session_id = storage.resolve_session_id("link-sess")
    checkpoint, ctx = turn_undo_service.begin_turn(
        session_id=session_id,
        turn_id="turn-link-1",
        workspace_root=str(tmp_path),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )
    return {"manager": manager, "storage": storage, "checkpoint": checkpoint, "ctx": ctx}


def test_user_message_links_in_same_transaction(env):
    manager = env["manager"]
    manager.add_message(
        "link-sess", "user", "本轮问题",
        turn_checkpoint_id=env["checkpoint"].checkpoint_id,
        turn_position_kind="user",
    )
    links = manager.load_session_turn_links("link-sess")
    assert len(links) == 1
    (info,) = links.values()
    assert info["turn_id"] == "turn-link-1"
    assert info["position_kind"] == PositionKind.USER.value


def test_harness_commit_path_links_messages(env):
    from core.harness.models import HarnessEventKind, JournalEventDraft

    manager = env["manager"]
    manager.heartbeat_harness_owner("owner-1", 0)
    manager.create_harness_turn(
        "turn-link-1", "link-sess", "owner-1", 0
    )
    manager.create_harness_step("step-1", "turn-link-1", 0, 0)
    draft = JournalEventDraft(
        kind=HarnessEventKind.ASSISTANT_COMMITTED, step_id="step-1", message_index=0,
    )
    message_ids = manager.add_messages_with_harness_events(
        "link-sess",
        [("assistant", "本轮回答")],
        turn_id="turn-link-1",
        step_id="step-1",
        events=[draft],
    )
    links = manager.load_session_turn_links("link-sess")
    assert message_ids[0] in links
    assert links[message_ids[0]]["position_kind"] \
        == PositionKind.ASSISTANT_INTERMEDIATE.value
    # TLS 绑定线程兜底（同线程显式传参与按 turn_id 查库等价）
    assert links[message_ids[0]]["checkpoint_id"] == env["checkpoint"].checkpoint_id


def test_mark_final_assistant_updates_position(env):
    from core.harness.models import HarnessEventKind, JournalEventDraft

    manager = env["manager"]
    manager.heartbeat_harness_owner("owner-1", 0)
    manager.create_harness_turn("turn-link-1", "link-sess", "owner-1", 0)
    manager.create_harness_step("step-1", "turn-link-1", 0, 0)

    # 两条 assistant：中间轮 + 最终轮

    drafts = [
        JournalEventDraft(kind=HarnessEventKind.ASSISTANT_COMMITTED, step_id="step-1", message_index=i)
        for i in range(2)
    ]
    ids = manager.add_messages_with_harness_events(
        "link-sess",
        [("assistant", "中间轮"), ("assistant", "最终轮")],
        turn_id="turn-link-1",
        step_id="step-1",
        events=drafts,
    )
    manager.mark_turn_final_assistant(env["checkpoint"].checkpoint_id)
    links = manager.load_session_turn_links("link-sess")
    assert links[ids[0]]["position_kind"] == PositionKind.ASSISTANT_INTERMEDIATE.value
    assert links[ids[1]]["position_kind"] == PositionKind.ASSISTANT_FINAL.value


def test_link_failure_rolls_back_message_batch(env):
    """非法 position_kind 显式失败：消息正文与关联行一起回滚。"""
    from core.turn_undo.models import TurnUndoError

    manager = env["manager"]
    before = manager.get_last_message_id("link-sess")
    with pytest.raises(TurnUndoError):
        manager.add_messages(
            "link-sess", [("user", "即将回滚")],
            turn_checkpoint_id=env["checkpoint"].checkpoint_id,
            turn_position_kinds=("bogus",),
        )
    assert manager.get_last_message_id("link-sess") == before


def test_provider_history_bytes_unchanged_by_links(env):
    """关联元数据是纯界面信息：get_history 返回值与未启用撤销时一致。"""
    manager = env["manager"]
    manager.add_message(
        "link-sess", "user", "问题A",
        turn_checkpoint_id=env["checkpoint"].checkpoint_id,
        turn_position_kind="user",
    )
    manager.add_message("link-sess", "assistant", "回答A")
    history = manager.get_history("link-sess")
    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "问题A"),
        ("assistant", "回答A"),
    ]


def test_links_scoped_to_session(env, tmp_path):
    """load_session_turn_links 只返回当前会话检查点的关联。"""
    manager = env["manager"]
    manager.ensure_session("other-sess")
    manager.add_message(
        "link-sess", "user", "我的",
        turn_checkpoint_id=env["checkpoint"].checkpoint_id,
        turn_position_kind="user",
    )
    assert len(manager.load_session_turn_links("link-sess")) == 1
    assert manager.load_session_turn_links("other-sess") == {}
    turn_undo_service.finish_turn(env["ctx"])
