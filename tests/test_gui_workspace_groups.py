import os
import threading

import gui_bridge
from core.memory import MemoryManager
from gui_bridge import ApiBridge


def _bare_gui_bridge(db=":memory:"):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = MemoryManager(db)
    bridge._current_session = "测试会话"
    bridge._turn_start_lock = threading.RLock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    return bridge


def test_get_sessions_does_not_register_app_install_dir(monkeypatch):
    bridge = _bare_gui_bridge()
    app_dir = gui_bridge._app_install_dir()
    monkeypatch.setattr(os, "getcwd", lambda: app_dir)

    groups = bridge.get_sessions()
    folder_paths = {g["folder_path"] for g in groups}
    assert app_dir not in folder_paths
    assert bridge._memory.get_all_workspaces() == []


def test_get_sessions_registers_normal_project_dir(monkeypatch, tmp_path):
    bridge = _bare_gui_bridge()
    proj = str(tmp_path)
    monkeypatch.setattr(os, "getcwd", lambda: proj)

    groups = bridge.get_sessions()
    folder_paths = {g["folder_path"] for g in groups}
    assert gui_bridge._resolve_real_path(proj) in folder_paths


def test_get_workspace_state_reads_explicit_project(tmp_path):
    bridge = _bare_gui_bridge()
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("project", encoding="utf-8")

    result = bridge.get_workspace_state(str(project))

    assert result["ok"] is True
    assert result["workdir"] == gui_bridge._resolve_real_path(str(project))
    assert [node["n"] for node in result["file_tree"]] == ["README.md"]


def test_delete_folder_group_does_not_resurrect(monkeypatch):
    bridge = _bare_gui_bridge()
    app_dir = gui_bridge._app_install_dir()
    monkeypatch.setattr(os, "getcwd", lambda: app_dir)
    bridge._memory.add_workspace(app_dir)
    assert app_dir in bridge._memory.get_all_workspaces()

    res = bridge.delete_folder_group(app_dir)
    assert res["success"] is True
    folder_paths = {g["folder_path"] for g in res["sessions"]}
    assert app_dir not in folder_paths
    assert bridge._memory.get_all_workspaces() == []


def test_file_tree_per_dir_quota_keeps_late_top_level_entries(tmp_path):
    bridge = _bare_gui_bridge()
    project = tmp_path / "project"
    project.mkdir()
    big = project / "aaa_big"
    big.mkdir()
    for i in range(30):
        sub = big / f"sub_{i:03d}"
        sub.mkdir()
        for j in range(30):
            (sub / f"file_{j:03d}.txt").write_text("x", encoding="utf-8")
    (project / "zzz_last.txt").write_text("x", encoding="utf-8")

    tree = bridge._get_file_tree(str(project))
    top_names = [node["n"] for node in tree]
    assert "aaa_big" in top_names
    assert "zzz_last.txt" in top_names


def test_file_tree_shows_dot_entries_like_explorer(tmp_path):
    bridge = _bare_gui_bridge()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".idea").mkdir()
    (project / ".env.local").write_text("x", encoding="utf-8")
    (project / ".git").mkdir()
    (project / "src").mkdir()

    top_names = [node["n"] for node in bridge._get_file_tree(str(project))]
    assert ".idea" in top_names
    assert ".env.local" in top_names
    assert "src" in top_names
    assert ".git" not in top_names


def test_describe_permission_request_human_readable():
    from core.tool_presenter import describe_permission_request

    assert describe_permission_request(
        "execute_command", {"command": "mvn install"}
    ) == "执行命令：\nmvn install"
    override_summary = describe_permission_request(
        "execute_command", {"command": "Get-Date", "shell_id": "powershell"}
    )
    assert "解释器：powershell（显式覆盖）" in override_summary
    assert "Get-Date" in override_summary
    assert describe_permission_request(
        "write_file", {"file_path": "a.txt"}
    ) == "写入文件：a.txt"
    summary = describe_permission_request(
        "sandbox_outside_access",
        {"path": r"D:\backup", "operation": "list_directory"},
    )
    assert "沙盒外" in summary
    assert r"D:\backup" in summary
    assert "查看目录" in summary


def test_get_dir_children_scans_one_level_on_demand(tmp_path):
    bridge = _bare_gui_bridge()
    project = tmp_path / "project"
    project.mkdir()
    deep = project
    for name in ("l1", "l2", "l3", "l4", "l5", "l6"):
        deep = deep / name
        deep.mkdir()
    (deep / "leaf.txt").write_text("x", encoding="utf-8")
    (deep / "sub").mkdir()

    tree = bridge._get_file_tree(str(project))
    l1 = tree[0]
    l4 = l1["children"][0]["children"][0]["children"][0]
    assert l4["n"] == "l4"
    assert "children" not in l4

    res = bridge.get_dir_children(os.path.join(str(project), "l1", "l2", "l3", "l4"))
    assert res["ok"] is True
    assert [c["n"] for c in res["children"]] == ["l5"]

    leaf_dir = os.path.join(str(deep))
    res = bridge.get_dir_children(leaf_dir)
    names = [c["n"] for c in res["children"]]
    assert "leaf.txt" in names
    assert "sub" in names

    res = bridge.get_dir_children(os.path.join(str(deep), "leaf.txt"))
    assert res["ok"] is False
    assert res["children"] == []
    res = bridge.get_dir_children(str(tmp_path / "no_such_dir"))
    assert res["ok"] is False


class _AliveThread:

    def is_alive(self):
        return True


def test_delete_session_refused_while_turn_active():
    bridge = _bare_gui_bridge()
    bridge._memory.create_session_explicit("活会话", workdir="GLOBAL", source="GUI")
    bridge._active_turns = {"活会话": _AliveThread()}

    res = bridge.delete_session("活会话")

    assert res["ok"] is False and res["success"] is False
    assert "运行" in res["msg"]
    assert any(s.get("name") == "活会话" for s in bridge._memory.get_all_sessions(workdir=None, include_empty=True))


def test_rename_session_refused_while_turn_active():
    bridge = _bare_gui_bridge()
    bridge._memory.create_session_explicit("旧名", workdir="GLOBAL", source="GUI")
    bridge._active_turns = {"旧名": _AliveThread()}

    res = bridge.rename_session("旧名", "新名")

    assert res["ok"] is False and res["name"] == "旧名"
    assert any(s.get("name") == "旧名" for s in bridge._memory.get_all_sessions(workdir=None, include_empty=True))


def test_delete_session_clears_task_permission_grants():
    from core import permission_grants

    permission_grants._clear_all_for_tests()
    bridge = _bare_gui_bridge()
    bridge._memory.create_session_explicit("待删除", workdir="GLOBAL", source="GUI")
    permission_grants.grant_command("git push origin", task_id="待删除")

    result = bridge.delete_session("待删除")

    assert result["ok"] is True
    assert permission_grants.snapshot("待删除")["commands"] == set()


def test_rename_session_migrates_task_permission_grants():
    from core import permission_grants

    permission_grants._clear_all_for_tests()
    bridge = _bare_gui_bridge()
    bridge._memory.create_session_explicit("旧任务", workdir="GLOBAL", source="GUI")
    permission_grants.grant_command("git push origin", task_id="旧任务")

    result = bridge.rename_session("旧任务", "新任务")

    assert result["ok"] is True
    assert permission_grants.matches_command("git push origin dev", "旧任务") is False
    assert permission_grants.matches_command("git push origin dev", "新任务") is True


def _collect_events(bridge):
    events = []
    bridge.emit_event = lambda kind, data: events.append((kind, data))
    return events


def _session_names_in_payload(payload):
    return {s.get("name") for group in payload["sessions"] for s in group["sessions"]}


def test_rename_session_emits_session_renamed_event():
    bridge = _bare_gui_bridge()
    bridge._memory.create_session_explicit("旧名", workdir="GLOBAL", source="GUI")
    events = _collect_events(bridge)

    res = bridge.rename_session("旧名", "新名")

    assert res["ok"] is True and res["name"] == "新名"
    assert len(events) == 1
    kind, payload = events[0]
    assert kind == "session_renamed"
    assert payload["old"] == "旧名" and payload["new"] == "新名"
    assert payload["old_name"] == "旧名" and payload["new_name"] == "新名"
    assert "新名" in _session_names_in_payload(payload)


def test_rename_session_refused_while_turn_active_emits_no_event():
    bridge = _bare_gui_bridge()
    bridge._memory.create_session_explicit("旧名", workdir="GLOBAL", source="GUI")
    bridge._active_turns = {"旧名": _AliveThread()}
    events = _collect_events(bridge)

    res = bridge.rename_session("旧名", "新名")

    assert res["ok"] is False
    assert events == []


def test_rename_session_emit_failure_still_reports_success():
    bridge = _bare_gui_bridge()
    bridge._memory.create_session_explicit("旧名", workdir="GLOBAL", source="GUI")

    def _boom(kind, data):
        raise RuntimeError("窗口已销毁")

    bridge.emit_event = _boom

    res = bridge.rename_session("旧名", "新名")

    assert res["ok"] is True and res["name"] == "新名"
    assert any(
        s.get("name") == "新名"
        for s in bridge._memory.get_all_sessions(workdir=None, include_empty=True)
    )


def test_rename_session_snapshot_failure_returns_ok_without_sessions():
    bridge = _bare_gui_bridge()
    bridge._memory.create_session_explicit("旧名", workdir="GLOBAL", source="GUI")
    _collect_events(bridge)

    def _boom():
        raise RuntimeError("数据库繁忙")

    bridge.get_sessions = _boom

    res = bridge.rename_session("旧名", "新名")

    assert res["ok"] is True and res["name"] == "新名"
    assert res["sessions"] is None
