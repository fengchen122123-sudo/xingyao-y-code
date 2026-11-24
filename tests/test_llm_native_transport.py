import json

import httpcore
import pytest

import core.llm_native_transport as nt
from core.llm_policy import (
    ModelPolicy,
    get_model_policy,
    POLICY_REGISTRY,
    DEFAULT_USER_AGENT,
    TOKENRHYTHM_AGENT_NAME,
)
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
    ContentFilteredError,
)


def test_model_policy_resolution():
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

    assert len(chunk.choices) == 1
    choice = chunk.choices[0]
    assert choice.delta.content == "Result"
    assert choice.delta.reasoning_content == "Step 1: thinking"
    assert choice.finish_reason == "tool_calls"

    assert len(choice.delta.tool_calls) == 1
    tc = choice.delta.tool_calls[0]
    assert tc.index == 0
    assert tc.id == "call_abc"
    assert tc.function.name == "read_file"
    assert tc.function.arguments == '{"path": "a.py"}'

    assert chunk.usage is not None
    assert chunk.usage.prompt_tokens == 100
    assert chunk.usage.prompt_tokens_details["cached_tokens"] == 64
    assert chunk.usage.prompt_cache_hit_tokens == 64


def test_would_block_read_retries_until_data():
    calls = {"n": 0}

    def operation(timeout):
        calls["n"] += 1
        assert timeout is None or timeout > 0
        if calls["n"] < 3:
            err = BlockingIOError(10035, "would block")
            raise httpcore.ReadError(err) from err
        return b"ok"

    result = nt._call_retrying_would_block(operation, timeout=5, sleeper=lambda _s: None)
    assert result == b"ok"
    assert calls["n"] == 3


def test_would_block_does_not_retry_other_socket_errors():
    calls = {"n": 0}

    def operation(timeout):
        calls["n"] += 1
        reset = OSError(10054, "connection reset")
        reset.winerror = 10054
        raise httpcore.ReadError(reset) from reset

    with pytest.raises(httpcore.ReadError):
        nt._call_retrying_would_block(operation, timeout=5, sleeper=lambda _s: None)
    assert calls["n"] == 1


def test_would_block_stops_at_deadline():
    clock = {"t": 0.0}

    def operation(timeout):
        err = BlockingIOError(10035, "would block")
        raise httpcore.ReadError(err) from err

    def sleeper(seconds):
        clock["t"] += seconds

    with pytest.raises(httpcore.ReadError):
        nt._call_retrying_would_block(
            operation,
            timeout=0.05,
            sleeper=sleeper,
            clock=lambda: clock["t"],
            pause_start=0.04,
        )


def test_retry_stream_survives_one_would_block():
    class Sock:
        def __init__(self):
            self.n = 0

        def settimeout(self, timeout):
            self.timeout = timeout

        def recv(self, _n):
            self.n += 1
            if self.n == 1:
                raise BlockingIOError(10035, "would block")
            return b"hello"

    stream = nt._WouldBlockRetryStream(Sock())
    assert stream.read(16, timeout=5) == b"hello"


def test_shared_client_uses_would_block_retry_backend(monkeypatch):
    monkeypatch.setattr(nt, "_client_instance", None)
    client = nt._get_shared_client()
    try:
        backend = client._transport._pool._network_backend
        assert isinstance(backend, nt._WouldBlockRetryBackend)
    finally:
        client.close()
        monkeypatch.setattr(nt, "_client_instance", None)


def test_native_stream_close():
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
        stream.close()
        break

    assert len(chunks) == 1
    assert fake_resp.closed is True
    stream.close()


def test_map_http_errors():
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


def test_gateway_content_filter_maps_to_dedicated_error():
    # 中转网关（new-api 系）命中后台关键词后的真实报文。触发词拼接构造，
    # 避免测试源码本身成为下一次请求的触发源。
    hit_word = "test_" + "sensitive"
    body = (
        '{"error":{"message":"user sensitive words detected: ' + hit_word + '",'
        '"type":"new_api_error","param":"","code":"sensitive_words_detected"}}'
    )

    class FakeHTTPResponse:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text

    err = _map_http_error(FakeHTTPResponse(500, body))
    assert isinstance(err, ContentFilteredError)
    assert not isinstance(err, APIConnectionError)
    assert "内容过滤" in str(err)
    assert hit_word not in str(err)
    assert "***" in str(err)


def test_fast_token_counter():
    messages = [
        {"role": "system", "content": "你是一个资深的 Python 架构师。"},
        {"role": "user", "content": "def hello_world(): return 'hello'"}
    ]
    tokens = fast_token_counter("deepseek-chat", messages)
    assert tokens > 10
    assert isinstance(tokens, int)


def test_native_sync_response():
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
        self.last_headers = None

    def build_request(self, method, url, headers=None, json=None, timeout=None):
        self.last_payload = json
        self.last_url = url
        self.last_headers = headers or {}
        self.last_timeout = timeout
        return object()

    def send(self, req, stream=False):
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


def test_native_completion_consumes_contract_stream_options_without_warning(monkeypatch, caplog):
    import logging

    lines = [
        'data: {"choices": [{"index": 0, "delta": {"content": "hi"}}]}',
        "data: [DONE]",
    ]
    client = _FakeClient(_FakeHttpResponse(lines=lines))
    monkeypatch.setattr(nt, "_client_instance", client)
    with caplog.at_level(logging.WARNING):
        nt.native_completion(
            model="deepseek-chat",
            api_key="k",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            stream_options={"include_usage": True},
        )
    assert client.last_payload["stream_options"] == {"include_usage": True}
    assert "未识别的请求参数" not in caplog.text


def test_dispatch_completion_routes_unsupported_models(monkeypatch):
    import pytest

    from core import llm_transport
    from core.llm_native_transport import NativeLLMError

    monkeypatch.setattr(
        nt, "native_completion", lambda **kw: (_ for _ in ()).throw(AssertionError("不应进 native"))
    )

    for model in ("o1-preview", "openai/o3-mini", "azure/gpt-4o", "gemini/gemini-pro"):
        with pytest.raises(NativeLLMError, match="不在自研引擎支持范围"):
            llm_transport._dispatch_completion(model=model, messages=[])

    monkeypatch.setattr(nt, "native_completion", lambda **kw: "NATIVE_RESULT")
    assert llm_transport._dispatch_completion(
        model="deepseek-chat", messages=[]
    ) == "NATIVE_RESULT"
    assert llm_transport._dispatch_completion(
        model="openai/gpt-4o", messages=[]
    ) == "NATIVE_RESULT"
    assert llm_transport._dispatch_completion(
        model="anthropic/step-3.7-flash", messages=[]
    ) == "NATIVE_RESULT"


def test_normalize_usage_derives_total_when_missing():
    normalizer = ChunkNormalizer(POLICY_REGISTRY["standard"])
    usage = normalizer.normalize_usage({"prompt_tokens": 30, "completion_tokens": 12})
    assert usage.total_tokens == 42


def test_fast_token_counter_counts_tool_call_arguments():
    big_args = "x" * 2000
    with_args = fast_token_counter("m", [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "1", "type": "function", "function": {"name": "f", "arguments": big_args}}],
    }])
    without = fast_token_counter("m", [{"role": "assistant", "content": ""}])
    assert with_args > without + 100


def test_openai_route_adds_tokenrhythm_agent_header():
    policy = get_model_policy("deepseek-chat", api_base="https://tokenrhythm.studio/v1")
    url, headers, _ = nt._resolve_endpoint_and_headers(
        model="deepseek-chat",
        api_key="k",
        api_base="https://tokenrhythm.studio/v1",
        policy=policy,
    )

    assert url == "https://tokenrhythm.studio/v1/chat/completions"
    assert headers["X-TokenRhythm-Agent"] == TOKENRHYTHM_AGENT_NAME


def test_anthropic_route_adds_tokenrhythm_agent_header():
    _url, headers = nt._anthropic_resolve_endpoint(
        api_base="https://tokenrhythm.studio",
        api_key="k",
        extra_headers=None,
    )

    assert headers["X-TokenRhythm-Agent"] == TOKENRHYTHM_AGENT_NAME


def test_non_tokenrhythm_bases_omit_tokenrhythm_agent_header():
    policy = get_model_policy("deepseek-chat")
    _url, headers, _ = nt._resolve_endpoint_and_headers(
        model="deepseek-chat",
        api_key="k",
        api_base="https://api.deepseek.com/v1",
        policy=policy,
    )
    assert "X-TokenRhythm-Agent" not in headers

    _a_url, anthropic_headers = nt._anthropic_resolve_endpoint(
        api_base="https://api.anthropic.com",
        api_key="k",
        extra_headers=None,
    )
    assert "X-TokenRhythm-Agent" not in anthropic_headers


def test_native_completion_sends_tokenrhythm_agent_header(monkeypatch):
    client = _FakeClient(_FakeHttpResponse(payload=_SYNC_PAYLOAD))
    monkeypatch.setattr(nt, "_client_instance", client)

    nt.native_completion(
        model="openai/deepseek-chat",
        api_key="k",
        api_base="https://tokenrhythm.studio/v1",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert client.last_url == "https://tokenrhythm.studio/v1/chat/completions"
    assert client.last_headers["X-TokenRhythm-Agent"] == TOKENRHYTHM_AGENT_NAME


def test_extra_headers_win_over_hardcoded_tokenrhythm_header():
    from core.llm_native_transport import _resolve_endpoint_and_headers
    from core.llm_policy import get_model_policy

    _url, headers, _clean = _resolve_endpoint_and_headers(
        model="openai/deepseek-flash",
        api_key="k",
        api_base="https://tokenrhythm.studio/v1",
        policy=get_model_policy(model_name="openai/deepseek-flash",
                                api_base="https://tokenrhythm.studio/v1"),
        extra_headers={"X-TokenRhythm-Agent": "CustomAgent/1.0"},
    )
    assert headers["X-TokenRhythm-Agent"] == "CustomAgent/1.0"


def test_session_id_header_added_when_thread_session_bound():
    from core import app_context

    old = app_context.get_thread_session()
    app_context.set_thread_session("session_a1b2c3")
    try:
        policy = get_model_policy("deepseek-chat", api_base="https://api.deepseek.com/v1")
        _url, headers, _ = nt._resolve_endpoint_and_headers(
            model="deepseek-chat",
            api_key="k",
            api_base="https://api.deepseek.com/v1",
            policy=policy,
        )
        assert headers["X-Session-Id"] == "session_a1b2c3"

        _a_url, anthropic_headers = nt._anthropic_resolve_endpoint(
            api_base="https://api.anthropic.com",
            api_key="k",
            extra_headers=None,
        )
        assert anthropic_headers["X-Session-Id"] == "session_a1b2c3"
    finally:
        app_context.set_thread_session(old)


def test_session_id_header_percent_encoded_for_non_ascii():
    from urllib.parse import unquote

    from core import app_context

    old = app_context.get_thread_session()
    app_context.set_thread_session("调试会话1")
    try:
        policy = get_model_policy("deepseek-chat", api_base="https://api.deepseek.com/v1")
        _url, headers, _ = nt._resolve_endpoint_and_headers(
            model="deepseek-chat",
            api_key="k",
            api_base="https://api.deepseek.com/v1",
            policy=policy,
        )
        encoded = headers["X-Session-Id"]
        assert encoded == "%E8%B0%83%E8%AF%95%E4%BC%9A%E8%AF%9D1"
        assert encoded.isascii()
        assert unquote(encoded) == "调试会话1"
    finally:
        app_context.set_thread_session(old)


def test_session_id_header_omitted_without_bound_session():
    from core import app_context

    old = app_context.get_thread_session()
    app_context.set_thread_session(None)
    try:
        policy = get_model_policy("deepseek-chat", api_base="https://api.deepseek.com/v1")
        _url, headers, _ = nt._resolve_endpoint_and_headers(
            model="deepseek-chat",
            api_key="k",
            api_base="https://api.deepseek.com/v1",
            policy=policy,
        )
        assert "X-Session-Id" not in headers

        _a_url, anthropic_headers = nt._anthropic_resolve_endpoint(
            api_base="https://api.anthropic.com",
            api_key="k",
            extra_headers=None,
        )
        assert "X-Session-Id" not in anthropic_headers
    finally:
        app_context.set_thread_session(old)


def test_extra_headers_win_over_builtin_session_id_header():
    from core import app_context

    old = app_context.get_thread_session()
    app_context.set_thread_session("session_a1b2c3")
    try:
        policy = get_model_policy("deepseek-chat", api_base="https://api.deepseek.com/v1")
        _url, headers, _ = nt._resolve_endpoint_and_headers(
            model="deepseek-chat",
            api_key="k",
            api_base="https://api.deepseek.com/v1",
            policy=policy,
            extra_headers={"X-Session-Id": "custom-session"},
        )
        assert headers["X-Session-Id"] == "custom-session"
    finally:
        app_context.set_thread_session(old)


def _mark_route_overrides_unconfigured(monkeypatch):
    import core.route_overrides as overrides

    monkeypatch.setattr(overrides, "_load_config_for_overrides", lambda: {})
    overrides._cache["entry"] = None


def test_openai_route_headers_are_byte_golden_without_route_overrides(monkeypatch):
    _mark_route_overrides_unconfigured(monkeypatch)

    url, tokenrhythm_headers, _clean = nt._resolve_endpoint_and_headers(
        model="openai/deepseek-flash",
        api_key="k",
        api_base="https://tokenrhythm.studio/v1",
        policy=get_model_policy("openai/deepseek-flash",
                                api_base="https://tokenrhythm.studio/v1"),
        extra_headers=None,
    )
    assert url == "https://tokenrhythm.studio/v1/chat/completions"
    assert tokenrhythm_headers == {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": "Bearer k",
        "User-Agent": DEFAULT_USER_AGENT,
        "X-TokenRhythm-Agent": TOKENRHYTHM_AGENT_NAME,
    }

    url, plain_headers, _clean = nt._resolve_endpoint_and_headers(
        model="openai/deepseek-flash",
        api_key="k",
        api_base="https://api.deepseek.com/v1",
        policy=get_model_policy("openai/deepseek-flash",
                                api_base="https://api.deepseek.com/v1"),
        extra_headers=None,
    )
    assert url == "https://api.deepseek.com/v1/chat/completions"
    assert plain_headers == {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": "Bearer k",
        "User-Agent": DEFAULT_USER_AGENT,
    }


def test_anthropic_route_headers_are_byte_golden_without_route_overrides(monkeypatch):
    _mark_route_overrides_unconfigured(monkeypatch)

    url, headers = nt._anthropic_resolve_endpoint(
        api_base="https://tokenrhythm.studio",
        api_key="k",
        extra_headers=None,
    )
    assert url == "https://tokenrhythm.studio/v1/messages"
    assert headers == {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "anthropic-version": "2023-06-01",
        "User-Agent": DEFAULT_USER_AGENT,
        "x-api-key": "k",
        "X-TokenRhythm-Agent": TOKENRHYTHM_AGENT_NAME,
    }
