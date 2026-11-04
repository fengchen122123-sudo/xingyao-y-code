"""手机端远程浏览/选择工作区目录：browse_remote_dirs 与 select_workspace_by_path。"""

import os

import pytest

from gui_bridge import ApiBridge


@pytest.fixture
def bridge(tmp_path):
    b = ApiBridge.__new__(ApiBridge)
    b._lan_server = None
    b._lan_lock = None
    b._memory = None
    return b


def test_browse_remote_dirs_lists_drives_when_no_path(bridge, monkeypatch):
    """path 为空时返回盘符列表（Windows）。"""
    import gui_bridge as gb

    monkeypatch.setattr(os, "name", "nt", raising=False)
    result = bridge.browse_remote_dirs("")
    assert result["ok"] is True
    assert result["is_root"] is True
    assert any(item["path"] == "C:\\" for item in result["dirs"])


def test_browse_remote_dirs_lists_subdirs(bridge, tmp_path):
    """给定目录时返回其子目录列表（过滤符号链接与隐藏目录）。"""
    (tmp_path / "projA").mkdir()
    (tmp_path / "projB").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    result = bridge.browse_remote_dirs(str(tmp_path))
    assert result["ok"] is True
    assert result["is_root"] is False
    names = [d["name"] for d in result["dirs"]]
    assert "projA" in names and "projB" in names
    names = [d["name"] for d in result["dirs"]]
    assert "file.txt" not in names
    assert result["current"] == os.path.normpath(str(tmp_path))


def test_browse_remote_dirs_invalid_path(bridge, tmp_path):
    result = bridge.browse_remote_dirs(str(tmp_path / "不存在"))
    assert result["ok"] is False


def test_select_workspace_by_path_rejects_non_dir(bridge, tmp_path):
    """不存在或非目录路径直接拒绝，不落库不切换。"""
    result = bridge.select_workspace_by_path(str(tmp_path / "ghost"))
    assert result["ok"] is False


def test_select_workspace_by_path_requires_memory(bridge):
    """memory 未初始化时防护性拒绝（不应崩溃）。"""
    result = bridge.select_workspace_by_path("C:\\")
    assert result["ok"] is False


def _bridge_with_spy_memory(bridge, current_workdir):
    """构造带记录桩的 bridge：只观察归属是否被改写，不真建库。"""
    calls = []

    class SpyMemory:
        def add_workspace(self, path):
            calls.append(("add_workspace", path))

        def update_session_workdir(self, name, path):
            calls.append(("update_session_workdir", name, path))
            return True

    bridge._memory = SpyMemory()
    bridge._current_session = "chat"
    bridge._session_workdir = lambda name: current_workdir
    bridge._get_file_tree = lambda path: []
    bridge.get_sessions = lambda: []
    bridge._build_skill_list = lambda: []
    return calls


def test_select_workspace_by_path_keeps_global_session(bridge, tmp_path):
    """引入新工作区只登记目录，绝不把当前 GLOBAL 自由对话搬进新分组。

    回归：用户在「对话」分组聊着天引入新工作区，会话被自动归并进新目录
    ——引入文件夹不应改写任何已有会话的归属。
    """
    calls = _bridge_with_spy_memory(bridge, "GLOBAL")
    result = bridge.select_workspace_by_path(str(tmp_path))
    assert result["ok"] is True
    assert any(
        c[0] == "add_workspace" and os.path.normpath(c[1]) == os.path.normpath(str(tmp_path))
        for c in calls
    )
    assert not any(c[0] == "update_session_workdir" for c in calls)


def test_select_workspace_by_path_keeps_other_project_session(bridge, tmp_path):
    """当前会话已归属其他项目时同样不改写归属（原有保护不回退）。"""
    calls = _bridge_with_spy_memory(bridge, "D:\\other-project")
    result = bridge.select_workspace_by_path(str(tmp_path))
    assert result["ok"] is True
    assert not any(c[0] == "update_session_workdir" for c in calls)


def test_select_workspace_folder_keeps_global_session(bridge, tmp_path, monkeypatch):
    """桌面端原生对话框入口同口径：引入文件夹不迁移 GLOBAL 会话。"""
    import sys
    import types

    calls = _bridge_with_spy_memory(bridge, "GLOBAL")

    class FakeWindow:
        def create_file_dialog(self, kind, directory=None):
            return [str(tmp_path)]

    fake_webview = types.SimpleNamespace(FOLDER_DIALOG=1)
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    bridge._window = FakeWindow()
    result = bridge.select_workspace_folder()
    assert result["ok"] is True
    assert any(c[0] == "add_workspace" for c in calls)
    assert not any(c[0] == "update_session_workdir" for c in calls)
