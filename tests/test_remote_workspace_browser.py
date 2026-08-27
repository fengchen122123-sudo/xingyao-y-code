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
