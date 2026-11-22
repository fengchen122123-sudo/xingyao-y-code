import pytest

import core.agent_service as agent_service
from core.agent_service import build_resume_messages
from core.memory import MemoryManager


@pytest.fixture()
def manager(tmp_path):
    manager = MemoryManager(str(tmp_path / "resume-messages.db"))
    manager.create_session_explicit("session")
    manager.add_messages(
        "session",
        [("user", "把 core/llm.py 的缓存前缀改稳"), ("assistant", "先读一下文件再动手")],
    )
    return manager


def _db_snapshot(manager):
    c = manager.conn
    messages = c.execute(
        "SELECT messages.id, messages.role, messages.content FROM messages "
        "JOIN sessions ON sessions.id = messages.session_id "
        "WHERE sessions.session_name = 'session' ORDER BY messages.id"
    ).fetchall()
    sessions = c.execute(
        "SELECT session_name, memory_summary, summarized_until_msg_id, updated_at "
        "FROM sessions WHERE session_name = 'session'"
    ).fetchall()
    return messages, sessions


def test_build_resume_messages_is_system_plus_llm_history(manager):
    history = manager.get_history_for_llm("session")

    messages = build_resume_messages(manager, "session", is_gui=True)

    assert messages[0]["role"] == "system"
    assert str(messages[0]["content"]).strip()
    assert messages[1:] == history
    assert [item["role"] for item in messages[1:]] == ["user", "assistant"]


def test_build_resume_messages_prefix_is_byte_stable_across_calls(manager):
    first = build_resume_messages(manager, "session", is_gui=True)
    second = build_resume_messages(manager, "session", is_gui=True)

    assert first == second


def test_build_resume_messages_never_compresses_nor_appends(manager, monkeypatch):
    import core.context_builder as context_builder
    import core.memory_compressor as memory_compressor

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "续跑消息组装不得压缩历史 / 追加动态块 / 追加 user 消息 / 写库"
        )

    monkeypatch.setattr(agent_service, "build_runtime_context", _forbidden)
    monkeypatch.setattr(agent_service, "_append_context_suffix_to_last_user", _forbidden)
    monkeypatch.setattr(context_builder, "build_dynamic_prompt", _forbidden)
    monkeypatch.setattr(memory_compressor, "compress_session_memory", _forbidden)
    for name in ("add_message", "add_messages", "replace_latest_user_message_content"):
        if hasattr(manager, name):
            monkeypatch.setattr(manager, name, _forbidden)

    before = _db_snapshot(manager)

    messages = build_resume_messages(manager, "session", is_gui=True)

    assert messages[0]["role"] == "system"
    assert messages[1:] == manager.get_history_for_llm("session")
    assert _db_snapshot(manager) == before
    assert sum(1 for item in messages[1:] if item["role"] == "user") == 1
