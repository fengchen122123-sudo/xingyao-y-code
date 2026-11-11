"""并发压测：预压缩守卫 / 工具结果改写 / 水位估算 多线程叠加（dev-compact 验证用）。"""
import tempfile
import os
import time
import threading
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.memory import MemoryManager
import core.agent_service as agent_service
import core.memory_compressor as mc

tmp = tempfile.mkdtemp()
mgr = MemoryManager(os.path.join(tmp, "stress.db"))
session = "stress_session"

for i in range(40):
    mgr.add_messages(session, [
        ("user", "第%d轮：请分析模块%d性能" % (i, i) + "背景" * 200),
        ("assistant", {"content": "", "tool_calls": [
            {"id": "call%d" % i, "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}},
        ]}),
        ("tool", {"tool_call_id": "call%d" % i, "name": "read_file",
                  "content": "x" * 8000}),
        ("assistant", "第%d轮结论：瓶颈在重复查询" % i + "分析" * 300),
    ])
print("历史构造 OK, 消息数 =", len(mgr.get_history(session)))


def fake_compress(manager, s, model, key, base, **kw):
    time.sleep(random.uniform(0.5, 2.0))
    return mc.CompressionOutcome("skipped", "stress_stub")


mc.compress_session_memory = fake_compress

errors, lock = [], threading.Lock()


def rec(e):
    with lock:
        errors.append(e)


t0 = time.time()
threads = []
for i in range(6):
    def pre(i=i):
        try:
            for _ in range(3):
                agent_service._trigger_background_precompress(
                    mgr, session, "openai/test", "", "", None, None)
                time.sleep(0.1)
        except Exception as e:
            rec("precompress%d: %r" % (i, e))
    threads.append(threading.Thread(target=pre))
for i in range(4):
    def mnt(i=i):
        try:
            for _ in range(5):
                mgr.maintain_tool_results(session, tier=(i % 2) + 1)
                time.sleep(0.05)
        except Exception as e:
            rec("maintain%d: %r" % (i, e))
    threads.append(threading.Thread(target=mnt))
for t in threads:
    t.start()
for _ in range(20):
    h = mgr.get_history_for_llm(session)
    agent_service.get_session_token_baseline(mgr, session, "openai/test", history=h)
for t in threads:
    t.join()
dt = time.time() - t0

assert not mc.is_session_compressing(mgr, session), "压缩守卫泄漏！"
print("并发压测完成: %.1fs, 异常数=%d, 守卫已释放" % (dt, len(errors)))
for e in errors[:5]:
    print("ERR:", e)
assert not errors
print("STRESS PASSED")
