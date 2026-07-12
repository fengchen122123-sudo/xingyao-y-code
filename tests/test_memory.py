import pytest
import sqlite3
from core.memory import SessionManager

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