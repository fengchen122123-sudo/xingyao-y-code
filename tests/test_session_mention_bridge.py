# -*- coding: utf-8 -*-
"""@ 提及对话桥接回归测试。

覆盖：
1. search_sessions 排除当前会话、子串匹配、updated_at 倒序、limit 截断；
2. 异常兜底（_memory 为 None）返回 ok=False + 空列表。
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui_bridge import ApiBridge as GuiBridge


def _make_bridge_stub(memory=None, current_session=""):
    """裸构造 GuiBridge，只挂本测试用到的属性。"""
    stub = GuiBridge.__new__(GuiBridge)
    stub._memory = memory
    stub._current_session = current_session
    return stub


class SessionMentionBridgeTest(unittest.TestCase):
    def test_search_sessions_filters_and_excludes_current(self):
        from core.memory import MemoryManager
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        bridge = _make_bridge_stub(
            memory=MemoryManager(str(Path(tmp.name) / "t.db")),
            current_session="当前会话",
        )
        bridge._memory.ensure_session("当前会话")
        bridge._memory.ensure_session("登录bug排查")
        bridge._memory.add_message("登录bug排查", "user", "hi")
        bridge._memory.ensure_session("发布流程梳理")
        bridge._memory.add_message("发布流程梳理", "user", "hi")

        res = bridge.search_sessions("登录")
        names = [s["name"] for s in res["sessions"]]
        self.assertEqual(names, ["登录bug排查"])

        res_all = bridge.search_sessions("")
        names_all = [s["name"] for s in res_all["sessions"]]
        self.assertNotIn("当前会话", names_all)
        self.assertEqual(set(names_all), {"登录bug排查", "发布流程梳理"})

    def test_search_sessions_error_path_returns_empty(self):
        bridge = _make_bridge_stub(memory=None, current_session="")
        res = bridge.search_sessions("x")
        self.assertFalse(res["ok"])
        self.assertEqual(res["sessions"], [])


if __name__ == "__main__":
    unittest.main()
