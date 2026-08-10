import os

import gui_bridge
from core.memory import MemoryManager
from gui_bridge import ApiBridge


def _bare_gui_bridge(db=":memory:"):
    """不经 __init__ 构造裸实例，挂一个内存版 MemoryManager。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = MemoryManager(db)
    bridge._current_session = "测试会话"
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
