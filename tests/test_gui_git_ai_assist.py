"""Git 面板 AI 辅助按钮的回合来源回归（§5.15）。

背景：git_ai_assist 曾以 turn_source="git_panel" 调 send_user_message，而入口
只放行受控值 local|lan，按钮点击即被拒（返回"无效的回合来源"），功能完全不可用。
本测试锁定：AI 辅助必须走受控来源 local，且真实启动一个回合。
"""
import threading

from gui_bridge import ApiBridge


class _MemoryStub:
    def __init__(self):
        self.ensured = []

    def ensure_session(self, name, workdir=None, source=None):
        self.ensured.append((name, workdir, source))


def _make_bridge():
    """裸桥实例：只装配 send_user_message 路径所需状态，回合 worker 用桩记录。"""
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
    """AI 辅助按钮必须真的启动回合，且回合来源落在受控值域内。"""
    bridge, spawned = _make_bridge()

    res = bridge.git_ai_assist("D:/proj", "review_staged")

    assert res["ok"] is True, res
    assert len(spawned) == 1, spawned
    _args, kwargs = spawned[0]
    assert kwargs["turn_source"] in ("local", "lan"), kwargs
    assert kwargs["turn_source"] == "local"
    assert kwargs["pending_workdir"] == "D:/proj"


def test_git_ai_assist_rejects_unknown_kind_without_starting_turn():
    """未知辅助类型不得启动回合（错误只走结构化通道）。"""
    bridge, spawned = _make_bridge()

    res = bridge.git_ai_assist("D:/proj", "no_such_kind")

    assert res["ok"] is False
    assert res["error"]["code"] == "invalid_state"
    assert spawned == []
