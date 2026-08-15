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
) -> PreparedStreamRequest:
    """Build the one kwargs mapping that will be submitted to the provider."""
    if protocol != "responses":
        provider_kwargs = dict(chat_kwargs)
        provider_kwargs["timeout"] = timeout
        return PreparedStreamRequest(protocol=protocol, kwargs=provider_kwargs)

    provider_kwargs = {
        "model": chat_kwargs["model"],
        "api_key": chat_kwargs.get("api_key"),
        "input": _responses_input_from_messages(chat_kwargs.get("messages") or []),
        "tools": _responses_tools_from_chat_tools(chat_kwargs.get("tools") or []),
        "stream": True,
        "timeout": timeout,
    }
    for source, target in (
        ("api_base", "api_base"),
        ("max_tokens", "max_output_tokens"),
        ("tool_choice", "tool_choice"),
        ("num_retries", "num_retries"),
        ("extra_body", "extra_body"),
    ):
        if source in chat_kwargs:
            provider_kwargs[target] = chat_kwargs[source]
    if chat_kwargs.get("reasoning_effort"):
        provider_kwargs["reasoning"] = {"effort": chat_kwargs["reasoning_effort"]}
    return PreparedStreamRequest(protocol=protocol, kwargs=provider_kwargs)


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
