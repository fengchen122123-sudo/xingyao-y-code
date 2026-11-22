import threading

from core.memory import MemoryManager
from gui_bridge import ApiBridge


class _FakeWorker:

    def __init__(self, exits_on_join=True):
        self.alive = True
        self.exits_on_join = exits_on_join
        self.join_timeouts = []

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_timeouts.append(timeout)
        if self.exits_on_join:
            self.alive = False


def _make_bridge(turn_active=False, worker=None, stop_ok=True):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._current_session = "s1"
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._cancel_events = {}
    bridge._is_turn_active = lambda name: turn_active
    bridge._memory = MemoryManager(":memory:")
    bridge._memory.create_session_explicit("s1", workdir="GLOBAL", source="GUI")
    events = []
    bridge.emit_event = lambda etype, payload: events.append((etype, payload))
    bridge._emit_message_queue_snapshot = lambda session: events.append(
        ("message_queue", {"session": session})
    )
    bridge.stop_calls = []
    bridge.stop_current_turn = lambda session: (
        bridge.stop_calls.append(session),
        {"ok": stop_ok, "msg": "" if stop_ok else "权限请求取消失败"},
    )[1]
    if worker is not None:
        bridge._active_turns = {"s1": worker}
        bridge._is_turn_active = lambda name: (lambda w: w is not None and w.is_alive())(
            bridge._active_turns.get(name)
        )
    return bridge, events


def _seed(bridge):
    for role, text in [
        ("user", "第一条提问"),
        ("assistant", "第一条回答"),
        ("user", "第二条提问"),
        ("assistant", "第二条回答"),
    ]:
        bridge._memory.add_message("s1", role, text)
    return bridge._memory.get_history_with_ids("s1")


def test_edit_rewinds_from_target_message():
    bridge, _ = _make_bridge()
    rows = _seed(bridge)

    result = bridge.edit_user_message("s1", rows[2]["_msg_id"], "改过的提问")

    assert result["ok"] is True
    assert result["removed"] == 2
    left = bridge._memory.get_history_with_ids("s1")
    assert [m["content"] for m in left] == ["第一条提问", "第一条回答"]


def test_edit_interrupts_running_turn_then_rewinds():
    worker = _FakeWorker()
    bridge, _ = _make_bridge(worker=worker)
    rows = _seed(bridge)

    result = bridge.edit_user_message("s1", rows[2]["_msg_id"], "改过的提问")

    assert result["ok"] is True
    assert bridge.stop_calls == ["s1"]
    assert worker.join_timeouts and worker.join_timeouts[0] > 0
    left = bridge._memory.get_history_with_ids("s1")
    assert [m["content"] for m in left] == ["第一条提问", "第一条回答"]


def test_edit_without_interrupt_still_rejects_running_turn():
    worker = _FakeWorker()
    bridge, _ = _make_bridge(worker=worker)
    rows = _seed(bridge)

    result = bridge.edit_user_message(
        "s1", rows[0]["_msg_id"], "改过的提问", interrupt=False
    )

    assert result["ok"] is False
    assert "生成" in result["msg"]
    assert bridge.stop_calls == []
    assert len(bridge._memory.get_history_with_ids("s1")) == 4


def test_edit_interrupt_timeout_reports_error():
    worker = _FakeWorker(exits_on_join=False)
    bridge, _ = _make_bridge(worker=worker)
    rows = _seed(bridge)

    result = bridge.edit_user_message("s1", rows[2]["_msg_id"], "改过的提问")

    assert result["ok"] is False
    assert "超时" in result["msg"]
    # 线程没退出就绝不能动历史
    assert len(bridge._memory.get_history_with_ids("s1")) == 4


def test_edit_interrupt_stop_failure_reports_error():
    worker = _FakeWorker()
    bridge, _ = _make_bridge(worker=worker, stop_ok=False)
    rows = _seed(bridge)

    result = bridge.edit_user_message("s1", rows[2]["_msg_id"], "改过的提问")

    assert result["ok"] is False
    assert "停止" in result["msg"]
    assert len(bridge._memory.get_history_with_ids("s1")) == 4


def test_edit_rejects_non_user_message():
    bridge, _ = _make_bridge()
    rows = _seed(bridge)

    result = bridge.edit_user_message("s1", rows[1]["_msg_id"], "改过的内容")

    assert result["ok"] is False
    assert "用户消息" in result["msg"]
    assert len(bridge._memory.get_history_with_ids("s1")) == 4


def test_edit_allows_empty_text():
    bridge, _ = _make_bridge()
    rows = _seed(bridge)

    result = bridge.edit_user_message("s1", rows[2]["_msg_id"], "   ")

    assert result["ok"] is True
    left = bridge._memory.get_history_with_ids("s1")
    assert [m["content"] for m in left] == ["第一条提问", "第一条回答"]


def test_edit_rejects_unknown_message():
    bridge, _ = _make_bridge()
    _seed(bridge)

    result = bridge.edit_user_message("s1", 999999, "改过的提问")

    assert result["ok"] is False
    assert len(bridge._memory.get_history_with_ids("s1")) == 4


def test_edit_drops_queued_messages_of_the_session():
    bridge, events = _make_bridge()
    rows = _seed(bridge)
    bridge._message_queues["s1"] = [{"id": "q_1", "text": "排队的消息"}]

    result = bridge.edit_user_message("s1", rows[2]["_msg_id"], "改过的提问")

    assert result["ok"] is True
    assert bridge._message_queues.get("s1") in (None, [])
    assert ("message_queue", {"session": "s1"}) in events
