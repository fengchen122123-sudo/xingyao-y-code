from types import SimpleNamespace

from core.harness.stream_assembler import (
    AssembledResponse,
    StreamAssembler,
    StreamUpdate,
)


def _chunk(
    *,
    content="",
    reasoning="",
    tool_calls=None,
    provider_fields=None,
    finish_reason=None,
    usage=None,
):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        provider_specific_fields=provider_fields,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


def _usage_only(value):
    return SimpleNamespace(choices=[], usage=value)


def _tool_call(index, call_id, name, arguments):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_ordinary_content_chunks_are_assembled_in_order():
    assembler = StreamAssembler()

    assert assembler.consume(_chunk(content="hel")) == StreamUpdate("hel", "")
    assert assembler.consume(_chunk(content="lo")) == StreamUpdate("lo", "")

    assert assembler.finish() == AssembledResponse(
        content="hello",
        reasoning_content=None,
        tool_calls=[],
        thinking_blocks=None,
        finish_reason=None,
        usage=None,
    )


def test_native_reasoning_chunks_keep_reasoning_separate_from_public_content():
    assembler = StreamAssembler()

    assert assembler.consume(_chunk(reasoning="private ")) == StreamUpdate(
        "<think>private ",
        "private ",
    )
    assert assembler.consume(_chunk(reasoning="trace")) == StreamUpdate(
        "trace",
        "trace",
    )
    assert assembler.consume(_chunk(content="answer")) == StreamUpdate(
        "</think>answer",
        "",
    )

    response = assembler.finish()
    assert response.content == "answer"
    assert response.reasoning_content == "private trace"


def test_split_tool_call_id_name_and_arguments_are_merged_once():
    assembler = StreamAssembler()

    assembler.consume(_chunk(tool_calls=[
        _tool_call(0, None, "read_file", '{"file_'),
    ]))
    assembler.consume(_chunk(tool_calls=[
        _tool_call(0, "provider-call", "read_file", 'path":"a.py"}'),
    ]))

    assert assembler.finish().tool_calls == [{
        "id": "provider-call",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"file_path":"a.py"}'},
    }]


def test_missing_tool_call_id_uses_current_index_fallback():
    assembler = StreamAssembler()
    assembler.consume(_chunk(tool_calls=[
        _tool_call(0, None, "read_file", '{"file_path":"a.py"}'),
    ]))

    assert assembler.finish().tool_calls == [{
        "id": "call_0",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"file_path":"a.py"}'},
    }]


def test_signed_anthropic_thinking_blocks_are_preserved():
    signed = [{"type": "thinking", "thinking": "分析", "signature": "sig"}]
    assembler = StreamAssembler(keep_thinking_blocks=True)
    assembler.consume(_chunk(provider_fields={"thinking_blocks": signed}))
    signed[0]["signature"] = "mutated-after-consume"

    assert assembler.finish().thinking_blocks == [
        {"type": "thinking", "thinking": "分析", "signature": "sig"},
    ]


def test_incomplete_thinking_blocks_are_omitted():
    assembler = StreamAssembler(keep_thinking_blocks=True)
    assembler.consume(_chunk(provider_fields={
        "thinking_blocks": [
            {"type": "thinking", "thinking": "unsigned"},
            {"type": "redacted_thinking"},
        ],
    }))

    assert assembler.finish().thinking_blocks is None


def test_only_the_final_usage_chunk_is_retained():
    first = SimpleNamespace(total_tokens=10)
    final = SimpleNamespace(total_tokens=20)
    assembler = StreamAssembler()

    assembler.consume(_usage_only(first))
    assembler.consume(_usage_only(final))

    assert assembler.finish().usage is final


def test_finish_reason_length_is_preserved():
    assembler = StreamAssembler()
    assembler.consume(_chunk(content="partial", finish_reason="length"))

    assert assembler.finish().finish_reason == "length"


def test_deepseek_thinking_tool_call_preserves_empty_reasoning_string():
    assembler = StreamAssembler(require_tool_call_reasoning=True)
    assembler.consume(_chunk(tool_calls=[
        _tool_call(0, None, "read_file", "{}"),
    ]))

    assert assembler.finish().reasoning_content == ""


def test_deepseek_thinking_text_reply_keeps_empty_reasoning_string():
    """无工具调用的常规回复也要带空串：留 None 会让下一轮历史缺字段，被上游拒绝。"""
    assembler = StreamAssembler(require_tool_call_reasoning=True)
    assembler.consume(_chunk(content="拉好了"))

    assert assembler.finish().reasoning_content == ""


def test_deepseek_thinking_text_reply_preserves_real_reasoning():
    assembler = StreamAssembler(require_tool_call_reasoning=True)
    assembler.consume(_chunk(reasoning="先看下工作区状态", content="拉好了"))

    assert assembler.finish().reasoning_content == "先看下工作区状态"


def test_non_reasoning_provider_strips_unsupported_reasoning_fields():
    assembler = StreamAssembler(
        keep_reasoning_fields=False,
        keep_thinking_blocks=False,
    )
    assembler.consume(_chunk(
        reasoning="private",
        provider_fields={
            "thinking_blocks": [
                {"type": "thinking", "thinking": "private", "signature": "sig"},
            ],
        },
    ))

    response = assembler.finish()
    assert response.reasoning_content is None
    assert response.thinking_blocks is None


def test_finish_drops_tool_call_with_empty_name_and_keeps_valid_sibling():
    """Agnes 网关 400 场景：流式残缺调用（空 name/空 arguments）必须在
    进入历史（内存/DB/DeepSeek 账本）之前丢弃，合法兄弟调用原样保留。"""
    assembler = StreamAssembler()
    assembler.consume(_chunk(tool_calls=[
        _tool_call(0, "call-bad", "", ""),
        _tool_call(1, "call-ok", "read_file", '{"file_path":"a.py"}'),
    ]))

    assert assembler.finish().tool_calls == [{
        "id": "call-ok",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"file_path":"a.py"}'},
    }]


def test_finish_normalizes_empty_arguments_to_empty_object():
    """有名字无参数的调用按无参调用回放 "{}"，不再携带非法的空串参数。"""
    assembler = StreamAssembler()
    assembler.consume(_chunk(tool_calls=[
        _tool_call(0, "call-1", "list_dir", ""),
    ]))

    assert assembler.finish().tool_calls == [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "list_dir", "arguments": "{}"},
    }]


def test_finish_repairs_truncated_arguments_to_wire_valid_json():
    """截断 JSON 参数按执行层同源的三段解析规整为可回放字节。"""
    assembler = StreamAssembler()
    assembler.consume(_chunk(tool_calls=[
        _tool_call(0, "call-1", "read_file", '{"file_path": "a.py'),
    ]))

    (call,) = assembler.finish().tool_calls
    import json as _json

    assert _json.loads(call["function"]["arguments"]) == {"file_path": "a.py"}


def test_finish_drops_tool_call_with_unparseable_arguments():
    """三段解析全部失败的调用整体丢弃，绝不把非法 JSON 送进历史。"""
    assembler = StreamAssembler()
    assembler.consume(_chunk(tool_calls=[
        _tool_call(0, "call-1", "read_file", "not-json{{"),
    ]))

    assert assembler.finish().tool_calls == []
