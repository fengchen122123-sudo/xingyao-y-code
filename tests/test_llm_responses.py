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


def test_responses_bridge_enum_typed_events_are_normalized():
    """litellm chat→responses 桥接（DeepSeek 等 chat-only provider）合成事件的
    type 是 ResponsesAPIStreamEvents 枚举成员而非字符串，必须按 .value 归一化。"""
    try:
        from litellm.types.llms.openai import ResponsesAPIStreamEvents
    except ImportError:  # pragma: no cover - litellm 缺失时跳过
        pytest.skip("litellm types 不可用")

    completed = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
        )
    )
    events = [
        _event(ResponsesAPIStreamEvents.RESPONSE_CREATED, response=SimpleNamespace(id="r1")),
        _event(ResponsesAPIStreamEvents.OUTPUT_TEXT_DELTA, delta="你好"),
        _event(ResponsesAPIStreamEvents.RESPONSE_COMPLETED, response=completed),
    ]

    chunks = list(llm._responses_stream_as_chat_chunks(events))

    texts = "".join(c.choices[0].delta.content for c in chunks)
    assert texts == "你好"
    assert chunks[-1].choices[0].finish_reason == "stop"
    assert chunks[-1].usage.total_tokens == 15


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


# ---------------------------------------------------------------------------
# Task 11a：原生 computer_call 流事件（终态合并，禁止半截 JSON 执行）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", [
    {"type": "computer"},
    {"type": "web_search"},
    {"type": "code_interpreter"},
    {"type": "computer", "environment": "browser"},
], ids=["computer", "web-search", "code-interpreter", "computer-with-fields"])
def test_responses_tools_reject_builtin_tool_definitions(tool):
    """_responses_tools_from_chat_tools 仍拒绝任意 built-in：只允许 capability
    分支直接传入的精确 computer definition（Task 11a Step 3）。"""
    with pytest.raises(ValueError):
        llm._responses_tools_from_chat_tools([tool])


def _computer_call_chunks(chunks):
    return [
        chunk for chunk in chunks
        if getattr(chunk.choices[0].delta, "computer_call", None) is not None
    ]


def test_responses_stream_forwards_terminal_computer_call():
    """output_item.done 的 computer_call 是终态完整载荷：转发给 assembler 合并，
    finish_reason 为 tool_calls（回合必须继续，不能当最终文本结束）。"""
    events = [
        _event("response.created", response=SimpleNamespace(id="resp_1")),
        _event("response.output_item.done", item=SimpleNamespace(
            type="computer_call",
            call_id="call_1",
            status="completed",
            actions=[{"type": "click", "x": 1, "y": 1}],
        )),
        _event("response.completed", response=SimpleNamespace(id="resp_1", output=[])),
    ]

    chunks = list(llm._responses_stream_as_chat_chunks(events))

    payloads = _computer_call_chunks(chunks)
    assert len(payloads) == 1
    payload = payloads[0].choices[0].delta.computer_call
    assert payload["response_id"] == "resp_1"
    assert payload["item"]["type"] == "computer_call"
    assert payload["item"]["call_id"] == "call_1"
    assert payload["item"]["actions"] == [{"type": "click", "x": 1, "y": 1}]
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


def test_responses_stream_ignores_partial_computer_call_added_events():
    """output_item.added 携带的是半截 computer_call：绝不产生 computer_call 载荷，
    禁止基于半截 JSON 触发执行。"""
    events = [
        _event("response.created", response=SimpleNamespace(id="resp_1")),
        _event("response.output_item.added", output_index=0, item=SimpleNamespace(
            type="computer_call",
            call_id="call_1",
            actions=[{"type": "click", "x": 1, "y": 1}],
        )),
        _event("response.completed", response=SimpleNamespace(id="resp_1", output=[])),
    ]

    chunks = list(llm._responses_stream_as_chat_chunks(events))

    assert _computer_call_chunks(chunks) == []
    assert chunks[-1].choices[0].finish_reason == "stop"


def test_responses_stream_completed_output_carries_computer_call():
    """非流式桥接只在 response.completed 的 output[] 里给出 computer_call：
    同样作为终态完整载荷转发，response id 取自 completed。"""
    events = [
        _event("response.output_text.delta", delta="先看图"),
        _event("response.completed", response=SimpleNamespace(
            id="resp_2",
            output=[{
                "type": "computer_call",
                "call_id": "call_9",
                "status": "completed",
                "actions": [{"type": "screenshot"}],
            }],
        )),
    ]

    chunks = list(llm._responses_stream_as_chat_chunks(events))

    payloads = _computer_call_chunks(chunks)
    assert len(payloads) == 1
    payload = payloads[0].choices[0].delta.computer_call
    assert payload["response_id"] == "resp_2"
    assert payload["item"]["call_id"] == "call_9"
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


def test_responses_stream_rejects_conflicting_response_ids():
    events = [
        _event("response.created", response=SimpleNamespace(id="resp_1")),
        _event("response.completed", response=SimpleNamespace(id="resp_2", output=[])),
    ]

    with pytest.raises(RuntimeError, match="response id"):
        list(llm._responses_stream_as_chat_chunks(events))


def test_stream_assembler_merges_computer_call_chunks():
    """StreamAssembler 合并 computer_call 流事件：终态 payload 在 finish 时暴露，
    普通（默认关闭）回合保持 None，AssembledResponse 形状不变。"""
    from core.harness.stream_assembler import StreamAssembler

    payload = {
        "response_id": "resp_1",
        "item": {
            "type": "computer_call",
            "call_id": "call_1",
            "status": "completed",
            "actions": [{"type": "click", "x": 1, "y": 1}],
        },
    }

    def _computer_chunk(call_payload):
        delta = SimpleNamespace(
            content="",
            reasoning_content=None,
            tool_calls=[],
            computer_call=call_payload,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=None)],
            usage=None,
        )

    assembler = StreamAssembler()
    assembler.consume(_computer_chunk(payload))
    # done 与 completed 重复携带同一 call：合并为一个（后者覆盖前者）
    assembler.consume(_computer_chunk(payload))
    assembled = assembler.finish()
    assert assembled.computer_call == payload

    plain = StreamAssembler()
    plain.consume(
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="文本", reasoning_content=None, tool_calls=[]),
                finish_reason="stop",
            )],
            usage=None,
        )
    )
    assert plain.finish().computer_call is None
