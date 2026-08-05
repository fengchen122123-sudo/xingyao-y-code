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
