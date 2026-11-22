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


def test_finalize_session_deltas_flushes_tail_before_discard(monkeypatch):
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
    bridge = _bare_bridge()
    bridge._finalize_session_deltas("不存在的会话")
    assert bridge._delta_buffers == {}


def test_emit_message_final_sends_last_assistant_full_text(monkeypatch):
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
    bridge = _bare_bridge()

    class _BrokenMemory:
        def get_history(self, session_name):
            raise RuntimeError("磁盘 IO 失败")

    bridge._memory = _BrokenMemory()
    bridge._emit_message_final("会话A")


def _bare_turn_bridge(db=":memory:"):
    bridge = _bare_bridge(db)
    bridge._cancel_events = {"session": threading.Event()}
    bridge._active_turns = {}
    bridge._active_turn_ids = {}
    bridge._active_turn_lock = threading.Lock()
    bridge._active_turn_snapshots = {}
    bridge._task_status = "idle"
    bridge._bg_summary_pending_sessions = set()
    bridge._bg_summary_lock = threading.Lock()
    bridge._turn_start_lock = threading.Lock()
    bridge._turn_owner_sessions = {}
    bridge._turn_owner_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._delta_flusher = None
    bridge._current_permission_mode = "confirm"
    bridge._emit_token_usage = lambda: None
    from core.turn_undo import service as _tu_service
    import tempfile as _tempfile

    _tu_service.configure_default_manager(
        bridge._memory, blob_root=_tempfile.mkdtemp()
    )
    return bridge


def test_emit_message_final_skips_stale_assistant_before_boundary(monkeypatch):
    bridge = _bare_bridge()
    events = []
    monkeypatch.setattr(
        bridge, "emit_event", lambda t, d: events.append((t, d))
    )
    bridge._memory.create_session_explicit("会话A")
    bridge._memory.add_message("会话A", "user", "上一轮提问")
    bridge._memory.add_message("会话A", "assistant", "上一轮的助手全文")
    boundary = bridge._memory.get_last_message_id("会话A")

    bridge._emit_message_final("会话A", after_msg_id=boundary)

    assert events == []


def test_emit_message_final_sends_only_message_after_boundary(monkeypatch):
    bridge = _bare_bridge()
    events = []
    monkeypatch.setattr(
        bridge, "emit_event", lambda t, d: events.append((t, d))
    )
    bridge._memory.create_session_explicit("会话A")
    bridge._memory.add_message("会话A", "user", "上一轮提问")
    bridge._memory.add_message("会话A", "assistant", "上一轮的助手全文")
    boundary = bridge._memory.get_last_message_id("会话A")
    bridge._memory.add_message("会话A", "user", "本轮提问")
    bridge._memory.add_message("会话A", "assistant", "本轮的助手全文")

    bridge._emit_message_final("会话A", after_msg_id=boundary)

    assert events == [(
        "message_final",
        {"session_name": "会话A", "content": "本轮的助手全文", "reasoning_content": ""},
    )]


def test_worker_exception_does_not_replay_previous_assistant_message(monkeypatch):
    bridge = _bare_turn_bridge()
    bridge._memory.create_session_explicit("session")
    bridge._memory.add_message("session", "user", "上一轮提问")
    bridge._memory.add_message("session", "assistant", "上一轮的助手全文" * 3)
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))
    monkeypatch.setattr(
        "gui_bridge.get_active_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider disconnected")),
    )

    bridge._run_turn_worker("你好", "session")

    kinds = [kind for kind, _ in emitted]
    assert "turn_error" in kinds
    assert "message_final" not in kinds
    assert [data for kind, data in emitted if kind == "message_final"] == []


def test_worker_emits_message_final_for_current_turn_message(monkeypatch, tmp_path):
    bridge = _bare_turn_bridge()
    bridge._memory.create_session_explicit("session")
    bridge._memory.add_message("session", "user", "上一轮提问")
    bridge._memory.add_message("session", "assistant", "上一轮的助手全文")
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))
    monkeypatch.setattr("gui_bridge.get_active_model", lambda *_a, **_k: {
        "litellm_model": "model",
        "api_key": "key",
        "base_url": None,
        "context_window": 128000,
    })
    monkeypatch.setattr(
        "core.input_parser.parse_user_input", lambda text, **_kwargs: (text, [])
    )
    monkeypatch.setattr("core.agent_service.prepare_turn_messages", lambda *_a, **_k: [])
    bridge._session_workdir = lambda _name: str(tmp_path)
    bridge._sync_active_model_context = lambda *_a: None
    bridge._recalc_token_usage = lambda *_a, **_k: None
    bridge._subagent_progress_poller = lambda *_a, **_k: None
    bridge._maybe_auto_rename = lambda _name: None

    def _fake_consume(*_args, **_kwargs):
        bridge._memory.add_message("session", "assistant", "本轮的完整回复")
        return False

    bridge._consume_turn_events = _fake_consume

    bridge._run_turn_worker("你好", "session")

    finals = [data for kind, data in emitted if kind == "message_final"]
    assert len(finals) == 1
    assert finals[0]["content"] == "本轮的完整回复"
