"""流式尾部丢失修复的回归测试。

根因 1（已修）：回合收尾 finally 直接 pop delta 缓冲，回合末尾滞留在缓冲里
的最后一块文本（距上次冲刷不足 60ms、冲刷线程未轮到）被静默丢弃，前端靠
delta 累加渲染，尾部永久缺失。
根因 2 兜底（新增）：evaluate_js 投递可能静默失败，后端回合结束补发
message_final 全文事件，前端仅在全文更长时覆盖校正。
"""

import threading

from core.memory import MemoryManager
from gui_bridge import ApiBridge


def _bare_bridge(db=":memory:"):
    """不经 __init__ 构造裸实例，挂载 delta 缓冲与内存版 MemoryManager。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = MemoryManager(db)
    bridge._shutting_down = False
    bridge._window = None
    bridge._lan_server = None
    bridge._delta_lock = threading.Lock()
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_last_flush = 0.0
    return bridge


def test_finalize_session_deltas_flushes_tail_before_discard(monkeypatch):
    """回归保护：收尾必须先冲刷再清缓冲，滞留在缓冲的尾部文本不得丢失。"""
    bridge = _bare_bridge()
    sent = []
    monkeypatch.setattr(
        bridge, "_emit_raw", lambda kind, data: sent.append((kind, data))
    )
    bridge._delta_buffers["会话A"] = ["尾部文本"]
    bridge._delta_kinds["会话A"] = "token_delta"

    bridge._finalize_session_deltas("会话A")

    assert sent == [("token_delta", {"text": "尾部文本", "session_name": "会话A"})]
    assert bridge._delta_buffers == {}
    assert bridge._delta_kinds == {}


def test_finalize_session_deltas_tolerates_empty_buffers():
    """缓冲为空时收尾是无害 no-op，不得抛异常。"""
    bridge = _bare_bridge()
    bridge._finalize_session_deltas("不存在的会话")
    assert bridge._delta_buffers == {}


def test_emit_message_final_sends_last_assistant_full_text(monkeypatch):
    """message_final 携带最后一条 assistant 消息全文（content + reasoning）。"""
    bridge = _bare_bridge()
    events = []
    monkeypatch.setattr(
        bridge, "emit_event", lambda t, d: events.append((t, d))
    )
    bridge._memory.create_session_explicit("会话A")
    bridge._memory.add_message("会话A", "user", "问")
    bridge._memory.add_message("会话A", "assistant", "前半轮回复")
    bridge._memory.add_message("会话A", "assistant", "完整回复全文")

    bridge._emit_message_final("会话A")

    assert events == [(
        "message_final",
        {"session_name": "会话A", "content": "完整回复全文", "reasoning_content": ""},
    )]


def test_emit_message_final_noop_without_assistant_text(monkeypatch):
    """会话中没有 assistant 文本（如开场即工具调用失败）时不发事件。"""
    bridge = _bare_bridge()
    events = []
    monkeypatch.setattr(
        bridge, "emit_event", lambda t, d: events.append((t, d))
    )
    bridge._memory.create_session_explicit("会话A")
    bridge._memory.add_message("会话A", "user", "只有用户消息")

    bridge._emit_message_final("会话A")

    assert events == []


def test_emit_message_final_swallows_errors():
    """兜底事件绝不能阻断 turn_finish：内存读取抛异常时静默吞掉。"""
    bridge = _bare_bridge()

    class _BrokenMemory:
        def get_history(self, session_name):
            raise RuntimeError("磁盘 IO 失败")

    bridge._memory = _BrokenMemory()
    # 不应抛出
    bridge._emit_message_final("会话A")
