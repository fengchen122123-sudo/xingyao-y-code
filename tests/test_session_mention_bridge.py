# -*- coding: utf-8 -*-
"""@ 提及对话桥接回归测试。

覆盖：
1. search_sessions 排除当前会话、子串匹配、updated_at 倒序、limit 截断；
2. 异常兜底（_memory 为 None）返回 ok=False + 空列表；
3. 空会话策略：重命名后的空会话进候选，默认名（session_xxxxxx）空会话被跳过；
4. 有 query 时排序：前缀命中 + 名字长度升序，同名兄弟不被挤出前 N 条。
"""
import sys
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

    def test_draft_new_session_excludes_nothing(self):
        """草稿新会话（current_session 传空串）不得排除任何会话。

        回归：启动后点「新会话」进入空白草稿时，后端 _current_session 仍停留在
        上一个真实会话（会话懒落库，落库才切换）；若继续按它排除，用户在这张
        空白草稿里 @ 上一个对话会永远搜不到，改名字也无效——排除依据是会话
        身份而不是名字。
        """
        bridge = self._make_bridge_with_tmp_memory(current_session="缓存改造")
        for name in ("缓存改造", "其他会话"):
            bridge._memory.ensure_session(name)
            bridge._memory.add_message(name, "user", "hi")

        # 旧调用（不传第三参）语义不变：仍按后端当前会话排除
        legacy = [s["name"] for s in bridge.search_sessions("")["sessions"]]
        self.assertNotIn("缓存改造", legacy)

        # 草稿态：显式传空串，任何会话都不排除，目标会话必须可见
        draft = [
            s["name"]
            for s in bridge.search_sessions("", current_session="")["sessions"]
        ]
        self.assertIn("缓存改造", draft)
        self.assertIn("其他会话", draft)

    def test_explicit_current_session_overrides_backend_state(self):
        """显式传入的 current_session 覆盖后端 _current_session（前端才是权威）。"""
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
        """已归档会话不参与 @ 候选：归档是用户主动收起的上下文。

        回归：search_sessions 必须显式带 include_archived=False。若哪天
        get_all_sessions 的默认值被改成包含归档，@ 列表会重新被收起的历史
        会话污染，且会挤占前 4 个会话名额。
        """
        bridge = self._make_bridge_with_tmp_memory(current_session="")
        for name in ("活跃会话", "已归档会话"):
            bridge._memory.ensure_session(name)
            bridge._memory.add_message(name, "user", "hi")
        self.assertTrue(bridge._memory.archive_session("已归档会话"))

        names = [s["name"] for s in bridge.search_sessions("")["sessions"]]
        self.assertIn("活跃会话", names)
        self.assertNotIn("已归档会话", names)
        # query 精确命中归档会话名时也不能被检索出来
        self.assertEqual(bridge.search_sessions("已归档")["sessions"], [])
        # 未归档会话仍可正常命中
        self.assertEqual(
            [s["name"] for s in bridge.search_sessions("活跃")["sessions"]],
            ["活跃会话"],
        )

    def test_messages_session_id_index_created(self):
        """messages(session_id) 必须建索引。

        @ 补全会话列表走 get_all_sessions 的 LEFT JOIN messages + COUNT，
        每次按键都会执行。缺索引时 SQLite 只能全表 SCAN messages
        （真机 900MB 库单次实测 82~95ms），这是输入 @ 后明显卡顿的根因。
        """
        bridge = self._make_bridge_with_tmp_memory()
        rows = bridge._memory.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages'"
        ).fetchall()
        self.assertIn("idx_messages_session", [r[0] for r in rows])

    def test_search_matches_full_first_message_beyond_truncated_name(self):
        """会话名被截断到 12 字，@ 检索仍要能命中完整首条消息。

        会话名只是首条消息的截断，用户记住的关键词经常落在第 12 字之后
        （例如名字「帮我看一下缓存命中率」，想搜「eviction」）。只按名字
        匹配时这类目标永远搜不到，表现为「@ 里什么都出不来」。
        """
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
        """search_text 只记录首条 user 消息，后续消息不能覆盖它。"""
        bridge = self._make_bridge_with_tmp_memory(current_session="")
        bridge._memory.ensure_session("会话")
        bridge._memory.add_message("会话", "user", "第一条：初始化仓库")
        bridge._memory.add_message("会话", "assistant", "好的")
        bridge._memory.add_message("会话", "user", "第二条：发布上线")

        self.assertEqual(
            [s["name"] for s in bridge.search_sessions("初始化")["sessions"]],
            ["会话"],
        )
        # 后续 user 消息不进入 search_text，避免检索文本漂移到会话中途
        self.assertEqual(bridge.search_sessions("发布上线")["sessions"], [])

    def test_name_hits_rank_before_body_hits(self):
        """名字命中的会话必须排在「仅正文命中」的会话前面。"""
        bridge = self._make_bridge_with_tmp_memory(current_session="")
        bridge._memory.ensure_session("缓存改造")
        bridge._memory.add_message("缓存改造", "user", "hi")
        # 名字不含「缓存」，只有首条消息里提到
        bridge._memory.ensure_session("登录问题")
        bridge._memory.add_message("登录问题", "user", "顺便聊聊缓存策略")

        names = [s["name"] for s in bridge.search_sessions("缓存")["sessions"]]
        self.assertEqual(names[0], "缓存改造")
        self.assertEqual(set(names), {"缓存改造", "登录问题"})

    def test_archived_session_not_matched_by_body(self):
        """正文命中也不能把已归档会话捞回 @ 候选。"""
        bridge = self._make_bridge_with_tmp_memory(current_session="")
        bridge._memory.ensure_session("归档目标")
        bridge._memory.add_message("归档目标", "user", "这里提到了 quantum 方案")
        self.assertTrue(bridge._memory.archive_session("归档目标"))

        self.assertEqual(bridge.search_sessions("quantum")["sessions"], [])

    def test_search_text_backfilled_on_legacy_database(self):
        """存量库升级：新增 search_text 列后要用首条 user 消息自动回填。

        否则老会话升级后只有会话名可搜，用户会以为「换个对话还是搜不到」。
        """
        import shutil
        import tempfile

        from core.memory import MemoryManager

        tmp_dir = tempfile.mkdtemp()
        db_path = str(Path(tmp_dir) / "legacy.db")
        mem = MemoryManager(db_path)
        mem.ensure_session("老会话")
        mem.add_message("老会话", "user", "历史问题：数据库迁移失败")
        # 模拟升级前的 schema：没有 search_text 列，也没有回填数据
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
        """首条消息尾部的运行上下文块不能进入检索文本。

        否则「deepseek」「当前时间」这类只存在于系统注入块里的词，会把一大片
        无关会话都顶进 @ 候选，用户反而找不到真正想要的那个。
        """
        bridge = self._make_bridge_with_tmp_memory(current_session="")
        bridge._memory.ensure_session("上下文会话")
        bridge._memory.add_message(
            "上下文会话",
            "user",
            "帮我修一下启动报错\n\n【\u200b当前运行上下文】\n当前模型：deepseek",
        )

        # 正文关键词照常命中
        self.assertEqual(
            [s["name"] for s in bridge.search_sessions("启动报错")["sessions"]],
            ["上下文会话"],
        )
        # 注入块里的词不进入检索文本
        self.assertEqual(bridge.search_sessions("deepseek")["sessions"], [])


if __name__ == "__main__":
    unittest.main()
