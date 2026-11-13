"""原生 HTTPX 流式传输核单元测试。

验证 SSE 解析、流块归一化、硬中断关闭、Token 快速估算与方言策略映射。
"""

import json
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
    """不传 stream 时必须按非流式处理（completion 契约），
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


def test_native_completion_consumes_contract_stream_options_without_warning(monkeypatch, caplog):
    """调用方契约带入的 stream_options（llm.py 固定形状）必须被显式识别：
    不落入未识别参数告警，最终请求体由 policy 自加等价的 include_usage。"""
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
    """自研引擎路由契约：不支持的路由（o1/o3、azure/ 等厂商云路由）
    显式抛 NativeLLMError（绝无静默兜底）；OpenAI 兼容与 anthropic/
    前缀路由进 native。"""
    import pytest

    from core import llm_transport
    from core.llm_native_transport import NativeLLMError

    monkeypatch.setattr(
        nt, "native_completion", lambda **kw: (_ for _ in ()).throw(AssertionError("不应进 native"))
    )

    for model in ("o1-preview", "openai/o3-mini", "azure/gpt-4o", "gemini/gemini-pro"):
        with pytest.raises(NativeLLMError, match="不在自研引擎支持范围"):
            llm_transport._dispatch_completion(model=model, messages=[])

    # 常规与 anthropic 路由进 native（native_completion 已被 mock 为哨兵）
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


# ---------------------------------------------------------------------------
# 基元律动（tokenrhythm.studio）客户端标识头：X-TokenRhythm-Agent
# ---------------------------------------------------------------------------

def test_openai_route_adds_tokenrhythm_agent_header():
    """OpenAI 兼容路由打到基元律动端点时，请求头必须补 X-TokenRhythm-Agent，
    值是纯客户端标识（不带版本号，版本信息由 User-Agent 承担）。"""
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
    """Anthropic Messages 路由打到基元律动端点时同样补该标识头。"""
    _url, headers = nt._anthropic_resolve_endpoint(
        api_base="https://tokenrhythm.studio",
        api_key="k",
        extra_headers=None,
    )

    assert headers["X-TokenRhythm-Agent"] == TOKENRHYTHM_AGENT_NAME


def test_non_tokenrhythm_bases_omit_tokenrhythm_agent_header():
    """非基元律动端点（OpenAI 兼容与 Anthropic 两条路由）不得携带该头。"""
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
    """端到端：经基元律动 API 基址的完整请求，实际发出的请求头包含该标识。"""
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


# tests/test_llm_native_transport.py（守护现有合并顺序，不改代码）
def test_extra_headers_win_over_hardcoded_tokenrhythm_header():
    """配置的 X-TokenRhythm-Agent 必须压过硬编码值——由现有合并顺序保证。"""
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


# ---------------------------------------------------------------------------
# 会话关联头：X-Session-Id（本地会话与服务端调用日志关联）
# ---------------------------------------------------------------------------

def test_session_id_header_added_when_thread_session_bound():
    """绑定执行会话时，两条路由的请求头都带上 X-Session-Id。"""
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
    """中文会话名 percent-encode 成纯 ASCII 头值，服务端 URL-decode 可还原。"""
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
    """无会话上下文（单测 / headless）不携带该头，保证既有请求字节不变。"""
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
    """配置的 extra_headers 可覆盖内置 X-Session-Id——由现有合并顺序保证。"""
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


# ---------------------------------------------------------------------------
# 渠道请求形态覆盖零影响（V2）：未配置 route_request_overrides 时请求头逐字节不变
# ---------------------------------------------------------------------------

def _mark_route_overrides_unconfigured(monkeypatch):
    """把渠道请求形态覆盖置为「未配置」状态。

    monkeypatch + _cache 置 None 的手法同 tests/test_route_overrides.py 的 _fake_config：
    配置指纹只反映 loader 身份 + 配置文件 mtime，替换 loader 后必须手动失效缓存，
    否则 10 秒 TTL 内可能读到上一组配置。
    """
    import core.route_overrides as overrides

    monkeypatch.setattr(overrides, "_load_config_for_overrides", lambda: {})
    overrides._cache["entry"] = None


def test_openai_route_headers_are_byte_golden_without_route_overrides(monkeypatch):
    """未配置 route_request_overrides 时，OpenAI 兼容路由 headers 与改动前逐键全等。

    渠道覆盖只经 _provider_request_options 的 extra_headers/extra_body 注入，
    传输层零改动；本用例断言两条基线请求头快照（基元律动 / 普通网关）逐字节一致，
    任何键的增删或取值变化都会挂。
    """
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
    """未配置 route_request_overrides 时，Anthropic Messages 路由 headers 逐键全等。"""
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
