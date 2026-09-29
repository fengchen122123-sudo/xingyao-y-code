"""_anthropic_translate_messages 相邻 user 消息合并回归。

背景：OpenAI 序列 assistant(tool_calls) → tool(...) → user(注入文本)
（编排提醒、follow-up 观察消息）翻译后会产生 user(tool_result 块) 与
user(text 块) 相邻。Anthropic 严格要求 user/assistant 交替且不自动合并，
连续 user 直接 400 "roles must alternate between user and assistant"。
翻译层必须把相邻 user 的内容块并入同一条消息（tool_result 在前、文本在后）。
"""

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
    """3 条及以上连续 user：全部并入同一条，块顺序保持原始顺序。"""
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
    """空输入不炸：system 为 None、消息列表为空。"""
    system, anth_messages = _anthropic_translate_messages([])

    assert system is None
    assert anth_messages == []


def test_cache_control_on_earlier_block_survives_later_merge():
    """记录性断言：前一条 user 段的 cache_control 留在原块上，不随并入搬到末块。

    当前实现是把后续 user 的块 `extend` 到前一条 user 的块列表尾部，原先挂在
    末块的 `cache_control` 于是变成中间块。Anthropic 语义上 cache_control 只应
    出现在最后一块——这里不做搬运是有意取舍（该断点本就是指"这一段的末尾"，
    语义仍成立），本用例固化现状，防止无意改动。
    """
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
    """记录性断言：tool 消息自带的 cache_control 未被转发到 tool_result 块。

    翻译层的 tool 分支只产出 `{type, tool_use_id, content}` 三个字段，不读取
    tool 消息上的 `cache_control`。这属于既有行为（缓存断点由 assistant/user
    侧控制），本用例固化现状，防止无意改动。
    """
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
