import json


def test_request_diagnostic_is_deterministic_and_contains_no_plaintext():
    from core.llm import _build_cache_request_diagnostic

    messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "SECRET-USER-TEXT"},
    ]
    tools = [{
        "type": "function",
        "function": {
            "name": "secret_tool",
            "description": "SECRET-TOOL-ARG",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    first = _build_cache_request_diagnostic(
        "diag-session", "main", "OPENAI/DeepSeek-V4",
        "https://API.EXAMPLE/v1/", tools, messages,
    )
    second = _build_cache_request_diagnostic(
        "diag-session", "main", "OPENAI/DeepSeek-V4",
        "https://API.EXAMPLE/v1/", tools, messages,
    )

    assert first == second
    assert first["model"] == "openai/deepseek-v4"
    assert first["api_base"] == "https://api.example/v1"
    assert first["message_count"] == 2
    assert first["system_message_count"] == 1
    for key in ("messages_sha256", "system_sha256", "tools_sha256"):
        assert len(first[key]) == 64
    serialized = json.dumps(first, ensure_ascii=False)
    assert "SECRET-USER-TEXT" not in serialized
    assert "SECRET-TOOL-ARG" not in serialized


def test_request_details_measure_append_and_middle_rewrite_prefixes():
    from core.llm import _classify_cache_request_details

    session = "diag-prefix-session"
    base = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "first"},
    ]
    first = _classify_cache_request_details(
        session, "main", "openai/deepseek-v4", "https://api.example/v1",
        "tools-v1", base,
    )
    appended = _classify_cache_request_details(
        session, "main", "openai/deepseek-v4", "https://api.example/v1",
        "tools-v1", base + [{"role": "assistant", "content": "answer"}],
    )
    rewritten = _classify_cache_request_details(
        session, "main", "openai/deepseek-v4", "https://api.example/v1",
        "tools-v1", [
            {"role": "system", "content": "changed"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
        ],
    )

    assert first["request_reason"] == "冷启动"
    assert first["common_prefix_messages"] == 0
    assert appended["request_reason"] == ""
    assert appended["common_prefix_messages"] == len(base)
    assert rewritten["request_reason"] == "上下文压缩或历史重写"
    assert rewritten["common_prefix_messages"] == 0


def test_persist_diagnostic_distinguishes_valid_invalid_and_missing_usage(monkeypatch):
    import core.app_context as app_context
    from core.llm import _persist_cache_request_diagnostic

    captured = []

    class FakeManager:
        def add_cache_request_diagnostic(self, diagnostic):
            captured.append(diagnostic)

    monkeypatch.setattr(app_context, "get_manager", lambda: FakeManager())
    details = {
        "session_id": "persist-diag",
        "model": "openai/deepseek-v4",
        "api_base": "https://api.example/v1",
        "cache_scope": "main",
        "request_reason": "",
        "messages_sha256": "a" * 64,
        "system_sha256": "b" * 64,
        "tools_sha256": "c" * 64,
        "message_count": 2,
        "system_message_count": 1,
        "common_prefix_messages": 1,
        "request_bytes": 100,
        "system_bytes": 50,
        "tools_bytes": 75,
    }
    valid_usage = {
        "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
        "prompt_cache_hit_tokens": 990, "prompt_cache_miss_tokens": 10,
    }
    invalid_usage = {
        "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
        "prompt_cache_hit_tokens": 990, "prompt_cache_miss_tokens": 9,
    }

    _persist_cache_request_diagnostic(details, valid_usage)
    _persist_cache_request_diagnostic(details, invalid_usage)
    _persist_cache_request_diagnostic(details, None)

    assert captured[0]["usage_valid"] is True
    assert captured[0]["prompt_tokens"] == 1000
    assert captured[0]["cached_tokens"] == 990
    assert captured[0]["cache_miss_tokens"] == 10
    assert captured[1]["usage_valid"] is False
    assert any("cache hit + miss" in err for err in captured[1]["usage_errors"])
    assert captured[2]["usage_valid"] is False
    assert captured[2]["usage_errors"] == ("usage missing",)
