# -*- coding: utf-8 -*-
"""@ 文件补全扫描根目录回归测试。

背景（bug）：GUI 多会话架构用线程本地 workdir 绑定替代了进程级 os.chdir，
进程 cwd 永远停在启动目录。_list_workspace_files_for_suggest 旧实现
os.walk(".") 扫的是启动目录，导致输入 @ 弹出的文件不是当前项目文件夹的文件。

回归点：
1. 扫描根目录必须是当前会话登记的 workdir，而不是进程 cwd；
2. TTL 缓存必须按扫描根目录键控，切换会话/项目后不能命中旧目录清单。
"""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gui_bridge


def _make_bridge_stub(session_workdir):
    """构造挂接真实 ApiBridge 方法的最小桩，避免实例化重量级桥对象。"""
    stub = types.SimpleNamespace()
    stub._current_session = "sess_test"
    stub._session_workdir = lambda name: session_workdir
    # 绑定真实方法（未绑定函数直接挂到桩上，self 即 stub）
    stub._suggest_scan_root = types.MethodType(ApiBridge._suggest_scan_root, stub)
    stub._list_workspace_files_for_suggest = types.MethodType(
        ApiBridge._list_workspace_files_for_suggest, stub
    )
    return stub


from gui_bridge import ApiBridge  # noqa: E402


class WorkspaceSuggestScanRootTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj_a = Path(self._tmp.name) / "proj_a"
        self.proj_b = Path(self._tmp.name) / "proj_b"
        for proj, marker in ((self.proj_a, "alpha_marker.txt"), (self.proj_b, "beta_marker.txt")):
            proj.mkdir()
            (proj / marker).write_text("x", encoding="utf-8")
        # 启动目录（进程 cwd 模拟）：与两个项目目录都不同
        self.launch_dir = Path(self._tmp.name) / "launch_dir"
        self.launch_dir.mkdir()
        (self.launch_dir / "launch_marker.txt").write_text("x", encoding="utf-8")
        self._orig_cwd = os.getcwd()
        os.chdir(self.launch_dir)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        self._tmp.cleanup()

    def test_scan_root_is_session_workdir_not_cwd(self):
        """扫描根目录 = 会话 workdir，而不是进程 cwd（启动目录）。"""
        stub = _make_bridge_stub(str(self.proj_a))
        self.assertEqual(stub._suggest_scan_root(), str(self.proj_a))
        files = stub._list_workspace_files_for_suggest()
        self.assertIn("alpha_marker.txt", files)
        self.assertNotIn("launch_marker.txt", files)
        self.assertNotIn("beta_marker.txt", files)

    def test_cache_keyed_by_scan_root(self):
        """TTL 内切换会话工作区，新目录不能命中旧缓存。"""
        stub = _make_bridge_stub(str(self.proj_a))
        files_a = stub._list_workspace_files_for_suggest()
        self.assertIn("alpha_marker.txt", files_a)
        # 不超过 TTL（5s）的情况下切到项目 B
        stub._session_workdir = lambda name: str(self.proj_b)
        files_b = stub._list_workspace_files_for_suggest()
        self.assertIn("beta_marker.txt", files_b)
        self.assertNotIn("alpha_marker.txt", files_b)

    def test_global_session_falls_back_to_cwd(self):
        """GLOBAL 会话兜底进程 cwd（与旧行为一致）。"""
        stub = _make_bridge_stub("GLOBAL")
        self.assertEqual(stub._suggest_scan_root(), str(self.launch_dir))
        files = stub._list_workspace_files_for_suggest()
        self.assertIn("launch_marker.txt", files)


if __name__ == "__main__":
    unittest.main()
