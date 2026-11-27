import threading

import gui_bridge
from core.memory import MemoryManager


def test_rename_session_to_title_skips_same_name_event(tmp_path):
    mem = MemoryManager(db_path=str(tmp_path / "rename.db"))
    mem.ensure_session("你现在工作空间是什么", workdir=str(tmp_path), source="GUI")

    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)
    bridge._memory = mem
    bridge._current_session = "你现在工作空间是什么"
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._goal_runs = {}
    emitted = []
    bridge.emit_event = lambda etype, data: emitted.append((etype, data))
    bridge.get_sessions = lambda: []
    bridge._set_current_session = lambda name: setattr(bridge, "_current_session", name)

    try:
        # AI 总结或兜底标题与当前名称相同时（仅更新 title_source），不应广播同名 session_renamed
        res_same = bridge._rename_session_to_title(
            "你现在工作空间是什么", "你现在工作空间是什么", require_untitled=True
        )
        assert res_same == "你现在工作空间是什么"
        assert emitted == []

        # 名称确实变化时正常广播 session_renamed
        mem.ensure_session("session_123456", workdir=str(tmp_path), source="GUI")
        bridge._current_session = "session_123456"
        res_diff = bridge._rename_session_to_title(
            "session_123456", "实际项目标题", require_untitled=True
        )
        assert res_diff == "实际项目标题"
        assert len(emitted) == 1
        assert emitted[0][0] == "session_renamed"
        assert emitted[0][1]["old"] == "session_123456"
        assert emitted[0][1]["new"] == "实际项目标题"
    finally:
        mem.close()


def test_remote_turn_source_skips_auto_rename_and_truncation(tmp_path):
    """验证 mcp/lan 远程来源在 send_user_message 与 _run_turn_worker 源码路径中受 turn_source 保护不触发改名。"""
    import pathlib

    src = pathlib.Path(gui_bridge.__file__).read_text(encoding="utf-8")
    assert 'elif turn_source in ("mcp", "lan"):' in src
    assert 'if turn_source == "local" and workdir and re.match(r"^session_[a-f0-9]{6}$"' in src

    mem = MemoryManager(db_path=str(tmp_path / "ensure.db"))
    try:
        mem.ensure_session("remote-keep", workdir="GLOBAL", source="GUI", title_source="manual")
        assert mem.get_session_title_source("remote-keep") == "manual"
    finally:
        mem.close()
