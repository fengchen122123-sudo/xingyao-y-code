import json
import os
import sqlite3

print("=== 测试技能插件 ===")
from core.skills import load_skills, loaded_skills, execute_skill
load_skills()
print("已加载技能:", list(loaded_skills.keys()))
print("执行结果:", execute_skill("say_hello", {"name": "Y-code"}))

print("\n=== 测试 MCP 客户端 ===")
from core.mcp_client import mcp_manager
print("MCP 配置文件是否存在:", os.path.exists("mcp.json"))
print("MCP 载入的 Schema 数量:", len(mcp_manager.get_schemas()))

print("\n=== 测试记忆压缩逻辑 ===")
from core.memory import MemoryManager
m = MemoryManager()
session = "test_prune_session"
cursor = m.conn.cursor()
cursor.execute("INSERT OR IGNORE INTO sessions (session_name) VALUES (?)", (session,))
m.conn.commit()

# 插入测试数据
m.add_message(session, "user", [
    {"type": "text", "text": "第一轮问答\n\n[附带上下文: aaa.py]\n```python\nimport os\nprint('hello world!')\n```\n"}
])
m.add_message(session, "assistant", "你好，已阅读。")
m.add_message(session, "user", [
    {"type": "text", "text": "第二轮问答\n\n[附带上下文: bbb.py]\n```\nimport sys\nprint('hello world!')\n```\n"}
])

# 触发压缩: 超过 1 轮就压缩，只保留最近 1 条
m.prune_history_files(session, max_turns=1, retain_recent=1)
h = m.get_history(session)
print("压缩后的历史:")
for msg in h:
    if msg['role'] == 'user':
        print(json.dumps(msg, ensure_ascii=False))

# 清理测试数据
cursor.execute("DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE session_name=?)", (session,))
cursor.execute("DELETE FROM sessions WHERE session_name=?", (session,))
m.conn.commit()
