import pytest

from core.memory import MemoryManager, _content_has_media, _try_parse_json
from core.tool_result_maintenance import ELIDED_PREFIX, SNIPPED_PREFIX


@pytest.fixture
def manager_tmp():
    manager = MemoryManager(":memory:")
    yield manager
    manager.close()


def _fill_tool_session(manager, session, tool_texts):
    manager.add_message(session, "user", "开始")
    for idx, text in enumerate(tool_texts):
        # tool_call_id 必须全局唯一，否则 get_history_for_llm 的配对清洗会合并/丢弃消息
        manager.add_messages(session, [
            ("assistant", {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call-{idx}", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]}),
            ("tool", {"role": "tool", "tool_call_id": f"call-{idx}",
                      "name": "read_file", "content": text}),
        ])


def test_maintain_snips_old_tool_results_but_protects_recent_tail(manager_tmp):
    session = "maintain-snip"
    big = "\n".join(f"line-{i} " + "x" * 40 for i in range(300))
    _fill_tool_session(manager_tmp, session, [big] * 6)
    snipped, elided = manager_tmp.maintain_tool_results(session, tier=1, protection_tokens=200)
    assert snipped > 0
    assert elided == 0
    history = manager_tmp.get_history_for_llm(session)
    tool_texts = [m["content"] for m in history if m.get("role") == "tool"]
    assert any(t.startswith(SNIPPED_PREFIX) for t in tool_texts)
    assert any(t == big for t in tool_texts)
    snipped2, _ = manager_tmp.maintain_tool_results(session, tier=1, protection_tokens=200)
    assert snipped2 == 0


def test_maintain_tier2_upgrades_snipped_to_elided(manager_tmp):
    session = "maintain-elide"
    big = "\n".join(f"line-{i} " + "x" * 40 for i in range(300))
    _fill_tool_session(manager_tmp, session, [big] * 6)
    manager_tmp.maintain_tool_results(session, tier=1, protection_tokens=200)
    snipped, elided = manager_tmp.maintain_tool_results(session, tier=2, protection_tokens=200)
    assert elided > 0
    history = manager_tmp.get_history_for_llm(session)
    tool_texts = [m["content"] for m in history if m.get("role") == "tool"]
    assert any(t.startswith(ELIDED_PREFIX) for t in tool_texts)
    s3, e3 = manager_tmp.maintain_tool_results(session, tier=2, protection_tokens=200)
    assert s3 == 0 and e3 == 0


def test_maintain_never_touches_error_results(manager_tmp):
    session = "maintain-error"
    err = "🚨 命令执行失败（退出码 1）。\n" + "traceback\n" * 500
    _fill_tool_session(manager_tmp, session, [err] * 6)
    s, e = manager_tmp.maintain_tool_results(session, tier=2, protection_tokens=200)
    assert s == 0 and e == 0
    history = manager_tmp.get_history_for_llm(session)
    assert any(m.get("role") == "tool" and m["content"] == err for m in history)


def test_add_media_message_folds_previous_media_in_db(manager_tmp):
    session = "media-fold"
    img1 = [{"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]
    img2 = [{"type": "text", "text": "再看这张"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBB"}}]
    manager_tmp.add_message(session, "user", img1)
    manager_tmp.add_message(session, "assistant", "看到了")
    manager_tmp.add_message(session, "user", img2)
    history = manager_tmp.get_history_for_llm(session)
    users = [m for m in history if m.get("role") == "user"]
    first_content = users[0]["content"]
    assert all(b.get("type") == "text" for b in first_content)
    assert any("已折叠" in b.get("text", "") for b in first_content)
    last_content = users[-1]["content"]
    assert any(b.get("type") == "image_url" for b in last_content)
    manager_tmp.add_message(session, "assistant", "明白")
    history2 = manager_tmp.get_history_for_llm(session)
    users2 = [m for m in history2 if m.get("role") == "user"]
    assert users2[0]["content"] == first_content


def test_add_media_message_single_media_stays_untouched(manager_tmp):
    session = "media-single"
    img = [{"type": "text", "text": "唯一的图"},
           {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]
    manager_tmp.add_message(session, "user", img)
    manager_tmp.add_message(session, "assistant", "收到")
    manager_tmp.add_message(session, "user", "纯文本追问")
    history = manager_tmp.get_history_for_llm(session)
    users = [m for m in history if m.get("role") == "user"]
    assert any(b.get("type") == "image_url" for b in users[0]["content"])


def _observation_message(ordinal, *, run_id="run-a", target="browser"):
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "【电脑观察】请只依据这张最新截图决定下一步。"},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,Q09NUFVURVI7e3VyZGluYWx9fQ=={ordinal}",
            }},
        ],
        "_computer_v1": {"run_id": run_id, "ordinal": ordinal, "target": target},
    }


@pytest.fixture
def store():
    manager = MemoryManager(":memory:")
    yield manager
    manager.close()


@pytest.fixture
def session(store):
    name = "computer-media-fold"
    store.add_message(name, "user", "开始电脑操控")
    return name


def _session_user_rows(store, session_name):
    with store._lock:
        rows = store.conn.execute(
            "SELECT id, content FROM messages "
            "WHERE session_id = (SELECT id FROM sessions WHERE session_name = ?) "
            "AND role = 'user' ORDER BY id ASC",
            (session_name,),
        ).fetchall()
    return rows


def add_observation_message(store, session_name, ordinal):
    store.add_message(session_name, "user", _observation_message(ordinal))
    rows = _session_user_rows(store, session_name)
    return rows[-1][0]


def raw_content_of(store, session_name, message_id):
    with store._lock:
        row = store.conn.execute(
            "SELECT content FROM messages WHERE id = ?", (message_id,),
        ).fetchone()
    assert row is not None
    return row[0]


def count_unfolded_media_rows(store, session_name):
    count = 0
    for _msg_id, raw in _session_user_rows(store, session_name):
        parsed, ok = _try_parse_json(raw)
        if ok and _content_has_media(parsed):
            count += 1
    return count


def test_content_has_media_accepts_full_message_dict_with_computer_meta():
    message = {
        "role": "user",
        "content": [{"type": "text", "text": "观察"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}],
        "_computer_v1": {"run_id": "run-a", "ordinal": 3, "target": "browser"},
    }
    assert _content_has_media(message) is True


def test_content_has_media_still_accepts_plain_list_and_rejects_text():
    assert _content_has_media([
        {"type": "text", "text": "纯文本"},
    ]) is False
    assert _content_has_media("纯文本消息") is False
    assert _content_has_media({"role": "user", "content": "纯文本"}) is False


def test_fold_keeps_computer_meta_and_is_idempotent(store, session):
    ids = [add_observation_message(store, session, ordinal) for ordinal in range(1, 4)]
    add_observation_message(store, session, 4)
    folded_first = raw_content_of(store, session, ids[0])
    assert '"_computer_v1"' in folded_first
    assert "data:image/png;base64" not in folded_first
    add_observation_message(store, session, 5)
    assert raw_content_of(store, session, ids[0]) == folded_first


def test_old_computer_screenshots_do_not_accumulate(store, session):
    for ordinal in range(1, 20):
        add_observation_message(store, session, ordinal)
    assert count_unfolded_media_rows(store, session) <= 1


def test_fold_handles_mixed_normal_and_computer_media_rows(store, session):
    normal_img = [
        {"type": "text", "text": "看这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
    ]
    store.add_message(session, "user", normal_img)
    add_observation_message(store, session, 1)
    add_observation_message(store, session, 2)
    assert count_unfolded_media_rows(store, session) == 1
    rows = _session_user_rows(store, session)
    normal_row = rows[1]
    parsed, ok = _try_parse_json(normal_row[1])
    assert ok and isinstance(parsed, list)
    assert all(block.get("type") == "text" for block in parsed)
    assert "data:image/png;base64" not in normal_row[1]
