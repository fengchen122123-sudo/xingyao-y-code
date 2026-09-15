"""GUI 桥协议占位符清理的回归测试。

根因：上游网关会把空 assistant 正文替换为占位符
`[System: Empty message content sanitised to satisfy protocol]`，
该标记进入历史后诱导模型复读。TUI 渲染、持久化与出站消息均已清理，
但 GUI 桥的四条路径此前漏清：
1. 实时 token_delta 流（含跨分片拆分）；
2. 回合收尾的最终冲刷；
3. `_load_session_messages` 历史回放；
4. `_emit_message_final` 兜底全文。
"""

import threading

from core.memory import MemoryManager
from gui_bridge import ApiBridge

MARKER = "[System: Empty message content sanitised to satisfy protocol]"


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


def _collect_raw(bridge, monkeypatch):
    sent = []
    monkeypatch.setattr(bridge, "_emit_raw", lambda kind, data: sent.append((kind, data)))
    return sent


def test_flush_deltas_removes_protocol_marker_from_token_delta(monkeypatch):
    """token_delta 冲刷时必须精确删除协议占位符，普通文本不受影响。"""
    bridge = _bare_bridge()
    sent = _collect_raw(bridge, monkeypatch)
    bridge._delta_buffers["会话A"] = [f"前文 {MARKER} 后文"]
    bridge._delta_kinds["会话A"] = "token_delta"

    bridge._flush_deltas()

    assert sent == [("token_delta", {"text": "前文  后文", "session_name": "会话A"})]


def test_flush_deltas_holds_back_split_marker_prefix(monkeypatch):
    """占位符横跨两次冲刷时不得泄漏：前缀暂扣，拼齐后完整删除。"""
    bridge = _bare_bridge()
    sent = _collect_raw(bridge, monkeypatch)
    cut = 30
    bridge._delta_buffers["会话A"] = ["正文" + MARKER[:cut]]
    bridge._delta_kinds["会话A"] = "token_delta"

    bridge._flush_deltas()
    # 第一片只发安全头部，疑似占位符前缀的尾部必须暂扣
    assert sent == [("token_delta", {"text": "正文", "session_name": "会话A"})]

    bridge._delta_buffers["会话A"].append(MARKER[cut:] + "后文")
    bridge._flush_deltas()

    assert sent[-1] == ("token_delta", {"text": "后文", "session_name": "会话A"})
    assert all(MARKER not in data["text"] for _, data in sent)


def test_finalize_session_deltas_flushes_held_back_tail(monkeypatch):
    """收尾最终冲刷不得丢弃暂扣的尾部（非占位符前缀的真实文本要发出去）。"""
    bridge = _bare_bridge()
    sent = _collect_raw(bridge, monkeypatch)
    # 文本以普通字符结尾，但冲刷时仍被当作疑似前缀暂扣的场景由
    # _safe_prefix_length 保证只暂扣真前缀；这里直接验证最终冲刷无保留。
    bridge._delta_buffers["会话A"] = ["尾部" + MARKER[:20]]
    bridge._delta_kinds["会话A"] = "token_delta"

    bridge._finalize_session_deltas("会话A")

    emitted = "".join(data["text"] for _, data in sent)
    assert emitted == "尾部" + MARKER[:20]
    assert bridge._delta_buffers == {}


def test_load_session_messages_sanitizes_assistant_content():
    """历史回放：数据库中旧 assistant 消息的占位符不得进入前端气泡。"""
    bridge = _bare_bridge()
    bridge._memory.create_session_explicit("会话A")
    bridge._memory.add_message("会话A", "assistant", f"回答{MARKER}完毕")
    bridge._memory.add_message("会话A", "user", f"用户原文 {MARKER} 保留")

    msgs, _, _ = bridge._load_session_messages("会话A")

    assert msgs[0]["content"] == "回答完毕"
    # 用户消息不做清理，避免误删用户真实输入
    assert msgs[1]["content"] == f"用户原文 {MARKER} 保留"


def test_emit_message_final_sanitizes_content(monkeypatch):
    """message_final 兜底全文同样不得携带占位符。"""
    bridge = _bare_bridge()
    events = []
    monkeypatch.setattr(bridge, "emit_event", lambda t, d: events.append((t, d)))
    bridge._memory.create_session_explicit("会话A")
    bridge._memory.add_message("会话A", "assistant", f"完整回复{MARKER}")

    bridge._emit_message_final("会话A")

    assert events == [(
        "message_final",
        {"session_name": "会话A", "content": "完整回复", "reasoning_content": ""},
    )]
