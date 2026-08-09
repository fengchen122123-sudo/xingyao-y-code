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
