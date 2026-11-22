import threading

import gui_bridge
from core.memory import MemoryManager
from gui_bridge import ApiBridge


def _bare_bridge():
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
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        mem.add_message(session, role, f"msg-{i}")
    return mem.get_history_with_ids(session)


def test_history_page_size_constant():
    assert gui_bridge.HISTORY_PAGE_SIZE == 50


def test_first_page_returns_triple_with_oldest_id():
    bridge = _bare_bridge()
    rows = _seed(bridge._memory, "s", 120)

    result = bridge._load_session_messages("s")

    assert isinstance(result, tuple) and len(result) == 3
    msgs, has_more, oldest_id = result
    assert has_more is True
    assert 1 <= len(msgs) <= gui_bridge.HISTORY_PAGE_SIZE + 30
    assert len(msgs) == gui_bridge.HISTORY_PAGE_SIZE
    page_rows, _ = bridge._memory.get_history_paged("s")
    assert oldest_id == page_rows[0]["_msg_id"] == rows[70]["_msg_id"]
    assert msgs[0]["content"] == "msg-70"
    assert msgs[-1]["content"] == "msg-119"
    assert all(m["role"] in ("user", "assistant", "tool") for m in msgs)


def test_last_error_only_appended_on_first_page():
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
    assert len(res["messages"]) == 10
    assert res["messages"][-1]["role"] == "assistant"


def test_load_older_messages_walks_pages_until_end():
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
    page_rows, _ = mem.get_history_paged("s")
    assert oldest1 == page_rows[0]["_msg_id"]

    res2 = bridge.load_older_messages("s", oldest1)
    assert res2["ok"] is True
    assert res2["has_more"] is False

    expected = [r["content"] for r in rows if r["role"] in ("user", "assistant", "tool")]
    got = [m["content"] for m in res2["messages"]] + [m["content"] for m in msgs1]
    assert got == expected


def _switch_ready_bridge():
    bridge = _bare_bridge()
    bridge._set_current_session = lambda name: setattr(bridge, "_current_session", name)
    bridge._build_skill_list = lambda: []
    bridge.get_sessions = lambda: []
    bridge._effective_permission_mode = lambda session: "confirm"
    bridge._message_queue_snapshot = lambda session: []
    bridge.get_active_turn_snapshot = lambda session: None
    bridge._emit_token_usage = lambda: None
    bridge.emit_event = lambda kind, data: None
    return bridge


def test_switch_session_response_has_history_pagination_fields(monkeypatch):
    bridge = _switch_ready_bridge()
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *a, **k: {})
    bridge._async_recalc_token_usage = lambda session: None
    bridge._recalc_token_usage = lambda session: None
    _seed(bridge._memory, "s", 120)

    res = bridge.switch_session("s")

    assert res["ok"] is True
    assert res["history_has_more"] is True
    page_rows, _ = bridge._memory.get_history_paged("s")
    assert res["history_oldest_id"] == page_rows[0]["_msg_id"]
    assert 1 <= len(res["messages"]) <= 80


def test_switch_session_recalcs_token_usage_async(monkeypatch):
    bridge = _switch_ready_bridge()
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *a, **k: {})
    _seed(bridge._memory, "s", 3)
    calls = {"async": [], "sync": []}
    bridge._async_recalc_token_usage = lambda session: calls["async"].append(session)
    bridge._recalc_token_usage = lambda session: calls["sync"].append(session)

    res = bridge.switch_session("s")

    assert res["ok"] is True
    assert calls["async"] == ["s"]
    assert calls["sync"] == []


def test_switch_session_response_omits_skills_and_skips_scan(monkeypatch):
    bridge = _switch_ready_bridge()
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *a, **k: {})
    bridge._async_recalc_token_usage = lambda session: None
    calls = []
    bridge._build_skill_list = lambda: (calls.append(1), [])[1]
    _seed(bridge._memory, "s", 3)

    res = bridge.switch_session("s")

    assert res["ok"] is True
    assert "skills" not in res
    assert calls == []


def test_observation_followup_messages_hidden_from_bubbles():
    import json

    from core.computer.provider_generic import _observation_followup_message

    class _Obs:
        target = "browser"
        elements = [
            {"ref": "el-1", "kind": "link", "name": "主页",
             "bounds": (10, 20, 30, 40), "enabled": True},
        ]
        png_path = ""

    bridge = _bare_bridge()
    bridge._memory.add_message("s", "user", "帮我打开浏览器看看")
    followup = _observation_followup_message(_Obs(), run_id="run-1", ordinal=1)
    bridge._memory.add_message("s", "user", json.dumps(followup, ensure_ascii=False))
    bridge._memory.add_message("s", "assistant", "done")

    msgs, _, _ = bridge._load_session_messages("s")

    user_contents = [m["content"] for m in msgs if m["role"] == "user"]
    assert user_contents == ["帮我打开浏览器看看"]

    # 展示过滤绝不影响模型上下文：跟进消息仍在 get_history 里
    history_roles = [m["role"] for m in bridge._memory.get_history("s")]
    assert history_roles.count("user") == 2


def test_load_older_messages_reports_failure_instead_of_fake_end(monkeypatch):
    import sqlite3

    bridge = _bare_bridge()
    _seed(bridge._memory, "s", 120)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(bridge._memory, "get_history_paged", boom)

    res = bridge.load_older_messages("s", 100)
    assert res["ok"] is False
    assert res["messages"] == []
    assert res["oldest_id"] is None
    assert "locked" in res["msg"]

    msgs, has_more, oldest_id = bridge._load_session_messages("s")
    assert msgs == []
    assert has_more is False
    assert oldest_id is None
