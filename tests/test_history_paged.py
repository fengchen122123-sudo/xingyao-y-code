# tests/test_history_paged.py — get_history_paged 分页查询（TDD，先行失败）
import pytest
from core.memory import MemoryManager


@pytest.fixture
def mgr():
    # 内存库，不污染真实数据
    return MemoryManager(":memory:")


def _seed(m, session, n):
    """建会话 + 依次写入 n 条交替 user/assistant 消息，返回消息 id 列表（正序）"""
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        m.add_message(session, role, f"msg-{i}")
    return [msg["_msg_id"] for msg in m.get_history_with_ids(session)]


def test_latest_page_returns_most_recent_in_ascending_order(mgr):
    """120 条 → 首页 50 条、has_more=True、内容=最新 50 条、id 递增"""
    ids = _seed(mgr, "s", 120)

    messages, has_more = mgr.get_history_paged("s", limit=50)

    assert has_more is True
    assert len(messages) == 50
    assert [m["_msg_id"] for m in messages] == ids[-50:]
    # 正序（id 递增）
    msg_ids = [m["_msg_id"] for m in messages]
    assert msg_ids == sorted(msg_ids)
    assert messages[0]["content"] == "msg-70"
    assert messages[-1]["content"] == "msg-119"


def test_before_id_walks_back_older(mgr):
    """before_id=首页最旧 _msg_id → 拿到更早一页，且不与上一页重叠"""
    ids = _seed(mgr, "s", 120)
    first_page, first_has_more = mgr.get_history_paged("s", limit=50)
    assert first_has_more is True

    oldest = first_page[0]["_msg_id"]
    second_page, second_has_more = mgr.get_history_paged("s", before_id=oldest, limit=50)

    assert second_has_more is True
    assert len(second_page) == 50
    # 不与上一页重叠
    first_ids = {m["_msg_id"] for m in first_page}
    second_ids = {m["_msg_id"] for m in second_page}
    assert first_ids.isdisjoint(second_ids)
    # 确实更早：本页 newest < 上页 oldest；最旧一条是 msg-20（user，不触发扩展）
    assert second_page[-1]["_msg_id"] < oldest
    assert [m["_msg_id"] for m in second_page] == ids[20:70]


def test_short_history_has_more_false(mgr):
    """10 条 → 一页拿全、has_more=False"""
    ids = _seed(mgr, "s", 10)

    messages, has_more = mgr.get_history_paged("s", limit=50)

    assert has_more is False
    assert len(messages) == 10
    assert [m["_msg_id"] for m in messages] == ids


def test_exact_page_boundary(mgr):
    """恰好 50 条且最旧是 user → 一页拿全、has_more=False"""
    ids = _seed(mgr, "s", 50)

    messages, has_more = mgr.get_history_paged("s", limit=50)

    assert len(messages) == 50
    assert messages[0]["role"] == "user"
    assert has_more is False
    assert [m["_msg_id"] for m in messages] == ids


def test_unknown_session_returns_empty(mgr):
    """会话不存在 → ([], False)"""
    messages, has_more = mgr.get_history_paged("ghost", limit=50)

    assert messages == []
    assert has_more is False


def test_empty_session_returns_empty(mgr):
    """会话存在但无消息 → ([], False)"""
    mgr.create_session_explicit("empty", workdir="GLOBAL", source="GUI")

    messages, has_more = mgr.get_history_paged("empty", limit=50)

    assert messages == []
    assert has_more is False


def test_turn_alignment_extends_to_user_boundary(mgr):
    """回合对齐扩展：limit 卡在回合中间的 tool 上时，向前吞并至最近 user。

    每回合 5 条：user, assistant, tool, tool, assistant；11 回合共 55 条。
    limit=8 时本页最旧一条落在 index 47（tool），扩展吞并 assistant(46)、user(45)。
    """
    session = "turns"
    for t in range(11):
        mgr.add_messages(session, [
            ("user", f"q{t}"),
            ("assistant", {"content": None, "tool_calls": [{"id": f"a{t}"}]}),
            ("tool", {"tool_call_id": f"a{t}", "content": "r1"}),
            ("tool", {"tool_call_id": f"a{t}", "content": "r2"}),
            ("assistant", f"answer{t}"),
        ])

    limit = 8
    messages, has_more = mgr.get_history_paged(session, limit=limit)

    # 扩展生效：返回页的最旧一条必须是 user，且确实比 limit 多
    assert messages[0]["role"] == "user"
    assert len(messages) > limit
    # 扩展上限 limit+30
    assert len(messages) <= limit + 30
    assert has_more is True


def test_get_history_with_ids_unchanged(mgr):
    """旧接口仍全量返回，不受新方法影响"""
    ids = _seed(mgr, "s", 120)
    before = mgr.get_history_with_ids("s")

    mgr.get_history_paged("s", limit=50)

    after = mgr.get_history_with_ids("s")
    assert after == before
    assert len(after) == 120
    assert [m["_msg_id"] for m in after] == ids


def test_alignment_extension_exhausted_no_user(mgr):
    """连续 assistant（无 user）：扩展余量耗尽后按上限截断返回，不死循环。

    60 条纯 assistant，limit=10：本页最旧一条始终非 user，
    吞并 30 条扩展余量后耗尽停止，返回条数恰好 = limit+30。
    """
    for i in range(60):
        mgr.add_message("spam", "assistant", f"a-{i}")

    limit = 10
    messages, has_more = mgr.get_history_paged("spam", limit=limit)

    assert len(messages) <= limit + 30
    assert len(messages) == limit + 30      # 余量全部耗尽
    assert all(m["role"] == "assistant" for m in messages)
    assert has_more is True                 # 仍有更早的 20 条
