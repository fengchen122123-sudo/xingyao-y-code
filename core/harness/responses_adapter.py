"""Adapters for converting OpenAI Responses protocol stream events into Chat completion chunks."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from core.llm_transport import _close_stream_quietly

def _obj_value(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _chat_chunk(*, content="", reasoning="", tool_calls=None, finish_reason=None,
                usage=None, computer_call=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls or [],
        computer_call=computer_call,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _merge_stream_response_id(current, seen):
    """合并流中出现的 response id；不同 id 显式冲突失败。

    冲突错误消息绝不内嵌任一 response id 原文（11b 用户裁决，Task 16
    隐私审计）——该 RuntimeError 可经 turn_error 外泄到 UI/持久化面。
    """
    seen = str(seen or "")
    if not seen:
        return current
    if current and current != seen:
        raise RuntimeError(
            "Responses 流的 response id 冲突（两个不同的 id，原文不回显）"
        )
    return seen


def _plain_value(value):
    """把上游事件对象树规范化为纯 dict/list/标量（computer_call 载荷用）。"""
    if isinstance(value, dict):
        return {key: _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    if isinstance(value, SimpleNamespace):
        return {key: _plain_value(item) for key, item in vars(value).items()}
    return value


def _responses_usage_as_chat_usage(response):
    usage = _obj_value(response, "usage")
    if usage is None:
        return None
    input_details = _obj_value(usage, "input_tokens_details")
    cached_tokens = int(_obj_value(input_details, "cached_tokens", 0) or 0)
    prompt_tokens = int(_obj_value(usage, "input_tokens", 0) or 0)
    completion_tokens = int(_obj_value(usage, "output_tokens", 0) or 0)
    total_tokens = int(
        _obj_value(usage, "total_tokens", prompt_tokens + completion_tokens)
        or prompt_tokens + completion_tokens
    )
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
    )


def _responses_stream_as_chat_chunks(events):
    """Responses 原始流 → chat chunks 的映射；生成器关闭时连带关闭底层流。"""
    try:
        yield from _responses_events_as_chat_chunks(events)
    finally:
        _close_stream_quietly(events)


def _normalize_responses_event_type(event_type) -> str:
    """Responses 事件 type 统一归一化为 'response.*' 字符串。

    上游原生流事件的 type 是裸字符串；而某些网关/桥接实现自己合成事件流的
    路径上（如 fake_stream：模型未登记原生流式能力时，改发非流式请求再
    合成事件流），合成事件的 type 直接填
    ResponsesAPIStreamEvents 枚举成员，str() 得到的是成员名
    （如 'ResponsesAPIStreamEvents.RESPONSE_COMPLETED'）而非事件值，必须用
    .value 还原，否则映射器全部失配。
    """
    if isinstance(event_type, str):
        return event_type
    value = getattr(event_type, "value", None)
    if isinstance(value, str):
        return value
    return str(event_type or "")


def _responses_events_as_chat_chunks(events):
    """将 Responses SSE 事件严格映射成现有工具循环消费的 chat chunks。"""
    saw_tool_calls = False
    response_id = ""
    # 已由 output_item.added 产出过的 function_call call_id：真流式路径
    # completed.output[] 会再次携带同一调用，用于去重（fake_stream 非流式合成
    # 路径相反，没有 added/delta 事件，依赖 completed 兜底产出）。
    emitted_call_ids = set()
    ignored = {
        "response.in_progress",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.done",
        "response.refusal.done",
        "response.output_text.annotation.added",
        "response.function_call_arguments.done",
        "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
    }
    for event in events:
        event_type = _normalize_responses_event_type(_obj_value(event, "type", ""))
        if event_type == "response.output_text.delta":
            yield _chat_chunk(content=str(_obj_value(event, "delta", "")))
        elif event_type == "response.reasoning_summary_text.delta":
            yield _chat_chunk(reasoning=str(_obj_value(event, "delta", "")))
        elif event_type == "response.refusal.delta":
            yield _chat_chunk(content=str(_obj_value(event, "delta", "")))
        elif event_type == "response.created":
            # Task 11a：记录 response id，供 computer_call 载荷与原生续传使用
            response_id = _merge_stream_response_id(
                response_id,
                _obj_value(_obj_value(event, "response"), "id", ""),
            )
        elif event_type == "response.output_item.added":
            item = _obj_value(event, "item")
            if _obj_value(item, "type") != "function_call":
                # 半截 computer_call（added 事件）绝不转发：禁止基于半截 JSON
                # 触发执行，终态载荷只来自 output_item.done / completed。
                continue
            saw_tool_calls = True
            output_index = int(_obj_value(event, "output_index", 0) or 0)
            call_id = _obj_value(item, "call_id") or _obj_value(item, "id")
            name = _obj_value(item, "name")
            if not call_id or not name:
                raise RuntimeError("Responses function_call 缺少 call_id 或 name")
            emitted_call_ids.add(str(call_id))
            tool_call = SimpleNamespace(
                index=output_index,
                id=str(call_id),
                function=SimpleNamespace(name=str(name), arguments=""),
            )
            yield _chat_chunk(tool_calls=[tool_call])
        elif event_type == "response.function_call_arguments.delta":
            saw_tool_calls = True
            tool_call = SimpleNamespace(
                index=int(_obj_value(event, "output_index", 0) or 0),
                id=None,
                function=SimpleNamespace(
                    name="",
                    arguments=str(_obj_value(event, "delta", "")),
                ),
            )
            yield _chat_chunk(tool_calls=[tool_call])
        elif event_type == "response.output_item.done":
            item = _obj_value(event, "item")
            if _obj_value(item, "type") == "computer_call":
                # Task 11a：终态完整 computer_call 载荷，转发给 assembler 合并
                saw_tool_calls = True
                yield _chat_chunk(computer_call={
                    "response_id": response_id,
                    "item": _plain_value(item),
                })
            # 其余 item 类型维持忽略（function_call 已由 added+delta 事件覆盖）
            continue
        elif event_type == "response.completed":
            response = _obj_value(event, "response")
            response_id = _merge_stream_response_id(
                response_id, _obj_value(response, "id", "")
            )
            # fake_stream 合成路径（网关认为模型无原生流式能力时改发非流式
            # 请求再合成事件，如 openai/deepseek-flash）只在 completed 的 output[]
            # 里给出完整工具调用，没有 added/delta 事件，必须在此兜底提取；
            # 真流式路径 output[] 与 added 事件重复，按 call_id 去重。
            for output_item in (_obj_value(response, "output", None) or ()):
                item_type = _obj_value(output_item, "type")
                if item_type == "computer_call":
                    saw_tool_calls = True
                    yield _chat_chunk(computer_call={
                        "response_id": response_id,
                        "item": _plain_value(output_item),
                    })
                elif item_type == "function_call":
                    call_id = str(
                        _obj_value(output_item, "call_id")
                        or _obj_value(output_item, "id")
                        or ""
                    )
                    if call_id and call_id in emitted_call_ids:
                        continue
                    name = _obj_value(output_item, "name")
                    if not call_id or not name:
                        raise RuntimeError("Responses function_call 缺少 call_id 或 name")
                    emitted_call_ids.add(call_id)
                    saw_tool_calls = True
                    yield _chat_chunk(tool_calls=[SimpleNamespace(
                        index=len(emitted_call_ids) - 1,
                        id=call_id,
                        function=SimpleNamespace(
                            name=str(name),
                            arguments=str(_obj_value(output_item, "arguments") or ""),
                        ),
                    )])
            yield _chat_chunk(
                finish_reason="tool_calls" if saw_tool_calls else "stop",
                usage=_responses_usage_as_chat_usage(response),
            )
        elif event_type == "response.incomplete":
            response = _obj_value(event, "response")
            details = _obj_value(response, "incomplete_details")
            reason = str(_obj_value(details, "reason", ""))
            if reason == "max_output_tokens":
                yield _chat_chunk(
                    finish_reason="length",
                    usage=_responses_usage_as_chat_usage(response),
                )
                continue
            raise RuntimeError(f"Responses 请求未完成: {reason or '未知原因'}")
        elif event_type in {"response.failed", "error"}:
            response = _obj_value(event, "response", event)
            error = _obj_value(response, "error", response)
            message = _obj_value(error, "message") or str(error)
            raise RuntimeError(f"Responses 请求失败: {message}")
        elif event_type in ignored:
            continue
        else:
            raise RuntimeError(f"未支持的 Responses 流事件: {event_type or '<empty>'}")


def _native_continuation_instructions(request_kwargs) -> str:
    """从产生 computer_call 的 Step 请求中提取续传必须重发的 instructions。

    首请求的系统上下文位于 input 的 system/developer 条目（prepare_stream_request
    语义）；continuation 请求已有 instructions 键则直接复用。多个 system 条目按
    出现顺序以空行连接。设计 11.3：官方 Responses 的 instructions 不会经
    previous_response_id 自动继承，漏发会导致 system prompt 语义漂移。
    """
    instructions = request_kwargs.get("instructions")
    if isinstance(instructions, str):
        return instructions
    parts = []
    for item in request_kwargs.get("input") or ():
        if not isinstance(item, dict) or item.get("role") not in ("system", "developer"):
            continue
        content = item.get("content")
        if isinstance(content, str):
            if content:
                parts.append(content)
        elif isinstance(content, list):
            text = "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") in ("text", "input_text")
            )
            if text:
                parts.append(text)
    return "\n\n".join(parts)

