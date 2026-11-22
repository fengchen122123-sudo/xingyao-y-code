import pytest
from core.memory import MemoryManager


@pytest.fixture
def mgr():
    return MemoryManager(":memory:")


def _seed(m, session, n):
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        m.add_message(session, role, f"msg-{i}")
    return [msg["_msg_id"] for msg in m.get_history_with_ids(session)]


def test_latest_page_returns_most_recent_in_ascending_order(mgr):
    ids = _seed(mgr, "s", 120)

    messages, has_more = mgr.get_history_paged("s", limit=50)

    assert has_more is True
    assert len(messages) == 50
    assert [m["_msg_id"] for m in messages] == ids[-50:]
    msg_ids = [m["_msg_id"] for m in messages]
    assert msg_ids == sorted(msg_ids)
    assert messages[0]["content"] == "msg-70"
    assert messages[-1]["content"] == "msg-119"


def test_before_id_walks_back_older(mgr):
    ids = _seed(mgr, "s", 120)
    first_page, first_has_more = mgr.get_history_paged("s", limit=50)
    assert first_has_more is True

    oldest = first_page[0]["_msg_id"]
    second_page, second_has_more = mgr.get_history_paged("s", before_id=oldest, limit=50)

    assert second_has_more is True
    assert len(second_page) == 50
    first_ids = {m["_msg_id"] for m in first_page}
    second_ids = {m["_msg_id"] for m in second_page}
    assert first_ids.isdisjoint(second_ids)
    assert second_page[-1]["_msg_id"] < oldest
    assert [m["_msg_id"] for m in second_page] == ids[20:70]


def test_short_history_has_more_false(mgr):
    ids = _seed(mgr, "s", 10)

    messages, has_more = mgr.get_history_paged("s", limit=50)

    assert has_more is False
    assert len(messages) == 10
    assert [m["_msg_id"] for m in messages] == ids


def test_exact_page_boundary(mgr):
    ids = _seed(mgr, "s", 50)

    messages, has_more = mgr.get_history_paged("s", limit=50)

    assert len(messages) == 50
    assert messages[0]["role"] == "user"
    assert has_more is False
    assert [m["_msg_id"] for m in messages] == ids


def test_unknown_session_returns_empty(mgr):
    messages, has_more = mgr.get_history_paged("ghost", limit=50)

    assert messages == []
    assert has_more is False


def test_empty_session_returns_empty(mgr):
    mgr.create_session_explicit("empty", workdir="GLOBAL", source="GUI")

    messages, has_more = mgr.get_history_paged("empty", limit=50)

    assert messages == []
    assert has_more is False


def test_turn_alignment_extends_to_user_boundary(mgr):
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

    assert messages[0]["role"] == "user"
    assert len(messages) > limit
    assert len(messages) <= limit + 30
    assert has_more is True


def test_get_history_with_ids_unchanged(mgr):
    ids = _seed(mgr, "s", 120)
    before = mgr.get_history_with_ids("s")

    mgr.get_history_paged("s", limit=50)

    after = mgr.get_history_with_ids("s")
    assert after == before
    assert len(after) == 120
    assert [m["_msg_id"] for m in after] == ids


def test_alignment_extension_exhausted_no_user(mgr):
    for i in range(60):
        mgr.add_message("spam", "assistant", f"a-{i}")

    limit = 10
    messages, has_more = mgr.get_history_paged("spam", limit=limit)

    assert len(messages) <= limit + 30
    assert len(messages) == limit + 30
    assert all(m["role"] == "assistant" for m in messages)
    assert has_more is True
