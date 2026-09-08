"""Immutable fingerprints captured from the exact provider request."""

from dataclasses import dataclass
import hashlib
import json

from core.harness.models import RequestFingerprint


_EXCLUDED_OPTION_KEYS = frozenset({"api_key", "messages", "input", "tools"})


@dataclass(frozen=True)
class PreparedStreamRequest:
    protocol: str
    kwargs: dict


def _responses_input_from_messages(messages):
    """Convert Chat Completions history into Responses input items."""

    def convert_content(content):
        if not isinstance(content, list):
            return content
        blocks = []
        for block in content:
            if not isinstance(block, dict):
                raise ValueError("Responses 多模态内容块必须是对象")
            block_type = block.get("type")
            if block_type in {"text", "input_text"}:
                blocks.append({"type": "input_text", "text": str(block.get("text") or "")})
            elif block_type in {"image_url", "input_image"}:
                image_url = block.get("image_url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                if not image_url:
                    raise ValueError("Responses 图片内容缺少 image_url")
                blocks.append({"type": "input_image", "image_url": str(image_url)})
            else:
                raise ValueError(f"Responses 不支持的内容块: {block_type}")
        return blocks

    items = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role in {"system", "developer", "user"}:
            items.append({"role": role, "content": convert_content(content)})
        elif role == "assistant":
            if content:
                items.append({"role": "assistant", "content": convert_content(content)})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id"),
                        "name": function.get("name"),
                        "arguments": function.get("arguments") or "{}",
                    }
                )
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not call_id:
                raise ValueError("Responses 工具结果缺少 tool_call_id")
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": (
                        content
                        if isinstance(content, str)
                        else json.dumps(content, ensure_ascii=False)
                    ),
                }
            )
        else:
            raise ValueError(f"Responses 不支持的消息角色: {role}")
    return items


def _responses_tools_from_chat_tools(tools):
    """Flatten Chat Completions function wrappers into Responses tools."""
    result = []
    for tool in tools or []:
        if tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise ValueError("Responses 仅支持显式 function 工具定义")
        function = tool["function"]
        result.append(
            {
                "type": "function",
                "name": function["name"],
                "description": function.get("description", ""),
                "parameters": function.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return result


def prepare_stream_request(
    protocol: str,
    chat_kwargs: dict,
    timeout: int,
    *,
    native_computer_tools: tuple | list = (),
) -> PreparedStreamRequest:
    """Build the one kwargs mapping that will be submitted to the provider.

    native_computer_tools（Task 11a，设计 11.1）是 capability 分支直接传入的
    精确 Responses 原生 computer 工具定义（computer_select_target function
    schema 与/或 {"type":"computer"} built-in）：只追加在 flattened function
    tools 之后，不经 _responses_tools_from_chat_tools（它仍拒绝任意 built-in）。
    非 responses 协议携带非空原生工具是接线错误，显式失败绝不静默丢弃。
    """
    if protocol != "responses":
        if native_computer_tools:
            raise ValueError("native computer tools 仅支持 responses 协议")
        provider_kwargs = dict(chat_kwargs)
        provider_kwargs["timeout"] = timeout
        return PreparedStreamRequest(protocol=protocol, kwargs=provider_kwargs)

    provider_kwargs = {
        "model": chat_kwargs["model"],
        "api_key": chat_kwargs.get("api_key"),
        "input": _responses_input_from_messages(chat_kwargs.get("messages") or []),
        "tools": [
            *_responses_tools_from_chat_tools(chat_kwargs.get("tools") or []),
            *(native_computer_tools or ()),
        ],
        "stream": True,
        "timeout": timeout,
    }
    for source, target in (
        ("api_base", "api_base"),
        ("max_tokens", "max_output_tokens"),
        ("tool_choice", "tool_choice"),
        ("num_retries", "num_retries"),
        ("extra_body", "extra_body"),
        ("parallel_tool_calls", "parallel_tool_calls"),
    ):
        if source in chat_kwargs:
            provider_kwargs[target] = chat_kwargs[source]
    if chat_kwargs.get("reasoning_effort"):
        provider_kwargs["reasoning"] = {"effort": chat_kwargs["reasoning_effort"]}
    return PreparedStreamRequest(protocol=protocol, kwargs=provider_kwargs)


def prepare_native_continuation_request(turn_state, call_output, *, timeout):
    """Build the PreparedStreamRequest for one native computer continuation Step.

    Task 11b（设计 11.3）：原生电脑操控活动 continuation 的下一 Step 不重发
    完整消息历史——input 只有 computer_call_output，model/tools/instructions/
    options 经 turn_state 字节级重发，previous_response_id 指向上一响应。
    协议形状由 core.computer.provider_openai.build_continuation_request 构造
    （协议层职责），本函数只负责把 timeout 按当前 Step 注入并包装成与
    prepare_stream_request 同一的请求准备接缝，使 RequestEnvelope.capture
    记录续传请求的真实最终字节。timeout 必须显式提供（与
    prepare_stream_request 的必填语义一致）。
    """
    from core.computer.provider_openai import build_continuation_request

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("native continuation request requires a numeric timeout")
    provider_kwargs = build_continuation_request(turn_state, call_output)
    provider_kwargs["timeout"] = timeout
    return PreparedStreamRequest(protocol="responses", kwargs=provider_kwargs)


def _json_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _cache_lane_sha256(kwargs: dict) -> str:
    extra_body = kwargs.get("extra_body")
    if isinstance(extra_body, dict) and "prompt_cache_key" in extra_body:
        return _sha256(str(extra_body["prompt_cache_key"]).encode("utf-8"))
    metadata = kwargs.get("metadata")
    if isinstance(metadata, dict) and "user_id" in metadata:
        return _sha256(str(metadata["user_id"]).encode("utf-8"))
    extra_headers = kwargs.get("extra_headers")
    if isinstance(extra_headers, dict) and "x-dashscope-session-cache" in extra_headers:
        # header 值恒为 "enable"，只哈希它会让所有 Qwen 请求挤进同一条
        # lane，journal 诊断无法区分缓存通道；并入模型与路由后同参数请求
        # 仍字节稳定（保活请求与主请求落同一条 lane）。会话身份不随请求
        # 发送（DashScope 按前缀/密钥侧建缓存），故不参与哈希。
        lane_material = json.dumps(
            {
                "session_cache": extra_headers["x-dashscope-session-cache"],
                "model": str(kwargs.get("model") or ""),
                "api_base": str(kwargs.get("api_base") or ""),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _sha256(lane_material.encode("utf-8"))
    return ""


@dataclass(frozen=True)
class RequestEnvelope:
    protocol: str
    cache_scope: str
    request_reason: str
    attempt: int
    deepseek_generation_id: int | None
    deepseek_message_count: int | None
    fingerprint: RequestFingerprint

    @classmethod
    def capture(
        cls,
        prepared: PreparedStreamRequest,
        *,
        cache_scope: str,
        request_reason: str,
        attempt: int,
        deepseek_generation_id: int | None,
        deepseek_message_count: int | None,
    ) -> "RequestEnvelope":
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be a one-based integer")

        kwargs = prepared.kwargs
        messages = kwargs.get("input") if prepared.protocol == "responses" else kwargs.get("messages")
        messages = messages or []
        system_messages = [
            message
            for message in messages
            if isinstance(message, dict) and message.get("role") == "system"
        ]
        tools = kwargs.get("tools") or []
        options = {
            key: value
            for key, value in kwargs.items()
            if key not in _EXCLUDED_OPTION_KEYS
        }

        messages_payload = _json_bytes(messages)
        system_payload = _json_bytes(system_messages)
        tools_payload = _json_bytes(tools)
        options_payload = _json_bytes(options)
        fingerprint = RequestFingerprint(
            messages_sha256=_sha256(messages_payload),
            system_sha256=_sha256(system_payload),
            tools_sha256=_sha256(tools_payload),
            options_sha256=_sha256(options_payload),
            cache_lane_sha256=_cache_lane_sha256(kwargs),
            message_count=len(messages),
            system_message_count=len(system_messages),
            request_bytes=len(messages_payload),
            system_bytes=len(system_payload),
            tools_bytes=len(tools_payload),
            options_bytes=len(options_payload),
        )
        return cls(
            protocol=prepared.protocol,
            cache_scope=str(cache_scope),
            request_reason=str(request_reason),
            attempt=attempt,
            deepseek_generation_id=deepseek_generation_id,
            deepseek_message_count=deepseek_message_count,
            fingerprint=fingerprint,
        )
