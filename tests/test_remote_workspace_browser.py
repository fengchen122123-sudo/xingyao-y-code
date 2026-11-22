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
    import gui_bridge as gb

    monkeypatch.setattr(os, "name", "nt", raising=False)
    result = bridge.browse_remote_dirs("")
    assert result["ok"] is True
    assert result["is_root"] is True
    assert any(item["path"] == "C:\\" for item in result["dirs"])


def test_browse_remote_dirs_lists_subdirs(bridge, tmp_path):
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
    result = bridge.select_workspace_by_path(str(tmp_path / "ghost"))
    assert result["ok"] is False


def test_select_workspace_by_path_requires_memory(bridge):
    result = bridge.select_workspace_by_path("C:\\")
    assert result["ok"] is False


def _bridge_with_spy_memory(bridge, current_workdir):
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
    calls = _bridge_with_spy_memory(bridge, "GLOBAL")
    result = bridge.select_workspace_by_path(str(tmp_path))
    assert result["ok"] is True
    assert any(
        c[0] == "add_workspace" and os.path.normpath(c[1]) == os.path.normpath(str(tmp_path))
        for c in calls
    )
    assert not any(c[0] == "update_session_workdir" for c in calls)


def test_select_workspace_by_path_keeps_other_project_session(bridge, tmp_path):
    calls = _bridge_with_spy_memory(bridge, "D:\\other-project")
    result = bridge.select_workspace_by_path(str(tmp_path))
    assert result["ok"] is True
    assert not any(c[0] == "update_session_workdir" for c in calls)


def test_select_workspace_folder_keeps_global_session(bridge, tmp_path, monkeypatch):
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
