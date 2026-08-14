import pytest

from core.harness.request_envelope import (
    PreparedStreamRequest,
    RequestEnvelope,
    prepare_stream_request,
)


def capture_messages_sha(messages):
    prepared = PreparedStreamRequest(
        "openai",
        {
            "model": "m",
            "messages": messages,
            "tools": [],
            "timeout": 90,
        },
    )
    return RequestEnvelope.capture(
        prepared,
        cache_scope="main",
        request_reason="冷启动",
        attempt=1,
        deepseek_generation_id=None,
        deepseek_message_count=None,
    ).fingerprint.messages_sha256


def test_envelope_excludes_api_key_and_raw_content():
    prepared = PreparedStreamRequest(
        "openai",
        {
            "model": "m",
            "api_key": "sk-secret-value",
            "messages": [{"role": "user", "content": "private body"}],
            "tools": [],
            "timeout": 90,
        },
    )
    envelope = RequestEnvelope.capture(
        prepared,
        cache_scope="main",
        request_reason="冷启动",
        attempt=1,
        deepseek_generation_id=None,
        deepseek_message_count=None,
    )
    assert "secret" not in repr(envelope)
    assert "private body" not in repr(envelope)
    assert len(envelope.fingerprint.messages_sha256) == 64


def test_envelope_hash_changes_when_key_order_changes():
    first = [{"role": "assistant", "content": "x", "reasoning_content": ""}]
    second = [{"content": "x", "role": "assistant", "reasoning_content": ""}]
    assert capture_messages_sha(first) != capture_messages_sha(second)


def test_responses_envelope_hashes_translated_input_and_tools():
    chat_messages = [
        {"role": "system", "content": "stable"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "lookup",
            "content": "private result",
        },
    ]
    chat_tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    prepared = prepare_stream_request(
        "responses",
        {
            "model": "openai/gpt-5",
            "api_key": "sk-secret-value",
            "messages": chat_messages,
            "tools": chat_tools,
            "stream": True,
        },
        90,
    )
    envelope = RequestEnvelope.capture(
        prepared,
        cache_scope="main",
        request_reason="冷启动",
        attempt=1,
        deepseek_generation_id=None,
        deepseek_message_count=None,
    )

    assert "messages" not in prepared.kwargs
    assert prepared.kwargs["input"][1]["type"] == "function_call"
    assert prepared.kwargs["tools"][0]["name"] == "lookup"
    assert envelope.fingerprint.messages_sha256 == (
        "ea711d8052d3fe02a36dbeadb60efc5b37e3f4c842630350203c52a8ec8ec26d"
    )
    assert envelope.fingerprint.tools_sha256 == (
        "0c82f92b9ade48c4143e8a5c84d800b7ae526fe4c48493d6e71cc12aa4819a4b"
    )


def test_chat_preparation_copies_outer_mapping_in_order_and_appends_timeout():
    messages = [{"role": "user", "content": "private body"}]
    tools = []
    chat_kwargs = {
        "model": "m",
        "api_key": "sk-secret-value",
        "messages": messages,
        "tools": tools,
        "stream": True,
    }

    prepared = prepare_stream_request("openai", chat_kwargs, 90)

    assert list(prepared.kwargs) == [
        "model",
        "api_key",
        "messages",
        "tools",
        "stream",
        "timeout",
    ]
    assert prepared.kwargs is not chat_kwargs
    assert prepared.kwargs["messages"] is messages
    assert prepared.kwargs["tools"] is tools


def test_envelope_rejects_non_json_nan_options():
    prepared = PreparedStreamRequest(
        "openai",
        {
            "model": "m",
            "messages": [],
            "tools": [],
            "temperature": float("nan"),
        },
    )

    with pytest.raises(ValueError, match="Out of range float values"):
        RequestEnvelope.capture(
            prepared,
            cache_scope="main",
            request_reason="冷启动",
            attempt=1,
            deepseek_generation_id=None,
            deepseek_message_count=None,
        )
