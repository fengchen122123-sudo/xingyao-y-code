"""真实 provider 压缩墙钟计时（dev-compact 验证用）。

对合成的大历史会话跑 compress_session_memory，记录分段耗时。
用法：
  python scripts/bench_compression_timing.py probe   # 小规模链路验证（1 段）
  python scripts/bench_compression_timing.py compare # 新旧行为对比（多段，两轮）
"""
import sys
import os
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # 后台管道为 GBK，emoji 输出需 UTF-8

from core.memory import MemoryManager
import core.memory_compressor as mc
from core.model_manager import load_config

MODEL = "openai/qwen3.8-flash"
DELAY_BEHAVIOR = {"inter_chunk_delay_seconds": 1.5}  # 模拟修复前的无条件延时
ROUNDS = [
    ("旧行为(段间延时1.5s)", DELAY_BEHAVIOR),
    ("新行为(自适应0s)", {}),
]


def build_session(mgr, session, turns):
    """构造 turns 轮历史，每轮 ~3600 CJK 字符（≈3600 token）。"""
    for i in range(turns):
        mgr.add_messages(session, [
            ("user", "第%d轮：请帮我重构模块%d并解释设计取舍。" % (i, i) + "背景上下文。" * 500),
            ("assistant", {"content": "", "tool_calls": [
                {"id": "call%d" % i, "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}},
            ]}),
            ("tool", {"tool_call_id": "call%d" % i, "name": "read_file",
                      "content": "第%d个文件内容：" % i + "代码字符" * 600}),
            ("assistant", "第%d轮重构完成：改为事件驱动，性能提升明显。" % i + "细节说明。" * 400),
        ])


def run_once(label, overrides):
    tmp = tempfile.mkdtemp()
    mgr = MemoryManager(os.path.join(tmp, "bench.db"))
    session = "bench_" + str(int(time.time()))
    build_session(mgr, session, turns=25)  # ~90K 字符 ≈ 90K token → 4 段左右
    key = ""
    base = ""
    for p in load_config().get("providers", []):
        if p.get("api_key"):
            key, base = p["api_key"], p["base_url"]
            break
    cfg = mc._load_compression_config()
    cfg.update(overrides)
    cfg["max_total_seconds"] = 600.0
    cfg["retain_count"] = 4
    stamps = []
    t0 = time.time()

    def emit(msg):
        clean = msg.replace("<ansiyellow>", "").replace("</ansiyellow>", "") \
                   .replace("<ansigray>", "").replace("</ansigray>", "") \
                   .replace("<ansigreen>", "").replace("</ansigreen>", "") \
                   .replace("<ansired>", "").replace("</ansired>", "").strip()
        if clean:
            stamps.append((time.time() - t0, clean[:80]))
            print("  [%6.1fs] %s" % (stamps[-1][0], clean[:80]))

    mc._load_compression_config = lambda: cfg
    try:
        outcome = mc.compress_session_memory(
            mgr, session, MODEL, key, base,
            output_callback=emit, return_outcome=True,
        )
    finally:
        mc._load_compression_config = mc._load_compression_config.__original__ \
            if hasattr(mc._load_compression_config, "__original__") else mc._load_compression_config
    dt = time.time() - t0
    status = getattr(outcome, "status", "?")
    reason = getattr(outcome, "reason", "")
    print(">>> %s: 总耗时 %.1fs, status=%s reason=%s\n" % (label, dt, status, reason))
    return dt, status


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if mode == "probe":
        run_once("链路验证", {})
    else:
        results = {}
        for label, ov in ROUNDS:
            results[label] = run_once(label, ov)
        print("==== 对比汇总 ====")
        (old_dt, old_st), (new_dt, new_st) = results["旧行为(段间延时1.5s)"], results["新行为(自适应0s)"]
        print("旧行为: %.1fs (%s) | 新行为: %.1fs (%s) | 节省 %.1fs (%.0f%%)"
              % (old_dt, old_st, new_dt, new_st, old_dt - new_dt,
                 (old_dt - new_dt) / old_dt * 100 if old_dt else 0))
