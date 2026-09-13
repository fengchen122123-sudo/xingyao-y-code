"""补验：DB 原文完整性与活跃区状态（fidelity 测试库）。"""
import sys
import os
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.memory import MemoryManager

d = sorted(glob.glob(r"C:\Users\nw02\AppData\Local\Temp\fidelity_*"))[-1]
m = MemoryManager(os.path.join(d, "fid.db"))
row = m.conn.execute("SELECT id FROM sessions").fetchone()
rows = m.conn.execute(
    "SELECT content FROM messages WHERE session_id=?", (row[0],)).fetchall()
all_text = "".join(str(r[0]) for r in rows)
facts = ["海燕", "5433", "冯氏科技", "48000", "scheduler.py", "陈工", "10 月 15",
         "E4721", "Redis", "2.3.1", "45 秒", "mq_dispatch", "180 天", "5 年直线",
         "800 毫秒", "Q3", "沪ICP备2026锦88"]
missing = [f for f in facts if f not in all_text]
print("DB 消息总数:", len(rows))
print("17 个关键词在 DB 原文缺失的:", missing if missing else "无——全部完整")
h = m.get_history_for_llm("fid_session")
print("活跃区条数:", len(h))
print("活跃区角色序列:", [x["role"] for x in h])
last = str(h[-1].get("content"))
print("活跃区最后1条前80字:", last[:80])
print("哨兵 A13 在活跃区:", "哨兵内容A13" in last or any("哨兵内容A13" in str(x.get("content")) for x in h))
