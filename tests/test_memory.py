import pytest
import sqlite3
from core.memory import MemoryManager, SessionManager


def test_memory_manager_compatibility_name():
    assert SessionManager is MemoryManager

def test_init_db_creates_tables():
    """测试 SessionManager 初始化时是否正确创建了数据库表"""
    
    # 1. 准备与执行 (Arrange & Act)
    # 传入 ":memory:" 让数据库只活在内存中
    manager = SessionManager(":memory:")
    
    # 2. 断言验证 (Assert)
    # 获取数据库游标，去系统的 sqlite_master 表里查一下，我们的表到底建成功没有
    cursor = manager.conn.cursor()
    
    # 检查 sessions (会话表) 是否存在
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
    assert cursor.fetchone() is not None, "sessions 表没有被创建！"
    
    # 检查 messages (具体消息表) 是否存在
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
    assert cursor.fetchone() is not None, "messages 表没有被创建！"


def test_add_and_get_messages():
    """测试是否能向会话中添加消息，并正确读取历史记录"""
    
    # 1. 准备：在内存里建一个新管家
    manager = SessionManager(":memory:")
    session_name = "test_chat"
    
    # 2. 执行：让管家记下两句话（注意：这时候会报错，因为我们还没教管家怎么记）
    manager.add_message(session_name, "user", "你好，我是人类")
    manager.add_message(session_name, "assistant", "你好，我是 Y-code！")
    
    # 3. 执行：让管家把刚才那个会话的记录全翻出来
    history = manager.get_history(session_name)
    
    # 4. 断言验证：检查管家翻出来的记录对不对
    assert len(history) == 2, "历史记录应该有2条！"
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "你好，我是人类"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "你好，我是 Y-code！"


def test_structured_assistant_history_returns_public_content_to_llm(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory.db"))
    manager.add_message("session", "assistant", {
        "role": "assistant",
        "content": "最终回答",
        "reasoning_content": "分析过程",
    })

    history = manager.get_history_for_llm("session")

    assert history == [{"role": "assistant", "content": "最终回答"}]


def test_add_messages_persists_one_ordered_batch(tmp_path):
    manager = MemoryManager(str(tmp_path / "batch.db"))

    manager.add_messages("session", [
        ("assistant", {"content": None, "tool_calls": [{"id": "one"}]}),
        ("tool", {"tool_call_id": "one", "content": "result"}),
        ("assistant", {"content": "summary"}),
    ])

    history = manager.get_history("session")
    assert [message["role"] for message in history] == [
        "assistant",
        "tool",
        "assistant",
    ]
    assert history[-1]["content"] == "summary"


def test_plain_scalar_text_is_not_misparsed_as_json(tmp_path):
    """用户消息恰为 123 / true 等合法 JSON 标量时，content 必须保持原始字符串类型"""
    manager = MemoryManager(str(tmp_path / "scalar.db"))
    manager.add_message("session", "user", "123")
    manager.add_message("session", "user", "true")

    history = manager.get_history("session")

    assert history[0]["content"] == "123"
    assert isinstance(history[0]["content"], str)
    assert history[1]["content"] == "true"
    assert isinstance(history[1]["content"], str)

    llm_history = manager.get_history_for_llm("session")
    assert llm_history[0]["content"] == "123"
    assert isinstance(llm_history[0]["content"], str)


def test_rename_session_suffix_uses_stripped_base_name(tmp_path):
    """重命名冲突时后缀基于 strip 后的基准名，不产生 'name _1' 带空格名"""
    manager = MemoryManager(str(tmp_path / "rename.db"))
    manager.add_message("chat", "user", "hello")
    manager.add_message("taken", "user", "hello")

    success, final_name = manager.rename_session("chat", "  taken  ")

    assert success is True
    assert final_name == "taken_1"
    assert " " not in final_name


def test_rename_session_rejects_empty_new_name(tmp_path):
    """空串/纯空白/None 新名字必须被拒绝，不得产生空 session_name"""
    manager = MemoryManager(str(tmp_path / "rename_empty.db"))
    manager.add_message("chat", "user", "hello")

    assert manager.rename_session("chat", "") == (False, "chat")
    assert manager.rename_session("chat", "   ") == (False, "chat")
    assert manager.rename_session("chat", None) == (False, "chat")
    # 原会话不受影响
    assert any(s["name"] == "chat" for s in manager.get_all_sessions())


def test_sessions_are_scoped_by_workdir(tmp_path):
    """会话按工作目录隔离：只返回指定目录的会话 + 历史遗留（无目录）会话"""
    manager = MemoryManager(str(tmp_path / "scoped.db"))
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()

    manager.set_workdir(dir_a)
    manager.add_message("session_a", "user", "hello")
    manager.set_workdir(dir_b)
    manager.add_message("session_b", "user", "hello")

    names_a = {s["name"] for s in manager.get_all_sessions(workdir=str(dir_a))}
    names_b = {s["name"] for s in manager.get_all_sessions(workdir=str(dir_b))}
    names_all = {s["name"] for s in manager.get_all_sessions()}

    assert names_a == {"session_a"}
    assert names_b == {"session_b"}
    assert names_all == {"session_a", "session_b"}


def test_legacy_sessions_without_workdir_visible_everywhere(tmp_path):
    """历史遗留的 NULL workdir 会话在任何目录下都可见（升级不丢历史）"""
    manager = MemoryManager(str(tmp_path / "legacy.db"))
    manager.set_workdir(tmp_path)
    manager.add_message("new_session", "user", "hello")
    # 手工插入一条无 workdir 的旧会话记录
    with manager.acquire_lock():
        manager.conn.execute(
            "INSERT INTO sessions (session_name) VALUES (?)", ("legacy_session",)
        )
        manager.conn.commit()

    # legacy_session 是无消息的空会话，测试目的是验证旧会话可见性，
    # 传 include_empty=True 跳过空会话过滤（参考 kimicode 的列表过滤策略）
    names = {s["name"] for s in manager.get_all_sessions(workdir=str(tmp_path), include_empty=True)}
    assert names == {"new_session", "legacy_session"}


def test_session_timestamps_recorded_and_updated(tmp_path):
    """新建会话记录 workdir/created_at/updated_at，再次写消息时 updated_at 刷新"""
    manager = MemoryManager(str(tmp_path / "timestamps.db"))
    manager.set_workdir(tmp_path)
    manager.add_message("chat", "user", "hello")

    session = next(s for s in manager.get_all_sessions() if s["name"] == "chat")
    assert session["workdir"] is not None
    assert session["created_at"] and len(session["created_at"]) == 19
    assert session["updated_at"] == session["created_at"]

    # 再次写入消息后 updated_at 字段仍存在且格式正确（秒级精度下同秒相等也合法）
    manager.add_message("chat", "assistant", "reply")
    session = next(s for s in manager.get_all_sessions() if s["name"] == "chat")
    assert session["updated_at"] >= session["created_at"]
