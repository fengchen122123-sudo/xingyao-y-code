# -*- coding: utf-8 -*-
"""@ 提及对话桥接回归测试。

覆盖：
1. search_sessions 排除当前会话、子串匹配、updated_at 倒序、limit 截断；
2. 异常兜底（_memory 为 None）返回 ok=False + 空列表；
3. 空会话策略：重命名后的空会话进候选，默认名（session_xxxxxx）空会话被跳过；
4. 有 query 时排序：前缀命中 + 名字长度升序，同名兄弟不被挤出前 N 条。
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
        bridge = self._make_bridge_with_tmp_memory(current_session="当前会话")
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

    def _make_bridge_with_tmp_memory(self, current_session=""):
        """建一个挂临时 SQLite 的裸 bridge（与上面的用例风格一致）。

        Windows 下必须先关连接再删库文件，否则句柄未释放会清理失败。
        """
        import shutil
        import tempfile

        from core.memory import MemoryManager

        tmp_dir = tempfile.mkdtemp()
        mem = MemoryManager(str(Path(tmp_dir) / "t.db"))

        def _cleanup():
            try:
                mem.conn.close()
            except Exception:
                pass
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.addCleanup(_cleanup)
        return _make_bridge_stub(memory=mem, current_session=current_session)

    def test_renamed_empty_session_included(self):
        """重命名后的空会话（无任何消息）必须出现在 @ 候选中。"""
        bridge = self._make_bridge_with_tmp_memory(current_session="其他会话")
        bridge._memory.ensure_session("tokenrhythm(5)")  # 人工重命名、尚未发言

        res = bridge.search_sessions("tok")
        names = [s["name"] for s in res["sessions"]]
        self.assertIn("tokenrhythm(5)", names)
        self.assertEqual(names[0], "tokenrhythm(5)")
        self.assertEqual(res["sessions"][0]["count"], 0)

    def test_default_named_empty_session_skipped(self):
        """默认名（session_xxxxxx）的空会话属于垃圾临时会话，不进候选。"""
        bridge = self._make_bridge_with_tmp_memory(current_session="其他会话")
        bridge._memory.ensure_session("session_309070")
        bridge._memory.ensure_session("session_abc123")
        # 再加一个有消息的会话，确认不是整体返空
        bridge._memory.ensure_session("正常会话")
        bridge._memory.add_message("正常会话", "user", "hi")

        names_all = [s["name"] for s in bridge.search_sessions("")["sessions"]]
        self.assertEqual(names_all, ["正常会话"])
        self.assertEqual(bridge.search_sessions("309070")["sessions"], [])

    def test_sibling_sessions_ranked_before_other_matches(self):
        """同名兄弟场景：query 命中的 X/X(2)/X(3) 不能被更长的匹配项挤出前 N 条。"""
        bridge = self._make_bridge_with_tmp_memory(current_session="其他会话")
        for name in ("X", "X(2)", "X(3)"):
            bridge._memory.ensure_session(name)
            bridge._memory.add_message(name, "user", "hi")
        # 名字更长且更晚活跃的“非前缀”匹配项：旧实现会把它排到前面
        bridge._memory.ensure_session("prefixed_x_very_long_name")
        bridge._memory.add_message("prefixed_x_very_long_name", "user", "hi")

        res = bridge.search_sessions("x", limit=3)
        names = [s["name"] for s in res["sessions"]]
        self.assertEqual(set(names), {"X", "X(2)", "X(3)"})
        self.assertEqual(names[0], "X")
        self.assertNotIn("prefixed_x_very_long_name", names)

    def test_current_session_always_excluded(self):
        """当前会话即使有消息也被排除。"""
        bridge = self._make_bridge_with_tmp_memory(current_session="X(2)")
        for name in ("X", "X(2)"):
            bridge._memory.ensure_session(name)
            bridge._memory.add_message(name, "user", "hi")

        names = [s["name"] for s in bridge.search_sessions("x")["sessions"]]
        self.assertNotIn("X(2)", names)
        self.assertIn("X", names)


if __name__ == "__main__":
    unittest.main()
