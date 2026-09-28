"""_sanitize_tool_call_pairing 对不可回放 tool call 的净化回归。

背景：Agnes AI 等严格网关对请求历史中 assistant.tool_calls 的
name/arguments 做校验，空函数名或非法 JSON arguments 直接 400 整个请求，
坏记录落库后该会话每轮复现。加载层净化后旧会话自愈（DB 原文不动）。
"""

from core.memory import _sanitize_tool_call_pairing


def _assistant(tool_calls, content=None):
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def _tool(call_id, name, content="ok"):
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


def test_broken_empty_name_call_and_its_orphan_result_are_dropped():
    history = [
        {"role": "user", "content": "hi"},
        _assistant([
            {"id": "call-bad", "type": "function",
             "function": {"name": "", "arguments": ""}},
            {"id": "call-ok", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}},
        ]),
        _tool("call-bad", "", "工具参数 JSON 解析失败"),
        _tool("call-ok", "read_file"),
    ]

    sanitized = _sanitize_tool_call_pairing(history)

    assistant = sanitized[1]
    assert [tc["id"] for tc in assistant["tool_calls"]] == ["call-ok"]
    tool_rows = [m for m in sanitized if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_rows] == ["call-ok"]


def test_truncated_arguments_call_is_dropped_for_wire_safety():
    """截断 JSON 的原始字节即使本地可修复修复也非 strict JSON，严格网关
    会拒绝整请求——加载层按不可回放剔除。"""
    history = [
        _assistant([
            {"id": "call-1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "a.txt'}},
        ]),
        _tool("call-1", "read_file"),
    ]

    sanitized = _sanitize_tool_call_pairing(history)

    assert "tool_calls" not in sanitized[0]
    assert all(m["role"] != "tool" for m in sanitized)


def test_valid_tool_calls_pass_through_untouched():
    calls = [
        {"id": "call-1", "type": "function",
         "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}},
        {"id": "call-2", "type": "function",
         "function": {"name": "list_dir", "arguments": "{}"}},
    ]
    history = [
        {"role": "user", "content": "hi"},
        _assistant(calls, content="先看下文件"),
        _tool("call-1", "read_file"),
        _tool("call-2", "list_dir"),
    ]

    sanitized = _sanitize_tool_call_pairing(history)

    assert sanitized[1]["tool_calls"] == calls
    assert len([m for m in sanitized if m["role"] == "tool"]) == 2


def test_all_calls_broken_leaves_plain_assistant_text():
    history = [
        _assistant([
            {"id": "call-1", "type": "function",
             "function": {"name": "", "arguments": "garbage"}},
        ], content="我调用了工具"),
        _tool("call-1", "", "工具参数 JSON 解析失败"),
    ]

    sanitized = _sanitize_tool_call_pairing(history)

    assistant = sanitized[0]
    assert "tool_calls" not in assistant
    assert assistant["content"] == "我调用了工具"
    assert all(m["role"] != "tool" for m in sanitized)


def test_missing_arguments_key_is_treated_as_no_arg_call():
    """缺 arguments 键按无参调用处理（与执行层默认一致），不得误杀旧历史。"""
    history = [
        _assistant([
            {"id": "call-1", "type": "function",
             "function": {"name": "read_file"}},
        ]),
        _tool("call-1", "read_file"),
    ]

    sanitized = _sanitize_tool_call_pairing(history)

    assert [tc["id"] for tc in sanitized[0]["tool_calls"]] == ["call-1"]
    assert sanitized[1]["tool_call_id"] == "call-1"


def test_null_arguments_call_is_dropped():
    """arguments 键存在但为 None：执行层无法解析、严格网关拒绝，判不可回放。"""
    history = [
        _assistant([
            {"id": "call-1", "type": "function",
             "function": {"name": "read_file", "arguments": None}},
        ]),
        _tool("call-1", "read_file"),
    ]

    sanitized = _sanitize_tool_call_pairing(history)

    assert "tool_calls" not in sanitized[0]
    assert all(m["role"] != "tool" for m in sanitized)


def test_notice_between_tool_calls_and_results_is_repaired():
    """编排提醒等注入的 user 消息落在 assistant.tool_calls 与 tool 结果之间时，
    上游报 "An assistant message with 'tool_calls' must be followed by tool
    messages"；加载层把 tool 结果搬回 assistant 之后，正文一律保留不丢。"""
    nudge = "本回合已连续执行多步操作，仍未建立任务计划。"
    history = [
        {"role": "user", "content": "跑一下"},
        _assistant([
            {"id": "call-1", "type": "function",
             "function": {"name": "execute_command", "arguments": '{"command": "git log"}'}},
        ]),
        {"role": "user", "content": nudge},
        _tool("call-1", "execute_command", "9c91d32 chore: ..."),
    ]

    sanitized = _sanitize_tool_call_pairing(history)

    assert [m["role"] for m in sanitized] == ["user", "assistant", "tool", "user"]
    assert [tc["id"] for tc in sanitized[1]["tool_calls"]] == ["call-1"]
    assert sanitized[2]["tool_call_id"] == "call-1"
    assert sanitized[2]["content"] == "9c91d32 chore: ..."
    assert sanitized[3]["content"] == nudge


def test_calls_split_by_notice_are_reordered_by_call_order():
    """同一 step 的多个调用被注入消息拆散、且结果乱序时，按 tool_calls 顺序重排。"""
    nudge = "本回合已连续执行多步操作，仍未建立任务计划。"
    calls = [
        {"id": "call-1", "type": "function",
         "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}},
        {"id": "call-2", "type": "function",
         "function": {"name": "glob_files", "arguments": '{"pattern": "*.py"}'}},
    ]
    history = [
        _assistant(calls),
        {"role": "user", "content": nudge},
        _tool("call-2", "glob_files"),
        _tool("call-1", "read_file"),
    ]

    sanitized = _sanitize_tool_call_pairing(history)

    assert [m["role"] for m in sanitized] == ["assistant", "tool", "tool", "user"]
    assert [m["tool_call_id"] for m in sanitized if m["role"] == "tool"] == ["call-1", "call-2"]
    assert sanitized[0]["tool_calls"] == calls


def test_already_adjacent_pairs_keep_original_order():
    """已经合法相邻的调用组不得被重排（幂等，避免每轮请求扰动前缀缓存）。"""
    nudge = "本回合已连续执行多步操作，仍未建立任务计划。"
    history = [
        {"role": "user", "content": "hi"},
        _assistant([
            {"id": "call-1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}},
        ], content="先读文件"),
        _tool("call-1", "read_file"),
        {"role": "user", "content": nudge},
    ]

    sanitized = _sanitize_tool_call_pairing(history)

    assert [m["role"] for m in sanitized] == ["user", "assistant", "tool", "user"]
    assert sanitized[1]["content"] == "先读文件"
    assert sanitized[2]["tool_call_id"] == "call-1"
    assert sanitized[3]["content"] == nudge
