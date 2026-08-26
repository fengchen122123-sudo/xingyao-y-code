# -*- coding: utf-8 -*-
"""read_file_preview 对目录 / 不存在路径的错误信息测试。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui_bridge import ApiBridge


def _make_bridge_with_workdir(workdir: str) -> ApiBridge:
    bridge = ApiBridge(mcp_manager=None)
    bridge._session_workdir = lambda session_name="": workdir  # type: ignore[method-assign]
    bridge._current_session = "test-session"
    return bridge


def test_preview_directory_returns_folder_message():
    with tempfile.TemporaryDirectory() as tmp:
        sub = os.path.join(tmp, "subdir")
        os.makedirs(sub)
        bridge = _make_bridge_with_workdir(tmp)
        res = bridge.read_file_preview(sub)
        assert res["ok"] is False
        # 目录要有专属、准确的提示，而不是误导性的「文件不存在」
        assert "文件夹" in res["msg"] or "目录" in res["msg"]


def test_preview_missing_file_keeps_missing_message():
    with tempfile.TemporaryDirectory() as tmp:
        bridge = _make_bridge_with_workdir(tmp)
        res = bridge.read_file_preview(os.path.join(tmp, "no_such.md"))
        assert res["ok"] is False


def test_preview_trailing_slash_directory():
    with tempfile.TemporaryDirectory() as tmp:
        sub = os.path.join(tmp, "dist")
        os.makedirs(sub)
        bridge = _make_bridge_with_workdir(tmp)
        # 模型常输出带尾反斜杠的目录路径：E:\xxx\dist\
        res = bridge.read_file_preview(sub.rstrip("\\/") + "\\")
        assert res["ok"] is False
        assert "文件夹" in res["msg"] or "目录" in res["msg"]


def test_check_paths_kind_classifies_file_dir_missing():
    with tempfile.TemporaryDirectory() as tmp:
        f = os.path.join(tmp, "a.md")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("x")
        d = os.path.join(tmp, "sub")
        os.makedirs(d)
        bridge = _make_bridge_with_workdir(tmp)
        res = bridge.check_paths_kind([f, d, os.path.join(tmp, "nope.md")])
        assert [r["kind"] for r in res] == ["file", "dir", "missing"]
