# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gui_bridge


def _make_bridge_stub(session_workdir):
    stub = types.SimpleNamespace()
    stub._current_session = "sess_test"
    stub._session_workdir = lambda name: session_workdir
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
        self.launch_dir = Path(self._tmp.name) / "launch_dir"
        self.launch_dir.mkdir()
        (self.launch_dir / "launch_marker.txt").write_text("x", encoding="utf-8")
        self._orig_cwd = os.getcwd()
        os.chdir(self.launch_dir)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        self._tmp.cleanup()

    def test_scan_root_is_session_workdir_not_cwd(self):
        stub = _make_bridge_stub(str(self.proj_a))
        self.assertEqual(stub._suggest_scan_root(), str(self.proj_a))
        files = stub._list_workspace_files_for_suggest()
        self.assertIn("alpha_marker.txt", files)
        self.assertNotIn("launch_marker.txt", files)
        self.assertNotIn("beta_marker.txt", files)

    def test_cache_keyed_by_scan_root(self):
        stub = _make_bridge_stub(str(self.proj_a))
        files_a = stub._list_workspace_files_for_suggest()
        self.assertIn("alpha_marker.txt", files_a)
        stub._session_workdir = lambda name: str(self.proj_b)
        files_b = stub._list_workspace_files_for_suggest()
        self.assertIn("beta_marker.txt", files_b)
        self.assertNotIn("alpha_marker.txt", files_b)

    def test_global_session_falls_back_to_cwd(self):
        stub = _make_bridge_stub("GLOBAL")
        self.assertEqual(stub._suggest_scan_root(), str(self.launch_dir))
        files = stub._list_workspace_files_for_suggest()
        self.assertIn("launch_marker.txt", files)


if __name__ == "__main__":
    unittest.main()
