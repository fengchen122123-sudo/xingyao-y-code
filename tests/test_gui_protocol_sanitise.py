import threading

from core.memory import MemoryManager
from gui_bridge import ApiBridge

MARKER = "[System: Empty message content sanitised to satisfy protocol]"


def _bare_bridge(db=":memory:"):
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
    bridge = _bare_bridge()
    sent = _collect_raw(bridge, monkeypatch)
    bridge._delta_buffers["会话A"] = [f"前文 {MARKER} 后文"]
    bridge._delta_kinds["会话A"] = "token_delta"

    bridge._flush_deltas()

    assert sent == [("token_delta", {"text": "前文  后文", "session_name": "会话A"})]


def test_flush_deltas_holds_back_split_marker_prefix(monkeypatch):
    bridge = _bare_bridge()
    sent = _collect_raw(bridge, monkeypatch)
    cut = 30
    bridge._delta_buffers["会话A"] = ["正文" + MARKER[:cut]]
    bridge._delta_kinds["会话A"] = "token_delta"

    bridge._flush_deltas()
    assert sent == [("token_delta", {"text": "正文", "session_name": "会话A"})]

    bridge._delta_buffers["会话A"].append(MARKER[cut:] + "后文")
    bridge._flush_deltas()

    assert sent[-1] == ("token_delta", {"text": "后文", "session_name": "会话A"})
    assert all(MARKER not in data["text"] for _, data in sent)


def test_finalize_session_deltas_flushes_held_back_tail(monkeypatch):
    bridge = _bare_bridge()
    sent = _collect_raw(bridge, monkeypatch)
    bridge._delta_buffers["会话A"] = ["尾部" + MARKER[:20]]
    bridge._delta_kinds["会话A"] = "token_delta"

    bridge._finalize_session_deltas("会话A")

    emitted = "".join(data["text"] for _, data in sent)
    assert emitted == "尾部" + MARKER[:20]
    assert bridge._delta_buffers == {}


def test_load_session_messages_sanitizes_assistant_content():
    bridge = _bare_bridge()
    bridge._memory.create_session_explicit("会话A")
    bridge._memory.add_message("会话A", "assistant", f"回答{MARKER}完毕")
    bridge._memory.add_message("会话A", "user", f"用户原文 {MARKER} 保留")

    msgs, _, _ = bridge._load_session_messages("会话A")

    assert msgs[0]["content"] == "回答完毕"
    assert msgs[1]["content"] == f"用户原文 {MARKER} 保留"


def test_emit_message_final_sanitizes_content(monkeypatch):
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
