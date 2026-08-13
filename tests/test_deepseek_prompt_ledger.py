import json
from copy import deepcopy
from types import SimpleNamespace

from core.memory import MemoryManager


def _message(role, content, **extra):
    return {"role": role, "content": content, **extra}


def _manager(*session_names):
    manager = MemoryManager(":memory:")
    for session_name in session_names:
        manager.create_session_explicit(session_name)
    return manager


def test_deepseek_generation_freezes_seed_and_tools():
    """防止当前 generation 因调用方修改对象或工具热加载而改变旧前缀。"""
    manager = _manager("session-a")
    seed = [_message("system", "stable"), _message("user", "first")]
    tools = [{"type": "function", "function": {"name": "alpha", "parameters": {}}}]

    generation = manager.get_or_create_deepseek_prompt_generation(
        "session-a", "openai/deepseek-v4-pro", "https://api.deepseek.com",
        "main", seed, tools,
    )
    seed[0]["content"] = "mutated"
    tools[0]["function"]["name"] = "beta"

    loaded = manager.get_deepseek_prompt_generation(generation["id"])
    assert loaded["messages"] == [
        _message("system", "stable"),
        _message("user", "first"),
    ]
    assert loaded["tools"][0]["function"]["name"] == "alpha"


def test_deepseek_generation_only_appends_messages():
    """防止第二轮覆盖、重排或重新序列化旧消息。"""
    manager = _manager("session-b")
    generation = manager.get_or_create_deepseek_prompt_generation(
        "session-b", "openai/deepseek-v4-pro", "https://api.deepseek.com/v1",
        "main", [_message("system", "s"), _message("user", "u1")], [],
    )
    manager.append_deepseek_prompt_messages(
        generation["id"],
        [_message("assistant", "a1"), _message("user", "u2")],
        expected_count=2,
    )

    loaded = manager.get_deepseek_prompt_generation(generation["id"])
    assert loaded["messages"] == [
        _message("system", "s"),
        _message("user", "u1"),
        _message("assistant", "a1"),
        _message("user", "u2"),
    ]


def test_deepseek_generation_rejects_stale_append():
    """防止并发回合把新消息接到错误的账本位置。"""
    manager = _manager("session-c")
    generation = manager.get_or_create_deepseek_prompt_generation(
        "session-c", "openai/deepseek-v4-pro", "https://api.deepseek.com",
        "main", [_message("system", "s")], [],
    )

    try:
        manager.append_deepseek_prompt_messages(
            generation["id"], [_message("user", "late")], expected_count=9,
        )
    except RuntimeError as error:
        assert "消息数量" in str(error)
    else:
        raise AssertionError("stale append must fail")


def test_deepseek_generation_identity_isolated_from_kimi_and_endpoints():
    """防止不同 Provider 或 endpoint 误用同一缓存账本。"""
    from core.llm import _uses_deepseek_prompt_ledger

    assert _uses_deepseek_prompt_ledger(
        "openai/deepseek-v4-pro", "https://api.deepseek.com", "main", True, "s1"
    )
    assert not _uses_deepseek_prompt_ledger(
        "anthropic/kimi-k2.5", "https://api.kimi.com/coding", "main", True, "s1"
    )
    assert not _uses_deepseek_prompt_ledger(
        "openai/deepseek-v4-pro", "https://relay.example/v1", "main", True, "s1"
    )
    assert not _uses_deepseek_prompt_ledger(
        "openai/deepseek-v4-pro", "https://api.deepseek.com", "subagent:explore", True, "s1"
    )


def test_prepare_deepseek_request_rotates_when_stable_header_changes():
    """防止 system/tools 已变化却继续向 DeepSeek 发送过期请求头。"""
    from core.llm import _prepare_deepseek_ledger_request

    manager = _manager("session-d")
    tools_v1 = [{"type": "function", "function": {"name": "alpha", "parameters": {}}}]
    first_input = [_message("system", "stable"), _message("user", "u1")]
    first = _prepare_deepseek_ledger_request(
        manager, "session-d", "openai/deepseek-v4-pro",
        "https://api.deepseek.com", "main", first_input, tools_v1,
    )
    manager.append_deepseek_prompt_messages(
        first["generation_id"], [_message("assistant", "a1")], expected_count=2,
    )

    rewritten_normal_history = [
        _message("system", "changed outside ledger"),
        _message("user", "summarized old history"),
        _message("assistant", "a1"),
        _message("system", "dynamic turn 2"),
        _message("user", "u2"),
    ]
    tools_v2 = tools_v1 + [
        {"type": "function", "function": {"name": "hot_loaded", "parameters": {}}}
    ]
    second = _prepare_deepseek_ledger_request(
        manager, "session-d", "openai/deepseek-v4-pro",
        "https://api.deepseek.com", "main", rewritten_normal_history, tools_v2,
    )

    assert second["generation_id"] != first["generation_id"]
    assert second["messages"] == rewritten_normal_history
    assert json.dumps(second["tools"], ensure_ascii=False) == json.dumps(
        tools_v2, ensure_ascii=False
    )
    assert manager.get_deepseek_prompt_generation(first["generation_id"])["tools"] == tools_v1


def test_deepseek_tools_are_sorted_without_changing_kimi_order():
    """防止 DeepSeek 工具注册顺序漂移，同时保护 Kimi 原有 payload 顺序。"""
    from core.llm import _select_request_tools_for_route

    tools = [
        {"type": "function", "function": {"name": "zeta", "parameters": {}}},
        {"type": "function", "function": {"name": "alpha", "parameters": {}}},
    ]

    deepseek = _select_request_tools_for_route(
        "openai/deepseek-v4-pro", "https://api.deepseek.com", tools
    )
    kimi = _select_request_tools_for_route(
        "openai/kimi-for-coding", "https://api.kimi.com/coding/v1", tools
    )

    assert [tool["function"]["name"] for tool in deepseek] == ["alpha", "zeta"]
    assert kimi is tools


def test_prepare_deepseek_request_appends_complete_tool_round():
    """防止 assistant tool_calls 或多条 tool result 在下一次请求中丢失/乱序。"""
    from core.llm import _prepare_deepseek_ledger_request

    manager = _manager("session-tool")
    first = _prepare_deepseek_ledger_request(
        manager, "session-tool", "openai/deepseek-v4-pro",
        "https://api.deepseek.com", "main",
        [_message("system", "stable"), _message("user", "inspect")], [],
    )
    tool_call = _message(
        "assistant", None, reasoning_content="reason",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "one", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "two", "arguments": "{}"}},
        ],
    )
    current = first["messages"] + [
        tool_call,
        _message("tool", "r1", tool_call_id="c1", name="one"),
        _message("tool", "r2", tool_call_id="c2", name="two"),
    ]
    second = _prepare_deepseek_ledger_request(
        manager, "session-tool", "openai/deepseek-v4-pro",
        "https://api.deepseek.com", "main", current, [],
    )

    assert second["messages"] == current


def test_prepare_deepseek_request_rotates_generation_when_history_cannot_align():
    """撤销或历史切换时创建冷启动 generation，绝不覆盖旧账本。"""
    from core.llm import _prepare_deepseek_ledger_request

    manager = _manager("session-rotate")
    first = _prepare_deepseek_ledger_request(
        manager, "session-rotate", "openai/deepseek-v4-pro",
        "https://api.deepseek.com", "main",
        [_message("system", "stable"), _message("user", "old")], [],
    )
    second_seed = [_message("system", "stable"), _message("user", "after undo")]
    second = _prepare_deepseek_ledger_request(
        manager, "session-rotate", "openai/deepseek-v4-pro",
        "https://api.deepseek.com", "main", second_seed, [],
    )

    assert second["generation_id"] != first["generation_id"]
    assert second["messages"] == second_seed
    assert manager.get_deepseek_prompt_generation(first["generation_id"])["messages"] == [
        _message("system", "stable"), _message("user", "old")
    ]


def test_official_deepseek_real_requests_reuse_previous_payload_prefix(monkeypatch):
    """截获真实 completion 边界，防止账本 helper 正确但请求接线绕过它。"""
    import core.app_context as app_context
    import core.llm as llm

    manager = _manager("payload-prefix")
    monkeypatch.setattr(app_context, "get_manager", lambda: manager)
    monkeypatch.setattr(llm, "get_tools_schema", lambda: [])
    monkeypatch.setattr(
        "core.model_manager.get_model_protocol",
        lambda model, api_base=None: "openai",
    )
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda model, api_base=None: {"enable_thinking": False},
    )
    captured = []

    def completion(**kwargs):
        captured.append(deepcopy(kwargs))
        delta = SimpleNamespace(
            content="answer", reasoning_content=None, tool_calls=None,
            provider_specific_fields=None,
        )
        chunk = SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
            usage=None,
        )
        return iter([chunk])

    monkeypatch.setattr(llm, "completion", completion)
    messages = [_message("system", "stable"), _message("user", "u1")]
    list(llm.chat_with_model_events(
        "openai/deepseek-v4-pro", "key", messages,
        "https://api.deepseek.com", persist=True,
        parent_session="payload-prefix", turn_id="turn-1",
        consume_background_notifications=False,
    ))
    messages.append(_message("user", "u2"))
    list(llm.chat_with_model_events(
        "openai/deepseek-v4-pro", "key", messages,
        "https://api.deepseek.com", persist=True,
        parent_session="payload-prefix", turn_id="turn-2",
        consume_background_notifications=False,
    ))

    assert len(captured) == 2
    assert captured[1]["messages"][:len(captured[0]["messages"])] == captured[0]["messages"]
    assert captured[1]["tools"] == captured[0]["tools"]


def test_deepseek_ledger_follows_session_rename_and_clear():
    """防止会话重命名丢失账本，或清空历史后复用旧前缀。"""
    manager = _manager("before")
    generation = manager.get_or_create_deepseek_prompt_generation(
        "before", "openai/deepseek-v4-pro", "https://api.deepseek.com",
        "main", [_message("system", "s"), _message("user", "old")], [],
    )

    assert manager.rename_session("before", "after") == (True, "after")
    renamed = manager.get_or_create_deepseek_prompt_generation(
        "after", "openai/deepseek-v4-pro", "https://api.deepseek.com",
        "main", [_message("system", "unused")], [],
    )
    assert renamed["id"] == generation["id"]

    manager.clear_session_messages("after")
    fresh = manager.get_or_create_deepseek_prompt_generation(
        "after", "openai/deepseek-v4-pro", "https://api.deepseek.com",
        "main", [_message("system", "s"), _message("user", "new")], [],
    )
    assert fresh["id"] != generation["id"]


def test_delete_session_removes_deepseek_ledger_rows():
    """防止删除会话后 prompt 正文长期残留或同名新会话复用旧账本。"""
    manager = _manager("delete-me")
    generation = manager.get_or_create_deepseek_prompt_generation(
        "delete-me", "openai/deepseek-v4-pro", "https://api.deepseek.com",
        "main", [_message("system", "secret")], [],
    )

    manager.delete_session("delete-me")

    assert manager.get_deepseek_prompt_generation(generation["id"]) is None


def test_prepare_deepseek_request_can_force_rotation_after_overflow():
    """压缩后的重试必须舍弃超长账本，否则会原样再次触发上下文超限。"""
    from core.llm import _prepare_deepseek_ledger_request

    manager = _manager("overflow")
    first = _prepare_deepseek_ledger_request(
        manager, "overflow", "openai/deepseek-v4-pro",
        "https://api.deepseek.com", "main",
        [_message("system", "s"), _message("user", "very old payload")], [],
    )
    compact = [_message("system", "s"), _message("user", "compressed")]

    rotated = _prepare_deepseek_ledger_request(
        manager, "overflow", "openai/deepseek-v4-pro",
        "https://api.deepseek.com", "main", compact, [], force_new=True,
    )

    assert rotated["generation_id"] != first["generation_id"]
    assert rotated["messages"] == compact


def test_deleted_session_rejects_new_or_appended_ledger_messages():
    """删除与在飞回合竞态不能重新生成孤儿账本。"""
    manager = _manager("race")
    generation = manager.get_or_create_deepseek_prompt_generation(
        "race", "openai/deepseek-v4-pro", "https://api.deepseek.com",
        "main", [_message("system", "s")], [],
    )
    manager.delete_session("race")

    for operation in (
        lambda: manager.get_or_create_deepseek_prompt_generation(
            "race", "openai/deepseek-v4-pro", "https://api.deepseek.com",
            "main", [_message("system", "new")], [],
        ),
        lambda: manager.append_deepseek_prompt_messages(
            generation["id"], [_message("assistant", "late")], expected_count=1,
        ),
    ):
        try:
            operation()
        except RuntimeError as error:
            assert "不存在" in str(error)
        else:
            raise AssertionError("deleted session must reject ledger writes")
