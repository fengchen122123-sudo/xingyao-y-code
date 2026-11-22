# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui_bridge import ApiBridge as GuiBridge


def _make_bridge_stub(memory=None, current_session=""):
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
        bridge = self._make_bridge_with_tmp_memory(current_session="其他会话")
        bridge._memory.ensure_session("tokenrhythm(5)")

        res = bridge.search_sessions("tok")
        names = [s["name"] for s in res["sessions"]]
        self.assertIn("tokenrhythm(5)", names)
        self.assertEqual(names[0], "tokenrhythm(5)")
        self.assertEqual(res["sessions"][0]["count"], 0)

    def test_default_named_empty_session_skipped(self):
        bridge = self._make_bridge_with_tmp_memory(current_session="其他会话")
        bridge._memory.ensure_session("session_309070")
        bridge._memory.ensure_session("session_abc123")
        bridge._memory.ensure_session("正常会话")
        bridge._memory.add_message("正常会话", "user", "hi")

        names_all = [s["name"] for s in bridge.search_sessions("")["sessions"]]
        self.assertEqual(names_all, ["正常会话"])
        self.assertEqual(bridge.search_sessions("309070")["sessions"], [])

    def test_sibling_sessions_ranked_before_other_matches(self):
        bridge = self._make_bridge_with_tmp_memory(current_session="其他会话")
        for name in ("X", "X(2)", "X(3)"):
            bridge._memory.ensure_session(name)
            bridge._memory.add_message(name, "user", "hi")
        bridge._memory.ensure_session("prefixed_x_very_long_name")
        bridge._memory.add_message("prefixed_x_very_long_name", "user", "hi")

        res = bridge.search_sessions("x", limit=3)
        names = [s["name"] for s in res["sessions"]]
        self.assertEqual(set(names), {"X", "X(2)", "X(3)"})
        self.assertEqual(names[0], "X")
        self.assertNotIn("prefixed_x_very_long_name", names)

    def test_current_session_always_excluded(self):
        bridge = self._make_bridge_with_tmp_memory(current_session="X(2)")
        for name in ("X", "X(2)"):
            bridge._memory.ensure_session(name)
            bridge._memory.add_message(name, "user", "hi")

        names = [s["name"] for s in bridge.search_sessions("x")["sessions"]]
        self.assertNotIn("X(2)", names)
        self.assertIn("X", names)

    def test_draft_new_session_excludes_nothing(self):
        bridge = self._make_bridge_with_tmp_memory(current_session="缓存改造")
        for name in ("缓存改造", "其他会话"):
            bridge._memory.ensure_session(name)
            bridge._memory.add_message(name, "user", "hi")

        legacy = [s["name"] for s in bridge.search_sessions("")["sessions"]]
        self.assertNotIn("缓存改造", legacy)

        draft = [
            s["name"]
            for s in bridge.search_sessions("", current_session="")["sessions"]
        ]
        self.assertIn("缓存改造", draft)
        self.assertIn("其他会话", draft)

    def test_explicit_current_session_overrides_backend_state(self):
        bridge = self._make_bridge_with_tmp_memory(current_session="后端以为的当前会话")
        for name in ("后端以为的当前会话", "前端所在的会话"):
            bridge._memory.ensure_session(name)
            bridge._memory.add_message(name, "user", "hi")

        names = [
            s["name"]
            for s in bridge.search_sessions("", current_session="前端所在的会话")["sessions"]
        ]
        self.assertNotIn("前端所在的会话", names)
        self.assertIn("后端以为的当前会话", names)

    def test_archived_session_excluded_from_suggestions(self):
        bridge = self._make_bridge_with_tmp_memory(current_session="")
        for name in ("活跃会话", "已归档会话"):
            bridge._memory.ensure_session(name)
            bridge._memory.add_message(name, "user", "hi")
        self.assertTrue(bridge._memory.archive_session("已归档会话"))

        names = [s["name"] for s in bridge.search_sessions("")["sessions"]]
        self.assertIn("活跃会话", names)
        self.assertNotIn("已归档会话", names)
        self.assertEqual(bridge.search_sessions("已归档")["sessions"], [])
        self.assertEqual(
            [s["name"] for s in bridge.search_sessions("活跃")["sessions"]],
            ["活跃会话"],
        )

    def test_messages_session_id_index_created(self):
        bridge = self._make_bridge_with_tmp_memory()
        rows = bridge._memory.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages'"
        ).fetchall()
        self.assertIn("idx_messages_session", [r[0] for r in rows])

    def test_search_matches_full_first_message_beyond_truncated_name(self):
        bridge = self._make_bridge_with_tmp_memory(current_session="")
        bridge._memory.ensure_session("帮我看一下缓存命中率")
        bridge._memory.add_message(
            "帮我看一下缓存命中率",
            "user",
            "帮我看一下缓存命中率为什么忽高忽低，顺便检查 eviction 策略",
        )

        names = [s["name"] for s in bridge.search_sessions("eviction")["sessions"]]
        self.assertEqual(names, ["帮我看一下缓存命中率"])

    def test_search_text_keeps_first_user_message_only(self):
        bridge = self._make_bridge_with_tmp_memory(current_session="")
        bridge._memory.ensure_session("会话")
        bridge._memory.add_message("会话", "user", "第一条：初始化仓库")
        bridge._memory.add_message("会话", "assistant", "好的")
        bridge._memory.add_message("会话", "user", "第二条：发布上线")

        self.assertEqual(
            [s["name"] for s in bridge.search_sessions("初始化")["sessions"]],
            ["会话"],
        )
        self.assertEqual(bridge.search_sessions("发布上线")["sessions"], [])

    def test_name_hits_rank_before_body_hits(self):
        bridge = self._make_bridge_with_tmp_memory(current_session="")
        bridge._memory.ensure_session("缓存改造")
        bridge._memory.add_message("缓存改造", "user", "hi")
        bridge._memory.ensure_session("登录问题")
        bridge._memory.add_message("登录问题", "user", "顺便聊聊缓存策略")

        names = [s["name"] for s in bridge.search_sessions("缓存")["sessions"]]
        self.assertEqual(names[0], "缓存改造")
        self.assertEqual(set(names), {"缓存改造", "登录问题"})

    def test_archived_session_not_matched_by_body(self):
        bridge = self._make_bridge_with_tmp_memory(current_session="")
        bridge._memory.ensure_session("归档目标")
        bridge._memory.add_message("归档目标", "user", "这里提到了 quantum 方案")
        self.assertTrue(bridge._memory.archive_session("归档目标"))

        self.assertEqual(bridge.search_sessions("quantum")["sessions"], [])

    def test_search_text_backfilled_on_legacy_database(self):
        import shutil
        import tempfile

        from core.memory import MemoryManager

        tmp_dir = tempfile.mkdtemp()
        db_path = str(Path(tmp_dir) / "legacy.db")
        mem = MemoryManager(db_path)
        mem.ensure_session("老会话")
        mem.add_message("老会话", "user", "历史问题：数据库迁移失败")
        mem.conn.execute("ALTER TABLE sessions DROP COLUMN search_text")
        mem.conn.commit()
        mem.conn.close()

        reopened = MemoryManager(db_path)
        try:
            row = reopened.conn.execute(
                "SELECT search_text FROM sessions WHERE session_name = '老会话'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn("数据库迁移失败", row[0] or "")
        finally:
            reopened.conn.close()
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_search_text_strips_injected_runtime_context(self):
        bridge = self._make_bridge_with_tmp_memory(current_session="")
        bridge._memory.ensure_session("上下文会话")
        bridge._memory.add_message(
            "上下文会话",
            "user",
            "帮我修一下启动报错\n\n【\u200b当前运行上下文】\n当前模型：deepseek",
        )

        self.assertEqual(
            [s["name"] for s in bridge.search_sessions("启动报错")["sessions"]],
            ["上下文会话"],
        )
        self.assertEqual(bridge.search_sessions("deepseek")["sessions"], [])


if __name__ == "__main__":
    unittest.main()
