"""原生 HTTPX 流式传输核单元测试。

验证 SSE 解析、流块归一化、硬中断关闭、Token 快速估算与方言策略映射。
"""

import io
import json
import pytest

import core.llm_native_transport as nt
from core.llm_policy import ModelPolicy, get_model_policy, POLICY_REGISTRY
from core.llm_native_transport import (
    SSEDecoder,
    ChunkNormalizer,
    NativeStreamResponse,
    NativeSyncResponse,
    StreamChunk,
    fast_token_counter,
    _map_http_error,
    RateLimitError,
    ContextWindowExceededError,
    AuthenticationError,
    APIConnectionError,
)


def test_model_policy_resolution():
    """测试不同模型名称与 URL 正确解析对应 Policy。"""
    p_deepseek = get_model_policy("deepseek-chat")
    assert p_deepseek.provider_kind == "deepseek"

    p_kimi = get_model_policy("moonshot-v1-8k")
    assert p_kimi.provider_kind == "moonshot"

    p_glm = get_model_policy("glm-4-plus")
    assert p_glm.provider_kind == "zhipu"

    p_o1 = get_model_policy("o1-preview")
    assert p_o1.provider_kind == "openai_reasoning"
    assert p_o1.supports_temperature is False

    p_custom = get_model_policy("my-custom-model", api_base="https://my-proxy.com/v1")
    assert p_custom.provider_kind == "standard"


def test_sse_decoder_parsing():
    """测试 SSEDecoder 能够正确解析 SSE 文本流并跳过心跳与空行。"""
    raw_sse = (
        ": ping\n\n"
        "data: {\"choices\": [{\"delta\": {\"content\": \"Hello\"}}]}\n\n"
        ": keep-alive\n\n"
        "data: {\"choices\": [{\"delta\": {\"content\": \" World\"}}]}\n\n"
        "data: [DONE]\n"
    )
    lines = raw_sse.splitlines()
    events = list(SSEDecoder.iter_events(iter(lines)))

    assert len(events) == 2
    assert events[0]["choices"][0]["delta"]["content"] == "Hello"
    assert events[1]["choices"][0]["delta"]["content"] == " World"


def test_chunk_normalizer_reasoning_and_tools():
    """测试 ChunkNormalizer 正确提取推理链、工具调用与缓存明细。"""
    policy = POLICY_REGISTRY["deepseek"]
    normalizer = ChunkNormalizer(policy)

    raw_event = {
        "id": "chatcmpl-123",
        "model": "deepseek-reasoner",
        "choices": [{
            "index": 0,
            "delta": {
                "content": "Result",
                "reasoning_content": "Step 1: thinking",
                "tool_calls": [{
                    "index": 0,
                    "id": "call_abc",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "a.py"}'
                    }
                }]
            },
            "finish_reason": "tool_calls"
        }],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {
                "cached_tokens": 64
            }
        }
    }

    chunk = normalizer.normalize(raw_event)
    assert chunk is not None
    assert chunk.id == "chatcmpl-123"

    # Choices & delta
    assert len(chunk.choices) == 1
    choice = chunk.choices[0]
    assert choice.delta.content == "Result"
    assert choice.delta.reasoning_content == "Step 1: thinking"
    assert choice.finish_reason == "tool_calls"

    # Tool calls
    assert len(choice.delta.tool_calls) == 1
    tc = choice.delta.tool_calls[0]
    assert tc.index == 0
    assert tc.id == "call_abc"
    assert tc.function.name == "read_file"
    assert tc.function.arguments == '{"path": "a.py"}'

    # Usage
    assert chunk.usage is not None
    assert chunk.usage.prompt_tokens == 100
    assert chunk.usage.prompt_tokens_details["cached_tokens"] == 64
    assert chunk.usage.prompt_cache_hit_tokens == 64


def test_native_stream_close():
    """测试 NativeStreamResponse 的 close 方法能安全幂等地关闭底层流。"""
    class FakeResponse:
        def __init__(self):
            self.closed = False

        def iter_lines(self):
            yield "data: {\"choices\": [{\"delta\": {\"content\": \"hi\"}}]}"
            yield "data: [DONE]"

        def close(self):
            self.closed = True

    fake_resp = FakeResponse()
    policy = POLICY_REGISTRY["standard"]
    stream = NativeStreamResponse(fake_resp, ChunkNormalizer(policy))

    chunks = []
    for c in stream:
        chunks.append(c)
        # 提前关闭（模拟用户急停中断）
        stream.close()
        break

    assert len(chunks) == 1
    assert fake_resp.closed is True
    # 再次 close 不抛异常
    stream.close()


def test_map_http_errors():
    """测试 HTTP 异常正确映射为 NativeLLMError 派生类。"""
    class FakeHTTPResponse:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text

    err_401 = _map_http_error(FakeHTTPResponse(401, "Unauthorized: invalid api key"))
    assert isinstance(err_401, AuthenticationError)

    err_429 = _map_http_error(FakeHTTPResponse(429, "Rate limit exceeded"))
    assert isinstance(err_429, RateLimitError)

    err_ctx = _map_http_error(FakeHTTPResponse(400, "maximum context length is 128000 tokens"))
    assert isinstance(err_ctx, ContextWindowExceededError)

    err_500 = _map_http_error(FakeHTTPResponse(502, "Bad Gateway"))
    assert isinstance(err_500, APIConnectionError)


def test_fast_token_counter():
    """测试微秒级启发式 token 估算器。"""
    messages = [
        {"role": "system", "content": "你是一个资深的 Python 架构师。"},
        {"role": "user", "content": "def hello_world(): return 'hello'"}
    ]
    tokens = fast_token_counter("deepseek-chat", messages)
    assert tokens > 10
    assert isinstance(tokens, int)


def test_native_sync_response():
    """测试 NativeSyncResponse 正确解析非流式响应的 choices 与 usage。"""
    from core.llm_native_transport import NativeSyncResponse
    policy = POLICY_REGISTRY["deepseek"]
    normalizer = ChunkNormalizer(policy)

    raw_json = {
        "id": "chatcmpl-sync-1",
        "model": "deepseek-chat",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello sync",
                "reasoning_content": "Thought 1",
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 10,
            "total_tokens": 60,
            "prompt_tokens_details": {
                "cached_tokens": 32
            }
        }
    }

    resp = NativeSyncResponse(raw_json, normalizer)
    assert resp.id == "chatcmpl-sync-1"
    assert len(resp.choices) == 1
    assert resp.choices[0].delta.content == "Hello sync"
    assert resp.choices[0].delta.reasoning_content == "Thought 1"
    assert resp.usage.prompt_tokens == 50
    assert resp.usage.prompt_cache_hit_tokens == 32



# ---------------------------------------------------------------------------
# native_completion 关键行为（stream 契约 / 参数透传 / 路由分流）
# ---------------------------------------------------------------------------

class _FakeHttpResponse:
    def __init__(self, payload=None, lines=None):
        self.status_code = 200
        self._payload = payload or {}
        self._lines = lines or []
        self.closed = False

    def iter_lines(self):
        for line in self._lines:
            yield line

    def close(self):
        self.closed = True

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.last_payload = None
        self.last_url = None

    def build_request(self, method, url, headers=None, json=None):
        self.last_payload = json
        self.last_url = url
        return object()

    def send(self, req, stream=False, timeout=None):
        return self._response


_SYNC_PAYLOAD = {
    "id": "chatcmpl-fake",
    "model": "deepseek-chat",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "ok"},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def test_native_completion_defaults_to_non_stream(monkeypatch):
    """不传 stream 时必须按非流式处理（litellm.completion 契约），
    返回可直接取 .choices 的完整响应。"""
    client = _FakeClient(_FakeHttpResponse(payload=_SYNC_PAYLOAD))
    monkeypatch.setattr(nt, "_client_instance", client)
    result = nt.native_completion(
        model="deepseek-chat",
        api_key="k",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert isinstance(result, NativeSyncResponse)
    assert client.last_payload["stream"] is False
    assert result.choices[0].delta.content == "ok"


def test_native_completion_explicit_stream_true(monkeypatch):
    """显式 stream=True 走流式并自动带 include_usage。"""
    lines = [
        'data: {"choices": [{"index": 0, "delta": {"content": "hi"}}]}',
        "data: [DONE]",
    ]
    client = _FakeClient(_FakeHttpResponse(lines=lines))
    monkeypatch.setattr(nt, "_client_instance", client)
    result = nt.native_completion(
        model="deepseek-chat",
        api_key="k",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )
    assert isinstance(result, NativeStreamResponse)
    assert client.last_payload["stream"] is True
    assert client.last_payload["stream_options"] == {"include_usage": True}


def test_native_completion_passes_reasoning_effort_and_warns_unknown(monkeypatch, caplog):
    """reasoning_effort 等标准参数必须透传；白名单外参数显式告警而非静默丢弃。"""
    import logging

    client = _FakeClient(_FakeHttpResponse(payload=_SYNC_PAYLOAD))
    monkeypatch.setattr(nt, "_client_instance", client)
    nt.native_completion(
        model="deepseek-chat",
        api_key="k",
        messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="high",
        mystery_param={"x": 1},
    )
    assert client.last_payload["reasoning_effort"] == "high"
    assert "mystery_param" in caplog.text


def test_litellm_completion_routes_unsupported_models(monkeypatch):
    """o1/o3、litellm provider 前缀（azure/ 等）、anthropic/ 留在 litellm；
    常规 OpenAI 兼容路由进 native。"""
    from core import llm_transport

    class _FakeLitellm:
        def __init__(self):
            self.calls = []

        def completion(self, **kwargs):
            self.calls.append(kwargs)
            return "LITELLM_RESULT"

    fake_litellm = _FakeLitellm()
    monkeypatch.setattr(llm_transport, "_litellm_module", fake_litellm)
    monkeypatch.setattr(
        nt, "native_completion", lambda **kw: (_ for _ in ()).throw(AssertionError("不应进 native"))
    )

    for model in ("o1-preview", "openai/o3-mini", "anthropic/claude-x", "azure/gpt-4o"):
        result = llm_transport._litellm_completion(model=model, messages=[])
        assert result == "LITELLM_RESULT", model

    # 常规模型进 native（native_completion 已被 mock 为哨兵）
    monkeypatch.setattr(nt, "native_completion", lambda **kw: "NATIVE_RESULT")
    assert llm_transport._litellm_completion(
        model="deepseek-chat", messages=[]
    ) == "NATIVE_RESULT"
    assert llm_transport._litellm_completion(
        model="openai/gpt-4o", messages=[]
    ) == "NATIVE_RESULT"


def test_normalize_usage_derives_total_when_missing():
    """provider 省略 total_tokens 时由 prompt+completion 推导，不记 0。"""
    normalizer = ChunkNormalizer(POLICY_REGISTRY["standard"])
    usage = normalizer.normalize_usage({"prompt_tokens": 30, "completion_tokens": 12})
    assert usage.total_tokens == 42


def test_fast_token_counter_counts_tool_call_arguments():
    """历史消息中的 tool_calls arguments 文本必须计入估算，避免长参数低估。"""
    big_args = "x" * 2000
    with_args = fast_token_counter("m", [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "1", "type": "function", "function": {"name": "f", "arguments": big_args}}],
    }])
    without = fast_token_counter("m", [{"role": "assistant", "content": ""}])
    assert with_args > without + 100
