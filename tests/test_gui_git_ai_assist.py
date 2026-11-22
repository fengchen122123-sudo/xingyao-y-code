import threading

from gui_bridge import ApiBridge


class _MemoryStub:
    def __init__(self):
        self.ensured = []

    def ensure_session(self, name, workdir=None, source=None):
        self.ensured.append((name, workdir, source))


def _make_bridge():
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._current_session = "s1"
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._cancel_events = {}
    bridge._is_turn_active = lambda _s: False
    spawned = []
    bridge._spawn_turn_worker = lambda *a, **k: spawned.append((a, k))
    bridge.emit_event = lambda *a, **k: None
    bridge._memory = _MemoryStub()
    return bridge, spawned


def test_git_ai_assist_starts_turn_with_controlled_source():
    bridge, spawned = _make_bridge()

    res = bridge.git_ai_assist("D:/proj", "review_staged")

    assert res["ok"] is True, res
    assert len(spawned) == 1, spawned
    _args, kwargs = spawned[0]
    assert kwargs["turn_source"] in ("local", "lan"), kwargs
    assert kwargs["turn_source"] == "local"
    assert kwargs["pending_workdir"] == "D:/proj"


def test_git_ai_assist_rejects_unknown_kind_without_starting_turn():
    bridge, spawned = _make_bridge()

    res = bridge.git_ai_assist("D:/proj", "no_such_kind")

    assert res["ok"] is False
    assert res["error"]["code"] == "invalid_state"
    assert spawned == []
