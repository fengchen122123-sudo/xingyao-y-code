# tests/test_gui_bridge_history_paged.py — 桥接层历史分页 + load_older_messages（TDD，先行失败）
import threading

import gui_bridge
from core.memory import MemoryManager
from gui_bridge import ApiBridge


def _bare_bridge():
    """不经 __init__ 构造裸实例，挂一个内存版 MemoryManager（参考 test_gui_workspace_groups）。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = MemoryManager(":memory:")
    bridge._current_session = "s"
    bridge._turn_start_lock = threading.RLock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._session_last_errors = {}
    bridge._session_last_errors_lock = threading.Lock()
    return bridge


def _seed(mem, session, n):
    """依次写入 n 条交替 user/assistant 消息，返回全部行（含 _msg_id，正序）。"""
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        mem.add_message(session, role, f"msg-{i}")
    return mem.get_history_with_ids(session)


def test_history_page_size_constant():
    """页大小常量定义在 gui_bridge，固定 50。"""
    assert gui_bridge.HISTORY_PAGE_SIZE == 50


def test_first_page_returns_triple_with_oldest_id():
    """首页（before_id=None）：三元组结构、≤ limit+30 条、oldest_id 为本页最小 _msg_id。"""
    bridge = _bare_bridge()
    rows = _seed(bridge._memory, "s", 120)

    result = bridge._load_session_messages("s")

    assert isinstance(result, tuple) and len(result) == 3
    msgs, has_more, oldest_id = result
    assert has_more is True
    assert 1 <= len(msgs) <= gui_bridge.HISTORY_PAGE_SIZE + 30
    # 交替 user/assistant：最旧一条是 user，不触发回合对齐扩展，恰好一页
    assert len(msgs) == gui_bridge.HISTORY_PAGE_SIZE
    # oldest_id 取未过滤的本页最旧消息 _msg_id（与 memory 层分页结果首行一致）
    page_rows, _ = bridge._memory.get_history_paged("s")
    assert oldest_id == page_rows[0]["_msg_id"] == rows[70]["_msg_id"]
    # 正序、内容为最新 50 条、只含可显示角色
    assert msgs[0]["content"] == "msg-70"
    assert msgs[-1]["content"] == "msg-119"
    assert all(m["role"] in ("user", "assistant", "tool") for m in msgs)


def test_last_error_only_appended_on_first_page():
    """last_error 只附加在首页；翻页响应不得再携带 error_note / 错误气泡。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "s", 60)
    bridge._session_last_errors["s"] = {
        "message": "litellm.APIError: 余额不足",
        "model_name": "deepseek-chat",
        "model_id": "deepseek-v3",
    }

    msgs, has_more, oldest_id = bridge._load_session_messages("s")
    assert has_more is True
    assert msgs[-1]["role"] == "assistant"
    assert "余额不足" in msgs[-1]["error_note"]
    assert msgs[-1]["execution_state"] == "error"

    res = bridge.load_older_messages("s", oldest_id)
    assert res["ok"] is True
    assert res["messages"], "第二页应有消息"
    assert all(not m.get("error_note") for m in res["messages"])
    assert all(m.get("execution_state") != "error" for m in res["messages"])
    # 末尾不得被追加错误气泡：第二页恰为 10 条交替 user/assistant
    assert len(res["messages"]) == 10
    assert res["messages"][-1]["role"] == "assistant"


def test_load_older_messages_walks_pages_until_end():
    """正常翻页：逐页向更早走，直到 has_more=False；页间不重叠，拼全全部历史。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "s", 120)

    msgs1, has_more1, oldest1 = bridge._load_session_messages("s")
    assert has_more1 is True

    res2 = bridge.load_older_messages("s", oldest1)
    assert res2["ok"] is True
    assert res2["has_more"] is True
    assert res2["oldest_id"] is not None and res2["oldest_id"] < oldest1
    assert len(res2["messages"]) == 50

    res3 = bridge.load_older_messages("s", res2["oldest_id"])
    assert res3["ok"] is True
    assert res3["has_more"] is False
    assert res3["oldest_id"] is not None
    assert len(res3["messages"]) == 20

    contents = ([m["content"] for m in res3["messages"]]
                + [m["content"] for m in res2["messages"]]
                + [m["content"] for m in msgs1])
    assert contents == [f"msg-{i}" for i in range(120)]


def test_load_older_messages_defensive_branches():
    """before_id=0 / 负数 / 非数字 / 会话不存在 四种防御分支。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "s", 10)

    for bad in (0, -5):
        res = bridge.load_older_messages("s", bad)
        assert res == {"ok": True, "messages": [], "has_more": False, "oldest_id": None}

    for bad in ("abc", None, ""):
        res = bridge.load_older_messages("s", bad)
        assert res == {"ok": False, "messages": [], "has_more": False, "oldest_id": None}

    res = bridge.load_older_messages("ghost", 10)
    assert res == {"ok": False, "messages": [], "has_more": False, "oldest_id": None}


def test_cursor_no_holes_with_system_messages():
    """游标不漏消息：含 system 消息的会话，两页拼接后 id 连续无洞、无重复。

    roles 过滤会丢 system 消息，游标必须以未过滤的 DB 行 id 为准。
    """
    bridge = _bare_bridge()
    mem = bridge._memory
    for i in range(60):
        if i % 6 == 5:
            mem.add_message("s", "system", f"sys-{i}")
        else:
            role = "user" if i % 2 == 0 else "assistant"
            mem.add_message("s", role, f"msg-{i}")
    rows = mem.get_history_with_ids("s")

    msgs1, has_more1, oldest1 = bridge._load_session_messages("s")
    assert has_more1 is True
    # oldest_id 与未过滤分页查询的首行 _msg_id 一致（不是过滤后首条显示消息的 id）
    page_rows, _ = mem.get_history_paged("s")
    assert oldest1 == page_rows[0]["_msg_id"]

    res2 = bridge.load_older_messages("s", oldest1)
    assert res2["ok"] is True
    assert res2["has_more"] is False

    # 两页拼接 == 全部可显示消息（顺序一致、无洞、无重复）
    expected = [r["content"] for r in rows if r["role"] in ("user", "assistant", "tool")]
    got = [m["content"] for m in res2["messages"]] + [m["content"] for m in msgs1]
    assert got == expected
