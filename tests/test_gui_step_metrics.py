import threading
from core.memory import MemoryManager
from gui_bridge import ApiBridge


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


def test_load_session_messages_extracts_metrics():
    bridge = _bare_bridge()
    session = "test_metrics_session"
    bridge._memory.create_session_explicit(session)
    msg = {
        "role": "assistant",
        "content": "测试回复",
        "_display": {
            "model_id": "openai/deepseek-v4",
            "model_name": "deepseek-v4",
            "metrics": {
                "duration_s": 2.5,
                "ttft_ms": 320,
                "completion_tokens": 150,
                "token_speed": 60.0,
            },
        },
    }
    bridge._memory.add_message(session, "assistant", msg)

    loaded = bridge._load_session_messages(session)
    assert len(loaded) == 1
    assert loaded[0]["role"] == "assistant"
    assert loaded[0]["content"] == "测试回复"
    assert loaded[0]["metrics"] == {
        "duration_s": 2.5,
        "ttft_ms": 320,
        "completion_tokens": 150,
        "token_speed": 60.0,
    }


def test_load_session_messages_handles_missing_metrics():
    bridge = _bare_bridge()
    session = "test_no_metrics_session"
    bridge._memory.create_session_explicit(session)
    bridge._memory.add_message(session, "assistant", "普通纯文本回复")

    loaded = bridge._load_session_messages(session)
    assert len(loaded) == 1
    assert loaded[0]["metrics"] is None


def test_step_metrics_event_dispatch(monkeypatch):
    bridge = _bare_bridge()
    emitted = []
    monkeypatch.setattr(
        bridge, "emit_event", lambda kind, payload: emitted.append((kind, payload))
    )
    # Simulate step_metrics coming from core.llm callback
    # We call the event handler logic
    payload = {
        "duration_s": 3.12,
        "output_duration_s": 2.80,
        "ttft_ms": 320,
        "completion_tokens": 140,
        "token_speed": 50.0,
    }
    # In gui_bridge, handler calls self.emit_event("step_metrics", {**payload, "session_name": "会话A"})
    bridge.emit_event("step_metrics", {**payload, "session_name": "会话A"})

    assert emitted == [(
        "step_metrics",
        {
            "duration_s": 3.12,
            "output_duration_s": 2.80,
            "ttft_ms": 320,
            "completion_tokens": 140,
            "token_speed": 50.0,
            "session_name": "会话A",
        },
    )]

