import pytest
import sqlite3
import time
from core.memory import MemoryManager, SessionManager


def test_memory_manager_compatibility_name():
    assert SessionManager is MemoryManager

def test_init_db_creates_tables():
    """测试 SessionManager 初始化时是否正确创建了数据库表"""
    
    # 1. 准备与执行 (Arrange & Act)
    # 传入 ":memory:" 让数据库只活在内存中
    manager = SessionManager(":memory:")
    
    # 2. 断言验证 (Assert)
    # 获取数据库游标，去系统的 sqlite_master 表里查一下，我们的表到底建成功没有
    cursor = manager.conn.cursor()
    
    # 检查 sessions (会话表) 是否存在
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
    assert cursor.fetchone() is not None, "sessions 表没有被创建！"
    
    # 检查 messages (具体消息表) 是否存在
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
    assert cursor.fetchone() is not None, "messages 表没有被创建！"


def test_add_and_get_messages():
    """测试是否能向会话中添加消息，并正确读取历史记录"""
    
    # 1. 准备：在内存里建一个新管家
    manager = SessionManager(":memory:")
    session_name = "test_chat"
    
    # 2. 执行：让管家记下两句话（注意：这时候会报错，因为我们还没教管家怎么记）
    manager.add_message(session_name, "user", "你好，我是人类")
    manager.add_message(session_name, "assistant", "你好，我是 Y-code！")
    
    # 3. 执行：让管家把刚才那个会话的记录全翻出来
    history = manager.get_history(session_name)
    
    # 4. 断言验证：检查管家翻出来的记录对不对
    assert len(history) == 2, "历史记录应该有2条！"
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "你好，我是人类"
    assert history[1]["role"] == "assistant"
    assert history[1]["content"] == "你好，我是 Y-code！"


def test_structured_assistant_history_preserves_provider_envelope_for_llm(tmp_path):
    manager = MemoryManager(str(tmp_path / "memory.db"))
    manager.add_message("session", "assistant", {
        "role": "assistant",
        "content": "最终回答",
        "reasoning_content": "分析过程",
        "thinking_blocks": [
            {"type": "thinking", "thinking": "分析过程", "signature": "sig"},
        ],
    })

    history = manager.get_history_for_llm("session")

    assert history == [{
        "role": "assistant",
        "content": "最终回答",
        "reasoning_content": "分析过程",
        "thinking_blocks": [
            {"type": "thinking", "thinking": "分析过程", "signature": "sig"},
        ],
    }]


def test_add_messages_persists_one_ordered_batch(tmp_path):
    manager = MemoryManager(str(tmp_path / "batch.db"))

    manager.add_messages("session", [
        ("assistant", {"content": None, "tool_calls": [{"id": "one"}]}),
        ("tool", {"tool_call_id": "one", "content": "result"}),
        ("assistant", {"content": "summary"}),
    ])

    history = manager.get_history("session")
    assert [message["role"] for message in history] == [
        "assistant",
        "tool",
        "assistant",
    ]
    assert history[-1]["content"] == "summary"


def test_plain_scalar_text_is_not_misparsed_as_json(tmp_path):
    """用户消息恰为 123 / true 等合法 JSON 标量时，content 必须保持原始字符串类型"""
    manager = MemoryManager(str(tmp_path / "scalar.db"))
    manager.add_message("session", "user", "123")
    manager.add_message("session", "user", "true")

    history = manager.get_history("session")

    assert history[0]["content"] == "123"
    assert isinstance(history[0]["content"], str)
    assert history[1]["content"] == "true"
    assert isinstance(history[1]["content"], str)

    llm_history = manager.get_history_for_llm("session")
    assert llm_history[0]["content"] == "123"
    assert isinstance(llm_history[0]["content"], str)


def test_rename_session_suffix_uses_stripped_base_name(tmp_path):
    """重命名冲突时后缀基于 strip 后的基准名，不产生 'name _1' 带空格名"""
    manager = MemoryManager(str(tmp_path / "rename.db"))
    manager.add_message("chat", "user", "hello")
    manager.add_message("taken", "user", "hello")

    success, final_name = manager.rename_session("chat", "  taken  ")

    assert success is True
    assert final_name == "taken_1"
    assert " " not in final_name


def test_rename_session_rejects_empty_new_name(tmp_path):
    """空串/纯空白/None 新名字必须被拒绝，不得产生空 session_name"""
    manager = MemoryManager(str(tmp_path / "rename_empty.db"))
    manager.add_message("chat", "user", "hello")

    assert manager.rename_session("chat", "") == (False, "chat")
    assert manager.rename_session("chat", "   ") == (False, "chat")
    assert manager.rename_session("chat", None) == (False, "chat")
    # 原会话不受影响
    assert any(s["name"] == "chat" for s in manager.get_all_sessions())


def test_sessions_are_scoped_by_workdir(tmp_path):
    """会话按工作目录隔离：只返回指定目录的会话 + 历史遗留（无目录）会话"""
    manager = MemoryManager(str(tmp_path / "scoped.db"))
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()

    manager.set_workdir(dir_a)
    manager.add_message("session_a", "user", "hello")
    manager.set_workdir(dir_b)
    manager.add_message("session_b", "user", "hello")

    names_a = {s["name"] for s in manager.get_all_sessions(workdir=str(dir_a))}
    names_b = {s["name"] for s in manager.get_all_sessions(workdir=str(dir_b))}
    names_all = {s["name"] for s in manager.get_all_sessions()}

    assert names_a == {"session_a"}
    assert names_b == {"session_b"}
    assert names_all == {"session_a", "session_b"}


def test_legacy_sessions_without_workdir_visible_everywhere(tmp_path):
    """历史遗留的 NULL workdir 会话在任何目录下都可见（升级不丢历史）"""
    manager = MemoryManager(str(tmp_path / "legacy.db"))
    manager.set_workdir(tmp_path)
    manager.add_message("new_session", "user", "hello")
    # 手工插入一条无 workdir 的旧会话记录
    with manager.acquire_lock():
        manager.conn.execute(
            "INSERT INTO sessions (session_name) VALUES (?)", ("legacy_session",)
        )
        manager.conn.commit()

    # legacy_session 是无消息的空会话，测试目的是验证旧会话可见性，
    # 传 include_empty=True 跳过空会话过滤（参考 kimicode 的列表过滤策略）
    names = {s["name"] for s in manager.get_all_sessions(workdir=str(tmp_path), include_empty=True)}
    assert names == {"new_session", "legacy_session"}


def test_session_timestamps_recorded_and_updated(tmp_path):
    """新建会话记录 workdir/created_at/updated_at，再次写消息时 updated_at 刷新"""
    manager = MemoryManager(str(tmp_path / "timestamps.db"))
    manager.set_workdir(tmp_path)
    manager.add_message("chat", "user", "hello")

    session = next(s for s in manager.get_all_sessions() if s["name"] == "chat")
    assert session["workdir"] is not None
    assert session["created_at"] and len(session["created_at"]) == 19
    assert session["updated_at"] == session["created_at"]

    # 再次写入消息后 updated_at 字段仍存在且格式正确（秒级精度下同秒相等也合法）
    manager.add_message("chat", "assistant", "reply")
    session = next(s for s in manager.get_all_sessions() if s["name"] == "chat")
    assert session["updated_at"] >= session["created_at"]


def test_update_session_workdir_migrates_group(tmp_path):
    """会话进行中切换项目工作区后，会话应迁移到新目录分组（GUI 侧栏归组依据）"""
    manager = MemoryManager(str(tmp_path / "rebind.db"))
    project = tmp_path / "project"
    project.mkdir()

    manager.create_session_explicit("chat", workdir="GLOBAL", source="GUI")
    manager.add_message("chat", "user", "hello")

    assert manager.update_session_workdir("chat", str(project)) is True
    session = next(s for s in manager.get_all_sessions() if s["name"] == "chat")
    assert session["workdir"] != "GLOBAL"
    assert {s["name"] for s in manager.get_all_sessions(workdir=str(project))} == {"chat"}

    # 也支持迁回 GLOBAL 自由对话分组
    assert manager.update_session_workdir("chat", "GLOBAL") is True
    session = next(s for s in manager.get_all_sessions() if s["name"] == "chat")
    assert session["workdir"] == "GLOBAL"

    # 不存在的会话返回 False，且不会误建新记录
    assert manager.update_session_workdir("ghost", str(project)) is False
    assert not any(s["name"] == "ghost" for s in manager.get_all_sessions(include_empty=True))


def test_get_history_for_llm_drops_orphan_tool_messages(tmp_path):
    """压缩边界切在 assistant(tool_calls) 与其 tool 结果之间时，
    保留区开头的孤儿 tool 消息必须被丢弃，否则 API 报 'tool_call_id is not found'
    且整个会话无法继续使用（回归：会话 '条码打印_1' 坏历史）"""
    manager = MemoryManager(str(tmp_path / "orphan.db"))
    manager.add_messages("session", [
        ("assistant", {"content": None, "tool_calls": [{"id": "tool_A", "function": {"name": "f"}}]}),
        ("tool", {"tool_call_id": "tool_A", "name": "f", "content": "result"}),
        ("assistant", {"content": "后续回答"}),
        ("user", {"content": "下一个问题"}),
    ])
    # 模拟压缩边界切在 assistant(tool_calls) 上：其 tool 结果成为孤儿
    with manager.acquire_lock():
        cursor = manager.conn.cursor()
        cursor.execute("SELECT id FROM messages ORDER BY id ASC LIMIT 1")
        boundary = cursor.fetchone()[0]
        cursor.execute(
            "UPDATE sessions SET memory_summary = ?, summarized_until_msg_id = ? WHERE session_name = ?",
            ("历史摘要", boundary, "session"),
        )
        manager.conn.commit()

    history = manager.get_history_for_llm("session")

    # history[0] 是摘要注入；活跃区不得含孤儿 tool 消息
    assert [m["role"] for m in history] == ["user", "assistant", "user"]


def test_get_history_for_llm_strips_unanswered_tool_calls(tmp_path):
    """assistant 声明了 tool_calls 但缺少对应 tool 结果时，移除 tool_calls，
    避免 'tool_calls must be followed by tool messages' 报错锁死会话"""
    manager = MemoryManager(str(tmp_path / "unanswered.db"))
    manager.add_messages("session", [
        ("assistant", {"content": None, "tool_calls": [{"id": "tool_B", "function": {"name": "f"}}]}),
        ("user", {"content": "中断了"}),
    ])

    history = manager.get_history_for_llm("session")

    assert "tool_calls" not in history[0]
    assert history[0]["content"]  # 空内容补占位文本，保证消息仍有效


def test_get_history_for_llm_keeps_valid_tool_pairing(tmp_path):
    """正常 assistant(tool_calls) + tool 结果配对不受 sanitizer 影响"""
    manager = MemoryManager(str(tmp_path / "paired.db"))
    manager.add_messages("session", [
        ("assistant", {"content": None, "tool_calls": [{"id": "tool_C", "function": {"name": "f"}}]}),
        ("tool", {"tool_call_id": "tool_C", "name": "f", "content": "ok"}),
        ("assistant", {"content": "完成"}),
    ])

    history = manager.get_history_for_llm("session")

    assert [m["role"] for m in history] == ["assistant", "tool", "assistant"]
    assert history[0]["tool_calls"][0]["id"] == "tool_C"
def test_top_level_user_addition_runs_history_maintenance_at_ingestion(monkeypatch):
    from core.memory import MemoryManager

    manager = MemoryManager(":memory:")
    calls = []
    monkeypatch.setattr(
        manager, "prune_history_files", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    manager.add_message("ingestion-maintenance", "user", "顶层输入")
    manager.add_messages("ingestion-maintenance", [
        ("user", "内部通知"),
        ("assistant", "批量回复"),
    ])

    assert calls == [(('ingestion-maintenance',), {"max_turns": 16, "retain_recent": 4})]


def test_replace_latest_user_content_is_compare_and_swap():
    from core.memory import MemoryManager

    manager = MemoryManager(":memory:")
    session = "vision-canonical-cas"
    original = [
        {"type": "text", "text": "分析图片"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    manager.add_message(session, "user", original)

    assert manager.replace_latest_user_message_content(
        session, original, "分析图片\n\n[图片分析结果]\n界面截图"
    ) is True
    assert manager.get_history(session)[-1]["content"] == (
        "分析图片\n\n[图片分析结果]\n界面截图"
    )

    assert manager.replace_latest_user_message_content(
        session, original, "不应覆盖"
    ) is False
    assert manager.get_history(session)[-1]["content"] == (
        "分析图片\n\n[图片分析结果]\n界面截图"
    )


def test_evict_uses_cas_and_rejects_stale_boundary(tmp_path):
    """水位在读取后被他线程推进时，淘汰必须放弃，不得覆盖别人的进度。"""
    from core.memory import MemoryManager

    manager = MemoryManager(str(tmp_path / "cas.db"))
    manager.create_session_explicit("s_cas", workdir="GLOBAL", source="GUI")
    for i in range(6):
        manager.add_message("s_cas", "user", "问题 %d" % i)
        manager.add_message("s_cas", "assistant", "回答 %d" % i)
    assert manager.evict_oldest_turn_persistent("s_cas") is True

    sid = manager.conn.execute(
        "SELECT id FROM sessions WHERE session_name='s_cas'").fetchone()[0]
    live = manager.conn.execute(
        "SELECT summarized_until_msg_id FROM sessions WHERE id=?", (sid,)).fetchone()[0]

    # 模拟「evict 读到旧值之后、写回之前」被别人把水位推得很远：
    # 用 conn 间谍在 evict 取完消息行之后（UPDATE 执行之前）注入推进。
    # 注意注入必须放在 fetchall 之后——execute 会重置语句，提前注入会让
    # fetchall 拿到空结果、evict 在空行检查提前退出，竞态根本没被触发。
    class _CursorSpy:
        def __init__(self, real):
            self._real = real
            self._last_sql = ""
            self._pushed = False

        def execute(self, sql, params=()):
            self._last_sql = sql
            return self._real.execute(sql, params)

        def fetchone(self):
            return self._real.fetchone()

        def fetchall(self):
            rows = self._real.fetchall()
            if "FROM messages" in self._last_sql and not self._pushed:
                self._pushed = True
                self._real.execute(
                    "UPDATE sessions SET summarized_until_msg_id=? WHERE id=?",
                    (live + 100000, sid))
                self._real.connection.commit()
            return rows

        @property
        def rowcount(self):
            return self._real.rowcount

    class _ConnSpy:
        def __init__(self, real):
            self._real = real

        def cursor(self):
            return _CursorSpy(self._real.cursor())

        def commit(self):
            self._real.commit()

        def rollback(self):
            self._real.rollback()

        def execute(self, sql, params=()):
            return self._real.execute(sql, params)

    real_conn = manager.conn
    manager.conn = _ConnSpy(real_conn)
    try:
        assert manager.evict_oldest_turn_persistent("s_cas") is False
    finally:
        manager.conn = real_conn
    now = manager.conn.execute(
        "SELECT summarized_until_msg_id FROM sessions WHERE id=?", (sid,)).fetchone()[0]
    assert now == live + 100000          # 未被覆盖


def test_memory_and_db_turn_boundary_agree(tmp_path):
    """同一份历史：DB 滑窗淘汰与内存轮次裁剪的轮次边界口径必须一致。

    DB 侧 evict 一次淘汰一轮、内存侧一次裁到放得下为止，比较口径必须让
    两边都淘汰到尽头：被裁掉的条数与保留下来的消息都必须完全相同。
    """
    from core.memory import MemoryManager
    import core.agent_service as ag
    import core.llm as llm

    manager = MemoryManager(str(tmp_path / "agree.db"))
    manager.create_session_explicit("s_ag", workdir="GLOBAL", source="GUI")
    manager.add_message("s_ag", "assistant", "开场白")          # 故意让首条非 user
    for i in range(5):
        manager.add_message("s_ag", "user", "问题 %d" % i)
        manager.add_message("s_ag", "assistant", "回答 %d" % i)

    before = manager.get_history_for_llm("s_ag")
    assert manager.evict_oldest_turn_persistent("s_ag") is True
    while manager.evict_oldest_turn_persistent("s_ag"):
        pass
    db_dropped = len(before) - len(manager.get_history_for_llm("s_ag"))
    assert db_dropped >= 1

    mem = list(before)
    orig = llm.estimate_tokens
    llm.estimate_tokens = lambda model, msgs: len(msgs) * 100000
    try:
        pruned, _ = ag._prune_history_in_memory_to_fit_window(
            mem, 100, "test/model", {"role": "system", "content": "s"}, 10 ** 9)
    finally:
        llm.estimate_tokens = orig
    assert pruned >= 1
    assert len(before) - len(mem) == db_dropped      # 口径必须一致
    assert manager.get_history_for_llm("s_ag") == mem   # 保留的消息逐条相同


def _harness_events_index_first_columns(manager):
    """返回 harness_events 上所有索引的首列名集合。"""
    cursor = manager.conn.cursor()
    columns = set()
    rows = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='harness_events'"
    ).fetchall()
    for (index_name,) in rows:
        info = cursor.execute(f"PRAGMA index_info({index_name})").fetchall()
        if info:
            columns.add(info[0][2])
    return columns


def test_harness_events_foreign_key_columns_are_indexed(tmp_path):
    """删除会话慢的根因：harness_events 的 step_id / message_id 外键列缺索引。

    缺索引时 SQLite 为每条被删的父行全表扫描 harness_events（ON DELETE
    CASCADE / SET NULL），实测删除 388 条消息的长会话需要 2.1 秒，补索引后 13 毫秒。
    """
    manager = MemoryManager(str(tmp_path / "fk_index.db"))

    columns = _harness_events_index_first_columns(manager)

    assert "turn_id" in columns          # 已有 idx_harness_events_turn_sequence
    assert "step_id" in columns
    assert "message_id" in columns


def test_delete_session_stays_fast_with_many_harness_events(tmp_path):
    """长会话删除耗时不得随 harness_events 规模爆炸。

    外键索引缺失时这条链退化成 O(父行数 × harness_events 行数)：本用例规模
    （300 消息 / 300 step / 40000 事件）实测无索引 823 ms、补索引 127 ms，
    真实库上 388 条消息的长会话是 2153 ms → 13 ms。
    """
    manager = MemoryManager(str(tmp_path / "del_perf.db"))
    session_name = "长会话"
    manager.create_session_explicit(session_name, workdir="GLOBAL", source="GUI")
    cursor = manager.conn.cursor()
    session_id = cursor.execute(
        "SELECT id FROM sessions WHERE session_name = ?", (session_name,)
    ).fetchone()[0]

    # 造一条与真实数据同构的级联链：owner → turn → steps → events，外加 messages
    cursor.execute(
        "INSERT INTO harness_owners (owner_id, heartbeat_at_ms) VALUES (?, ?)",
        ("perf-owner", 0),
    )
    cursor.execute(
        "INSERT INTO harness_turns (turn_id, session_id, owner_id, status, started_at_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        ("perf-turn", session_id, "perf-owner", "completed", 0),
    )
    cursor.executemany(
        "INSERT INTO harness_steps (step_id, turn_id, ordinal, status, started_at_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        [(f"perf-step-{i}", "perf-turn", i, "completed", 0) for i in range(300)],
    )
    cursor.executemany(
        "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
        [(session_id, "user", f"长会话消息 {i}") for i in range(300)],
    )
    cursor.executemany(
        "INSERT INTO harness_events "
        "(turn_id, step_id, sequence, kind, created_at_ms, message_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("perf-turn", f"perf-step-{i % 300}", i, "model_request", 0, i % 300 + 1)
            for i in range(40000)
        ],
    )
    manager.conn.commit()

    started = time.perf_counter()
    manager.delete_session(session_name)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.35, f"删除长会话耗时 {elapsed * 1000:.0f} ms，疑似外键索引缺失"
