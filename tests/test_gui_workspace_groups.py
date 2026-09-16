import os
import threading

import gui_bridge
from core.memory import MemoryManager
from gui_bridge import ApiBridge


def _bare_gui_bridge(db=":memory:"):
    """不经 __init__ 构造裸实例，挂一个内存版 MemoryManager。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = MemoryManager(db)
    bridge._current_session = "测试会话"
    bridge._turn_start_lock = threading.RLock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    return bridge


def test_get_sessions_does_not_register_app_install_dir(monkeypatch):
    """cwd 是应用安装目录时，不得自动注册为项目分组（幻影分组根因）。"""
    bridge = _bare_gui_bridge()
    app_dir = gui_bridge._app_install_dir()
    monkeypatch.setattr(os, "getcwd", lambda: app_dir)

    groups = bridge.get_sessions()
    folder_paths = {g["folder_path"] for g in groups}
    assert app_dir not in folder_paths
    assert bridge._memory.get_all_workspaces() == []


def test_get_sessions_registers_normal_project_dir(monkeypatch, tmp_path):
    """cwd 是普通项目目录时，仍然自动注册分组（原有行为不破坏）。"""
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
    """删除安装目录幻影分组后，刷新列表不会再把它加回来。"""
    bridge = _bare_gui_bridge()
    app_dir = gui_bridge._app_install_dir()
    monkeypatch.setattr(os, "getcwd", lambda: app_dir)
    # 模拟老版本遗留：workspaces 表里已有安装目录记录
    bridge._memory.add_workspace(app_dir)
    assert app_dir in bridge._memory.get_all_workspaces()

    res = bridge.delete_folder_group(app_dir)
    assert res["success"] is True
    folder_paths = {g["folder_path"] for g in res["sessions"]}
    assert app_dir not in folder_paths
    assert bridge._memory.get_all_workspaces() == []


def test_file_tree_per_dir_quota_keeps_late_top_level_entries(tmp_path):
    """回归：字母序靠前目录的子树不得吃满配额导致靠后的顶层条目被丢弃。"""
    bridge = _bare_gui_bridge()
    project = tmp_path / "project"
    project.mkdir()
    # 字母序靠前的目录：嵌套子树总条目数远超旧版全局配额（旧实现深优扫描
    # 会在该子树内耗尽全局预算，丢弃靠后的顶层条目）
    big = project / "aaa_big"
    big.mkdir()
    for i in range(30):
        sub = big / f"sub_{i:03d}"
        sub.mkdir()
        for j in range(30):
            (sub / f"file_{j:03d}.txt").write_text("x", encoding="utf-8")
    # 字母序靠后的顶层条目必须仍然可见
    (project / "zzz_last.txt").write_text("x", encoding="utf-8")

    tree = bridge._get_file_tree(str(project))
    top_names = [node["n"] for node in tree]
    assert "aaa_big" in top_names
    assert "zzz_last.txt" in top_names


def test_file_tree_shows_dot_entries_like_explorer(tmp_path):
    """点开头的隐藏条目与文件资源管理器一致照常显示（仅跳过 .git 等重目录）。"""
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
    """权限询问文案必须写清楚执行什么命令 / 查看什么文件，不给 JSON 原文。"""
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
    """懒加载接口：任意深度目录都能按需取下一层（初始树深度上限之外的层级）。"""
    bridge = _bare_gui_bridge()
    project = tmp_path / "project"
    project.mkdir()
    deep = project
    for name in ("l1", "l2", "l3", "l4", "l5", "l6"):
        deep = deep / name
        deep.mkdir()
    (deep / "leaf.txt").write_text("x", encoding="utf-8")
    (deep / "sub").mkdir()

    # 初始树深度 4：l3 可展开出 l4，但 l4 不再有预载 children
    tree = bridge._get_file_tree(str(project))
    l1 = tree[0]
    l4 = l1["children"][0]["children"][0]["children"][0]
    assert l4["n"] == "l4"
    assert "children" not in l4  # 超出深度，未预载

    # 懒加载：对 l4 及其之下任意层级都能取到下一层
    res = bridge.get_dir_children(os.path.join(str(project), "l1", "l2", "l3", "l4"))
    assert res["ok"] is True
    assert [c["n"] for c in res["children"]] == ["l5"]

    leaf_dir = os.path.join(str(deep))
    res = bridge.get_dir_children(leaf_dir)
    names = [c["n"] for c in res["children"]]
    assert "leaf.txt" in names
    assert "sub" in names

    # 非目录 / 不存在路径：返回 ok=False 与空 children，前端兜底为空数组
    res = bridge.get_dir_children(os.path.join(str(deep), "leaf.txt"))
    assert res["ok"] is False
    assert res["children"] == []
    res = bridge.get_dir_children(str(tmp_path / "no_such_dir"))
    assert res["ok"] is False


class _AliveThread:
    """模拟存活的回合线程（is_alive 恒真）。"""

    def is_alive(self):
        return True


def test_delete_session_refused_while_turn_active():
    """M21：回合运行中删除会话必须拒绝，否则回合线程继续写已删会话（幽灵数据）。"""
    bridge = _bare_gui_bridge()
    bridge._memory.create_session_explicit("活会话", workdir="GLOBAL", source="GUI")
    bridge._active_turns = {"活会话": _AliveThread()}

    res = bridge.delete_session("活会话")

    assert res["ok"] is False and res["success"] is False
    assert "运行" in res["msg"]
    assert any(s.get("name") == "活会话" for s in bridge._memory.get_all_sessions(workdir=None, include_empty=True))


def test_rename_session_refused_while_turn_active():
    """M21：回合运行中重命名必须拒绝，否则终态事件带旧名导致前端状态失配。"""
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
    """替换 emit_event 收集事件，返回 [(event_type, payload), ...]。"""
    events = []
    bridge.emit_event = lambda kind, data: events.append((kind, data))
    return events


def _session_names_in_payload(payload):
    return {s.get("name") for group in payload["sessions"] for s in group["sessions"]}


def test_rename_session_emits_session_renamed_event():
    """手动重命名成功后推送 session_renamed，前端据此迁移运行态与当前会话名。"""
    bridge = _bare_gui_bridge()
    bridge._memory.create_session_explicit("旧名", workdir="GLOBAL", source="GUI")
    events = _collect_events(bridge)

    res = bridge.rename_session("旧名", "新名")

    assert res["ok"] is True and res["name"] == "新名"
    assert len(events) == 1
    kind, payload = events[0]
    assert kind == "session_renamed"
    assert payload["old"] == "旧名" and payload["new"] == "新名"
    # 兼容旧字段名，与 _rename_session_to_title 的负载保持一致
    assert payload["old_name"] == "旧名" and payload["new_name"] == "新名"
    assert "新名" in _session_names_in_payload(payload)


def test_rename_session_refused_while_turn_active_emits_no_event():
    """回合运行中重命名被拒时不推事件，避免前端把状态迁到未生效的新名。"""
    bridge = _bare_gui_bridge()
    bridge._memory.create_session_explicit("旧名", workdir="GLOBAL", source="GUI")
    bridge._active_turns = {"旧名": _AliveThread()}
    events = _collect_events(bridge)

    res = bridge.rename_session("旧名", "新名")

    assert res["ok"] is False
    assert events == []


def test_rename_session_emit_failure_still_reports_success():
    """事件推送失败不改判结果：DB 与授权迁移已生效，报失败会让前端以为没改名。"""
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
    """取列表失败仍返回 ok=True 且 sessions=None：前端据此跳过列表覆盖，不清空侧栏。"""
    bridge = _bare_gui_bridge()
    bridge._memory.create_session_explicit("旧名", workdir="GLOBAL", source="GUI")
    _collect_events(bridge)

    def _boom():
        raise RuntimeError("数据库繁忙")

    bridge.get_sessions = _boom

    res = bridge.rename_session("旧名", "新名")

    assert res["ok"] is True and res["name"] == "新名"
    assert res["sessions"] is None


def test_terminal_command_normalizes_global_workdir(monkeypatch, tmp_path):
    """M19：GLOBAL 自由对话的内嵌终端不能把字符串 "GLOBAL" 当 Popen cwd。"""
    import subprocess

    bridge = _bare_gui_bridge()
    bridge._current_session = "global-session"
    bridge._term_proc = None
    bridge._term_lock = __import__("threading").Lock()
    bridge._session_workdir = lambda _session: "GLOBAL"
    bridge.emit_event = lambda _kind, _data: None

    monkeypatch.chdir(tmp_path)
    captured = {}

    class FakeProc:
        def __init__(self):
            self.stdout = type("Out", (), {"readline": lambda self: b""})()

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    res = bridge.run_terminal_command("echo hi")

    assert res["ok"] is True
    assert captured["cwd"] == gui_bridge._resolve_real_path(str(tmp_path))
