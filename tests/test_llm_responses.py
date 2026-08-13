from types import SimpleNamespace

import pytest

import core.llm as llm
import core.model_manager as model_manager


def _event(event_type, **kwargs):
    return SimpleNamespace(type=event_type, **kwargs)


def test_responses_input_preserves_messages_and_tool_rounds():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "file body"},
    ]

    assert llm._responses_input_from_messages(messages) == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect"},
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "read_file",
            "arguments": '{"path":"a.py"}',
        },
        {"type": "function_call_output", "call_id": "call-1", "output": "file body"},
    ]


def test_responses_input_converts_multimodal_chat_blocks():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }
    ]

    assert llm._responses_input_from_messages(messages) == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "看图"},
                {"type": "input_image", "image_url": "data:image/png;base64,abc"},
            ],
        }
    ]

def test_responses_tools_flatten_chat_completion_schema():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]

    assert llm._responses_tools_from_chat_tools(tools) == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]


def test_responses_stream_normalizes_text_reasoning_tools_and_usage():
    completed = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=120,
            output_tokens=30,
            total_tokens=150,
            input_tokens_details=SimpleNamespace(cached_tokens=80),
        )
    )
    events = [
        _event(
            "response.output_item.added",
            output_index=2,
            item=SimpleNamespace(
                type="function_call",
                id="fc-1",
                call_id="call-1",
                name="read_file",
            ),
        ),
        _event("response.function_call_arguments.delta", output_index=2, delta='{"path":'),
        _event("response.function_call_arguments.delta", output_index=2, delta='"a.py"}'),
        _event("response.reasoning_summary_text.delta", delta="先检查"),
        _event("response.output_text.delta", delta="完成"),
        _event("response.completed", response=completed),
    ]

    chunks = list(llm._responses_stream_as_chat_chunks(events))
    tool_delta = chunks[0].choices[0].delta.tool_calls[0]
    assert tool_delta.index == 2
    assert tool_delta.id == "call-1"
    assert tool_delta.function.name == "read_file"
    assert chunks[1].choices[0].delta.tool_calls[0].function.arguments == '{"path":'
    assert chunks[3].choices[0].delta.reasoning_content == "先检查"
    assert chunks[4].choices[0].delta.content == "完成"
    assert chunks[-1].choices[0].finish_reason == "tool_calls"
    assert chunks[-1].usage.prompt_tokens == 120
    assert chunks[-1].usage.completion_tokens == 30
    assert chunks[-1].usage.prompt_tokens_details.cached_tokens == 80


def test_responses_failed_event_does_not_fall_back():
    failed = SimpleNamespace(error=SimpleNamespace(message="bad request"))

    with pytest.raises(RuntimeError, match="bad request"):
        list(
            llm._responses_stream_as_chat_chunks(
                [_event("response.failed", response=failed)]
            )
        )


def test_responses_max_output_incomplete_maps_to_length_finish():
    response = SimpleNamespace(
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
    )

    chunks = list(
        llm._responses_stream_as_chat_chunks(
            [_event("response.incomplete", response=response)]
        )
    )

    assert chunks[-1].choices[0].finish_reason == "length"
    assert chunks[-1].usage.total_tokens == 15


def test_responses_protocol_calls_native_responses_api(monkeypatch):
    captured = {}

    def fake_responses(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(llm, "_litellm_responses", fake_responses)
    monkeypatch.setattr(
        llm,
        "_litellm_completion",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("不得降级到 completion")),
    )

    stream, _holder = llm._request_stream(
        "responses",
        {
            "model": "openai/deepseek-v4-pro",
            "api_key": "sk-test",
            "api_base": "https://api.deepseek.com",
            "messages": [{"role": "user", "content": "ping"}],
            "tools": [],
            "stream": True,
            "max_tokens": 32,
            "tool_choice": "auto",
            "num_retries": 2,
        },
        timeout=15,
    )

    assert list(stream) == []
    assert captured["model"] == "openai/deepseek-v4-pro"
    assert captured["input"] == [{"role": "user", "content": "ping"}]
    assert captured["max_output_tokens"] == 32
    assert captured["stream"] is True
    assert "messages" not in captured


def test_chat_event_gateway_uses_configured_responses_protocol(monkeypatch):
    captured = []

    def fake_responses(**kwargs):
        captured.append(kwargs)
        response = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=2, output_tokens=1, total_tokens=3)
        )
        return [
            _event("response.output_text.delta", delta="真实 Responses"),
            _event("response.completed", response=response),
        ]

    monkeypatch.setattr(model_manager, "get_model_protocol", lambda *_: "responses")
    monkeypatch.setattr(llm, "_litellm_responses", fake_responses)

    events = list(
        llm.chat_with_model_events(
            "openai/test-model",
            "sk-test",
            [{"role": "user", "content": "ping"}],
            "https://api.example.com",
            persist=False,
        )
    )

    assert any(
        event.kind == "assistant_delta" and event.payload.get("text") == "真实 Responses"
        for event in events
    )
    assert captured
    assert captured[0]["input"][-1] == {"role": "user", "content": "ping"}
