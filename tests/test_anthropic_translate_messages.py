import pytest

from core.llm_native_transport import _anthropic_translate_messages


def _user(content):
    return {"role": "user", "content": content}


def _assistant(content=None, tool_calls=None):
    return {"role": "assistant", "content": content, "tool_calls": tool_calls or []}


def _tool(call_id, content="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _tool_call(call_id, name="read_file"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": '{"path": "a.txt"}'},
    }


def _assert_roles_alternate(messages):
    roles = [m["role"] for m in messages]
    for prev_role, next_role in zip(roles, roles[1:]):
        assert prev_role != next_role, f"连续 {prev_role} 消息: {roles}"


def test_tool_results_followed_by_text_user_merge_into_one_message():
    messages = [
        _user("帮我改一下这个文件"),
        _assistant(tool_calls=[_tool_call("call-1"), _tool_call("call-2")]),
        _tool("call-1", "第一段结果"),
        _tool("call-2", "第二段结果"),
        _user("本回合已连续执行多步操作，仍未建立任务计划。"),
    ]

    _, anth_messages = _anthropic_translate_messages(messages)

    _assert_roles_alternate(anth_messages)
    assert [m["role"] for m in anth_messages] == ["user", "assistant", "user"]
    merged = anth_messages[-1]["content"]
    assert [b["type"] for b in merged] == ["tool_result", "tool_result", "text"]
    assert merged[0]["tool_use_id"] == "call-1"
    assert merged[2]["text"] == "本回合已连续执行多步操作，仍未建立任务计划。"


def test_consecutive_text_users_merge_into_one_message():
    messages = [
        _user("第一条"),
        _user("第二条"),
    ]

    _, anth_messages = _anthropic_translate_messages(messages)

    _assert_roles_alternate(anth_messages)
    assert len(anth_messages) == 1
    assert [b["text"] for b in anth_messages[0]["content"]] == ["第一条", "第二条"]


def test_alternating_history_stays_untouched():
    messages = [
        _user("问题"),
        _assistant("回答"),
        _user("追问"),
    ]

    _, anth_messages = _anthropic_translate_messages(messages)

    assert [m["role"] for m in anth_messages] == ["user", "assistant", "user"]
    assert anth_messages[0]["content"] == [{"type": "text", "text": "问题"}]
    assert anth_messages[2]["content"] == [{"type": "text", "text": "追问"}]


def test_consecutive_tool_results_still_merge_into_single_user_message():
    messages = [
        _user("问题"),
        _assistant(tool_calls=[_tool_call("call-1"), _tool_call("call-2")]),
        _tool("call-1"),
        _tool("call-2"),
    ]

    _, anth_messages = _anthropic_translate_messages(messages)

    _assert_roles_alternate(anth_messages)
    assert [m["role"] for m in anth_messages] == ["user", "assistant", "user"]
    assert [b["type"] for b in anth_messages[-1]["content"]] == [
        "tool_result",
        "tool_result",
    ]


def test_cache_control_stays_on_last_block_of_merged_segment():
    messages = [
        _assistant(tool_calls=[_tool_call("call-1")]),
        _tool("call-1"),
        {"role": "user", "content": "注入提醒", "cache_control": {"type": "ephemeral"}},
    ]

    _, anth_messages = _anthropic_translate_messages(messages)

    _assert_roles_alternate(anth_messages)
    merged = anth_messages[-1]["content"]
    assert [b["type"] for b in merged] == ["tool_result", "text"]
    assert "cache_control" not in merged[0]
    assert merged[-1]["cache_control"] == {"type": "ephemeral"}


def test_three_consecutive_users_merge_in_order():
    messages = [
        _user("第一条"),
        _user("第二条"),
        _user("第三条"),
    ]

    _, anth_messages = _anthropic_translate_messages(messages)

    _assert_roles_alternate(anth_messages)
    assert len(anth_messages) == 1
    assert [b["text"] for b in anth_messages[0]["content"]] == [
        "第一条",
        "第二条",
        "第三条",
    ]


def test_empty_message_list_returns_empty():
    system, anth_messages = _anthropic_translate_messages([])

    assert system is None
    assert anth_messages == []


def test_cache_control_on_earlier_block_survives_later_merge():
    messages = [
        {"role": "user", "content": "旧段", "cache_control": {"type": "ephemeral"}},
        _user("新段"),
    ]

    _, anth_messages = _anthropic_translate_messages(messages)

    _assert_roles_alternate(anth_messages)
    merged = anth_messages[-1]["content"]
    assert [b["text"] for b in merged] == ["旧段", "新段"]
    assert merged[0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in merged[1]


def test_tool_message_cache_control_is_not_forwarded():
    messages = [
        _user("问题"),
        _assistant(tool_calls=[_tool_call("call-1")]),
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "结果",
            "cache_control": {"type": "ephemeral"},
        },
    ]

    _, anth_messages = _anthropic_translate_messages(messages)

    _assert_roles_alternate(anth_messages)
    tool_block = anth_messages[-1]["content"][0]
    assert tool_block["type"] == "tool_result"
    assert tool_block["tool_use_id"] == "call-1"
    assert "cache_control" not in tool_block


if __name__ == "__main__":
    pytest.main([__file__])
