import threading

import gui_bridge
from core.memory import MemoryManager
from gui_bridge import ApiBridge


class _AliveTurn:

    def is_alive(self):
        return True


def _bare_gui_bridge(db=":memory:"):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = MemoryManager(db)
    bridge._current_session = "当前会话"
    bridge._turn_start_lock = threading.RLock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._shutting_down = True
    bridge._window = None
    return bridge


def _events(bridge):
    got = []
    bridge.emit_event = lambda t, d: got.append((t, d))
    return got


def _workdir_of(bridge, name):
    for s in bridge._memory.get_all_sessions(workdir=None, include_empty=True):
        if s["name"] == name:
            return s["workdir"]
    return None


def _seed(bridge, name, workdir="GLOBAL"):
    bridge._memory.create_session_explicit(name, workdir=workdir, source="GUI")
    bridge._memory.add_message(name, "user", "hi")


def test_move_session_across_projects(tmp_path):
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "projA"
    proj_b = tmp_path / "projB"
    proj_a.mkdir()
    proj_b.mkdir()
    a_norm = gui_bridge._resolve_real_path(str(proj_a))
    b_norm = gui_bridge._resolve_real_path(str(proj_b))

    _seed(bridge, "任务", workdir=str(proj_a))

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    assert res["ok"] is True
    assert res["moved"] is True
    assert res["workdir"] == b_norm
    assert _workdir_of(bridge, "任务") == b_norm
    a_names = {s["name"] for s in bridge._memory.get_all_sessions(workdir=a_norm, include_empty=True)}
    b_names = {s["name"] for s in bridge._memory.get_all_sessions(workdir=b_norm, include_empty=True)}
    assert "任务" not in a_names
    assert "任务" in b_names


def test_move_global_session_into_project(tmp_path):
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()
    p_norm = gui_bridge._resolve_real_path(str(proj))

    _seed(bridge, "闲聊", workdir="GLOBAL")

    res = bridge.move_session_to_workdir("闲聊", str(proj))

    assert res["ok"] is True
    assert res["moved"] is True
    assert _workdir_of(bridge, "闲聊") == p_norm


def test_move_project_session_back_to_global(tmp_path):
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()

    _seed(bridge, "任务", workdir=str(proj))

    res = bridge.move_session_to_workdir("任务", "GLOBAL")

    assert res["ok"] is True
    assert res["moved"] is True
    assert _workdir_of(bridge, "任务") == "GLOBAL"


def test_move_rejects_empty_workdir(tmp_path):
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()
    p_norm = gui_bridge._resolve_real_path(str(proj))

    _seed(bridge, "任务", workdir=str(proj))

    res = bridge.move_session_to_workdir("任务", "")

    assert res["ok"] is False
    assert res["msg"]
    assert _workdir_of(bridge, "任务") == p_norm


def test_move_rejects_empty_session_name():
    bridge = _bare_gui_bridge()
    res = bridge.move_session_to_workdir("", "GLOBAL")
    assert res["ok"] is False


def test_move_to_same_group_is_noop(tmp_path):
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()
    p_norm = gui_bridge._resolve_real_path(str(proj))

    _seed(bridge, "任务", workdir=str(proj))
    bridge._memory.conn.execute(
        "UPDATE sessions SET updated_at = '2000-01-01 00:00:00' WHERE session_name = '任务'"
    )
    bridge._memory.conn.commit()

    res = bridge.move_session_to_workdir("任务", str(proj))

    assert res["ok"] is True
    assert res["moved"] is False
    row_updated = next(
        s for s in bridge._memory.get_all_sessions(include_empty=True, include_archived=True)
        if s["name"] == "任务"
    )["updated_at"]
    assert row_updated == "2000-01-01 00:00:00"
    assert _workdir_of(bridge, "任务") == p_norm


def test_move_global_to_global_is_noop():
    bridge = _bare_gui_bridge()
    _seed(bridge, "闲聊", workdir="GLOBAL")

    res = bridge.move_session_to_workdir("闲聊", "GLOBAL")

    assert res["ok"] is True
    assert res["moved"] is False


def test_move_rejects_while_turn_active(tmp_path):
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    a_norm = gui_bridge._resolve_real_path(str(proj_a))

    _seed(bridge, "任务", workdir=str(proj_a))
    bridge._active_turns["任务"] = _AliveTurn()

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    assert res["ok"] is False
    assert "运行" in res["msg"]
    assert _workdir_of(bridge, "任务") == a_norm


def test_move_rejects_with_running_background_tasks(tmp_path, monkeypatch):
    from core.tools import subagent_manager

    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    a_norm = gui_bridge._resolve_real_path(str(proj_a))

    _seed(bridge, "任务", workdir=str(proj_a))
    monkeypatch.setattr(subagent_manager, "has_running_background_tasks",
                        lambda s: s == "任务")

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    assert res["ok"] is False
    assert "后台任务" in res["msg"]
    assert _workdir_of(bridge, "任务") == a_norm


def test_move_proceeds_when_background_probe_raises(tmp_path, monkeypatch):
    from core.tools import subagent_manager

    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    b_norm = gui_bridge._resolve_real_path(str(proj_b))

    _seed(bridge, "任务", workdir=str(proj_a))

    def boom(_s):
        raise RuntimeError("probe down")

    monkeypatch.setattr(subagent_manager, "has_running_background_tasks", boom)

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    assert res["ok"] is True
    assert _workdir_of(bridge, "任务") == b_norm


def test_move_rejects_unknown_session(tmp_path):
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()

    res = bridge.move_session_to_workdir("查无此会话", str(proj))

    assert res["ok"] is False
    assert res["msg"]


def test_move_rejects_missing_target_dir(tmp_path):
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()
    p_norm = gui_bridge._resolve_real_path(str(proj))

    _seed(bridge, "任务", workdir=str(proj))

    res = bridge.move_session_to_workdir("任务", str(tmp_path / "不存在"))

    assert res["ok"] is False
    assert _workdir_of(bridge, "任务") == p_norm


def test_move_rejects_secretary_session(tmp_path):
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()

    bridge._memory.create_session_explicit("session_abc123", workdir="GLOBAL", source="SECRETARY")
    bridge._memory.add_message("session_abc123", "user", "hi")

    res = bridge.move_session_to_workdir("session_abc123", str(proj))

    assert res["ok"] is False
    assert "秘书" in res["msg"]
    assert _workdir_of(bridge, "session_abc123") == "GLOBAL"


def test_move_preserves_messages_and_model_binding(tmp_path):
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()

    bridge._memory.create_session_explicit("任务", workdir=str(proj_a), source="GUI")
    bridge._memory.add_message("任务", "user", "第一条")
    bridge._memory.add_message("任务", "assistant", "第二条")
    bridge._memory.set_session_model_id("任务", "model-xyz")
    before = bridge._memory.get_history("任务")

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    assert res["ok"] is True
    after = bridge._memory.get_history("任务")
    assert [m["content"] for m in after] == [m["content"] for m in before]
    assert len(after) == len(before)
    assert bridge._memory.get_session_model_id("任务") == "model-xyz"


def test_move_does_not_touch_other_sessions(tmp_path):
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    a_norm = gui_bridge._resolve_real_path(str(proj_a))

    for n in ("任务一", "任务二"):
        _seed(bridge, n, workdir=str(proj_a))

    bridge.move_session_to_workdir("任务一", str(proj_b))

    assert _workdir_of(bridge, "任务二") == a_norm
    a_names = {s["name"] for s in bridge._memory.get_all_sessions(workdir=a_norm, include_empty=True)}
    assert a_names == {"任务二"}


def test_move_does_not_migrate_permission_grants(tmp_path):
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()

    _seed(bridge, "任务", workdir=str(proj_a))
    bridge._message_queues["任务"] = ["排队消息"]

    bridge.move_session_to_workdir("任务", str(proj_b))

    assert bridge._message_queues == {"任务": ["排队消息"]}


def test_move_registers_target_as_workspace(tmp_path):
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    b_norm = gui_bridge._resolve_real_path(str(proj_b))

    _seed(bridge, "任务", workdir=str(proj_a))

    bridge.move_session_to_workdir("任务", str(proj_b))

    assert b_norm in bridge._memory.get_all_workspaces()


def test_move_emits_session_moved_event(tmp_path):
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    got = _events(bridge)

    _seed(bridge, "任务", workdir=str(proj_a))

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    assert res["ok"] is True
    assert any(t == "session_moved" for t, _ in got)
    payload = next(d for t, d in got if t == "session_moved")
    assert payload["name"] == "任务"
    assert payload["workdir"] == gui_bridge._resolve_real_path(str(proj_b))
    assert isinstance(payload["sessions"], list)
    assert any(g.get("folder_path") == payload["workdir"] for g in payload["sessions"])


def test_move_returns_group_tree(tmp_path):
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    b_norm = gui_bridge._resolve_real_path(str(proj_b))

    _seed(bridge, "任务", workdir=str(proj_a))

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    groups = {g["folder_path"]: g for g in res["sessions"]}
    assert b_norm in groups
    assert "任务" in {s["name"] for s in groups[b_norm]["sessions"]}
