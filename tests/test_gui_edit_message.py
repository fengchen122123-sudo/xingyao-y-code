"""编辑已发送用户消息的桥接层契约（GUI）。

edit_user_message 只做「校验 + 截断」：重发由前端复用 sendMessage，
乐观渲染、排队与错误提示不在这里重复实现。
"""

import threading

from core.memory import MemoryManager
from gui_bridge import ApiBridge


class _FakeWorker:
    """模拟回合线程：join 时按用例配置决定是否真的退出。"""

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
    """构造仅含编辑所需内部状态的裸桥实例。

    worker 非空时该会话有活跃回合（用于验证「编辑即打断」路径）；
    stop_ok=False 模拟停止回合本身失败。
    """
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
    # 停止回合是既有接口，这里用桩记录调用：打断路径必须真的先停再截断
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
    """生成中点编辑：先停止回合并等线程退出，再截断重发。"""
    worker = _FakeWorker()
    bridge, _ = _make_bridge(worker=worker)
    rows = _seed(bridge)

    result = bridge.edit_user_message("s1", rows[2]["_msg_id"], "改过的提问")

    assert result["ok"] is True
    assert bridge.stop_calls == ["s1"]
    # 截断必须发生在回合线程退出之后：join 传了超时上限
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
    """纯图片编辑（文字清空只留图）由前端拦截后带附件重发，后端不拦空文本。"""
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
