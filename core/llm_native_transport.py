"""Y-code 原生 HTTPX 同步流式传输核与解析引擎。

摒弃 LiteLLM，采用原生 httpx.Client 连接池与 SSEDecoder 解析器，
实现零黑盒丢参、100% 前缀字节锁定、微秒级 Token 估算与物理级硬中断。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx

from core.llm_policy import (
    ModelPolicy,
    get_model_policy,
    DEFAULT_USER_AGENT,
    tokenrhythm_agent_headers,
    session_id_headers,
)

logger = logging.getLogger(__name__)


# ==============================================================================
# 1. 统一异常体系 (标准错误类，与系统错误映射器无缝对接)
# ==============================================================================

class NativeLLMError(Exception):
    """原生 LLM 通信基础异常。"""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response: Optional[Any] = None,
        body: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response = response
        self.body = body


class RateLimitError(NativeLLMError):
    """触发频率限制或配额耗尽 (HTTP 429)。"""
    pass


class ContextWindowExceededError(NativeLLMError):
    """模型上下文窗口溢出 (HTTP 400/413)。"""
    pass


class AuthenticationError(NativeLLMError):
    """API Key 缺失、未授权或过期 (HTTP 401/403)。"""
    pass


class APIConnectionError(NativeLLMError):
    """上游服务故障或连接失败 (HTTP 5xx / 网络超时)。"""
    pass


class APIError(NativeLLMError):
    """其他通用 API 异常。"""
    pass


# ==============================================================================
# 2. 轻量级流数据对象 (兼容 StreamAssembler 的属性访问与字典提取)
# ==============================================================================

class ToolCallFunction:
    __slots__ = ("name", "arguments")

    def __init__(self, name: str = "", arguments: str = ""):
        self.name = name
        self.arguments = arguments

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class ChunkToolCall:
    __slots__ = ("index", "id", "type", "function")

    def __init__(
        self,
        index: int = 0,
        id: Optional[str] = None,
        type: str = "function",
        function: Optional[ToolCallFunction] = None,
    ):
        self.index = index
        self.id = id
        self.type = type
        self.function = function or ToolCallFunction()

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class ChunkDelta:
    __slots__ = ("content", "reasoning_content", "tool_calls")

    def __init__(
        self,
        content: str = "",
        reasoning_content: str = "",
        tool_calls: Optional[List[ChunkToolCall]] = None,
    ):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls or []

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class _ChoiceMessage:
    """非流式响应的 message 形状（litellm ModelResponse.choices[0].message 兼容）。

    部分调用方（视觉副模型/记忆压缩/Kairos）按 .message.content 消费非流式
    响应，与流式 delta 形状并存，二者字段同源。
    """

    __slots__ = ("role", "content", "reasoning_content", "tool_calls")

    def __init__(
        self,
        content: str = "",
        reasoning_content: str = "",
        tool_calls: Optional[List[ChunkToolCall]] = None,
        role: str = "assistant",
    ):
        self.role = role
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls or []

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class ChunkChoice:
    __slots__ = ("index", "delta", "finish_reason", "message")

    def __init__(
        self,
        delta: ChunkDelta,
        finish_reason: Optional[str] = None,
        index: int = 0,
        message: Optional[_ChoiceMessage] = None,
    ):
        self.delta = delta
        self.finish_reason = finish_reason
        self.index = index
        # 流式 chunk 无 message 形状；仅 NativeSyncResponse 填充
        self.message = message

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class ChunkUsage:
    __slots__ = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_tokens_details",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    )

    def __init__(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        prompt_tokens_details: Optional[Dict[str, Any]] = None,
        prompt_cache_hit_tokens: Optional[int] = None,
        prompt_cache_miss_tokens: Optional[int] = None,
    ):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.prompt_tokens_details = prompt_tokens_details
        self.prompt_cache_hit_tokens = prompt_cache_hit_tokens
        self.prompt_cache_miss_tokens = prompt_cache_miss_tokens

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class StreamChunk:
    __slots__ = ("id", "model", "choices", "usage")

    def __init__(
        self,
        choices: Optional[List[ChunkChoice]] = None,
        usage: Optional[ChunkUsage] = None,
        id: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.choices = choices or []
        self.usage = usage
        self.id = id
        self.model = model

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


# ==============================================================================
# 3. 原生 SSE 解析器与流数据归一化
# ==============================================================================

class SSEDecoder:
    """标准 Server-Sent Events (SSE) 流式解析器。"""

    @staticmethod
    def iter_events(line_iterator: Iterator[str]) -> Iterator[dict]:
        """从 HTTP 文本行迭代器中提取标准 JSON 数据包。"""
        for line in line_iterator:
            line = line.strip()
            if not line or line.startswith(":"):
                # 忽略心跳注释与空行
                continue
            if line.startswith("data:"):
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    # 容忍偶发的非标准分段
                    continue


class ChunkNormalizer:
    """将厂商原始 SSE JSON 块归一化为标准的 StreamChunk 结构。"""

    def __init__(self, policy: ModelPolicy):
        self.policy = policy

    def normalize_usage(self, raw_usage: Any) -> Optional[ChunkUsage]:
        """归一化 usage 字段，完整提取 prompt_tokens_details.cached_tokens。"""
        if not isinstance(raw_usage, dict):
            return None
        details = raw_usage.get("prompt_tokens_details")
        cached_tokens = None
        if isinstance(details, dict):
            cached_tokens = details.get("cached_tokens")
        if cached_tokens is None:
            cached_tokens = raw_usage.get("prompt_cache_hit_tokens")

        prompt_tokens = int(raw_usage.get("prompt_tokens") or 0)
        completion_tokens = int(raw_usage.get("completion_tokens") or 0)
        # total_tokens 缺省时由 prompt+completion 推导，不算缺失，
        # 避免部分 provider 省略该字段时记账 total=0。
        total_tokens = raw_usage.get("total_tokens")
        if total_tokens is None:
            total_tokens = prompt_tokens + completion_tokens

        return ChunkUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=int(total_tokens or 0),
            prompt_tokens_details={"cached_tokens": cached_tokens} if cached_tokens is not None else details,
            prompt_cache_hit_tokens=cached_tokens,
            prompt_cache_miss_tokens=raw_usage.get("prompt_cache_miss_tokens"),
        )

    def normalize(self, raw: dict) -> Optional[StreamChunk]:
        if not isinstance(raw, dict):
            return None

        # 1. 解析 Usage（重点支持 DeepSeek/Kimi 原生缓存明细）
        usage_obj = self.normalize_usage(raw.get("usage"))

        # 2. 解析 Choices
        raw_choices = raw.get("choices") or []
        choices_list: List[ChunkChoice] = []

        for idx, choice in enumerate(raw_choices):
            if not isinstance(choice, dict):
                continue
            delta_dict = choice.get("delta") or {}

            # 正文内容
            content = delta_dict.get("content") or ""

            # 思维链抽取 (按 policy.reasoning_field 优先，其余字段兜底)
            reasoning = (
                delta_dict.get(self.policy.reasoning_field)
                or delta_dict.get("reasoning_content")
                or delta_dict.get("reasoning")
                or delta_dict.get("thought")
                or ""
            )

            # 工具调用增量
            tool_calls_list: List[ChunkToolCall] = []
            for tc in delta_dict.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn_dict = tc.get("function") or {}
                fn_obj = ToolCallFunction(
                    name=str(fn_dict.get("name") or ""),
                    arguments=str(fn_dict.get("arguments") or ""),
                )
                tool_calls_list.append(
                    ChunkToolCall(
                        index=int(tc.get("index") or 0),
                        id=tc.get("id"),
                        type=tc.get("type") or "function",
                        function=fn_obj,
                    )
                )

            choices_list.append(
                ChunkChoice(
                    delta=ChunkDelta(
                        content=content,
                        reasoning_content=reasoning,
                        tool_calls=tool_calls_list,
                    ),
                    finish_reason=choice.get("finish_reason"),
                    index=int(choice.get("index", idx)),
                )
            )

        return StreamChunk(
            choices=choices_list,
            usage=usage_obj,
            id=raw.get("id"),
            model=raw.get("model"),
        )

    def finalize_sync(self, raw: dict) -> "NativeSyncResponse":
        """非流式响应收口：OpenAI 兼容形状原样包装。"""
        return NativeSyncResponse(raw, self)


class NativeStreamResponse:
    """包装原生 HTTP 响应，支持同步流式消费与毫秒级硬中断关闭。"""

    def __init__(self, response: httpx.Response, normalizer: ChunkNormalizer):
        self._response = response
        self._normalizer = normalizer
        self._closed = False
        self._lock = threading.Lock()
        # 记录收到的所有 chunks，以支持 core.llm 从尾部抓取最终 usage
        self.chunks: List[StreamChunk] = []

    def __iter__(self) -> Iterator[StreamChunk]:
        try:
            for raw_dict in SSEDecoder.iter_events(self._response.iter_lines()):
                chunk = self._normalizer.normalize(raw_dict)
                if chunk is not None:
                    self.chunks.append(chunk)
                    yield chunk
        finally:
            self.close()

    def close(self) -> None:
        """物理级硬中断：立即掐断底层 TCP 套接字连接。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._response.close()
        except Exception:
            pass


class NativeSyncResponse:
    """包装原生 HTTP 非流式响应对象，提供 .usage 与 .choices 兼容访问。"""

    def __init__(self, raw: dict, normalizer: ChunkNormalizer):
        self.raw = raw
        self.usage = normalizer.normalize_usage(raw.get("usage"))
        self.id = raw.get("id")
        self.model = raw.get("model")

        choices_list: List[ChunkChoice] = []
        for idx, choice in enumerate(raw.get("choices") or []):
            if not isinstance(choice, dict):
                continue
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
            reasoning = (
                msg.get(normalizer.policy.reasoning_field)
                or msg.get("reasoning_content")
                or msg.get("reasoning")
                or msg.get("thought")
                or ""
            )
            tool_calls_list: List[ChunkToolCall] = []
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                tool_calls_list.append(
                    ChunkToolCall(
                        index=int(tc.get("index") or 0),
                        id=tc.get("id"),
                        type=tc.get("type") or "function",
                        function=ToolCallFunction(
                            name=str(fn.get("name") or ""),
                            arguments=str(fn.get("arguments") or ""),
                        ),
                    )
                )
            choices_list.append(
                ChunkChoice(
                    delta=ChunkDelta(
                        content=content,
                        reasoning_content=reasoning,
                        tool_calls=tool_calls_list,
                    ),
                    finish_reason=choice.get("finish_reason"),
                    index=int(choice.get("index", idx)),
                    message=_ChoiceMessage(
                        content=content,
                        reasoning_content=reasoning,
                        tool_calls=tool_calls_list,
                        role=str(msg.get("role") or "assistant"),
                    ),
                )
            )
        self.choices = choices_list

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


# ==============================================================================
# 4. HTTP 连接池管理与请求派发
# ==============================================================================

_client_instance: Optional[httpx.Client] = None
_client_lock = threading.Lock()


def _get_shared_client() -> httpx.Client:
    """获取/初始化共享的高性能 httpx 同步长连接池。"""
    global _client_instance
    if _client_instance is not None:
        return _client_instance
    with _client_lock:
        if _client_instance is None:
            limits = httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=60.0,
            )
            # 客户端级兜底 read=None：主路径请求都会带请求级 timeout_arg
            # （流式 read 有界 600s，见 native_completion），此默认仅覆盖
            # 未指定 timeout 的边缘调用。
            timeout = httpx.Timeout(connect=15.0, read=None, write=30.0, pool=10.0)
            _client_instance = httpx.Client(limits=limits, timeout=timeout)
        return _client_instance


def _dispatch_request(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    is_stream: bool,
    timeout_arg: Optional[httpx.Timeout],
    normalizer: Any,
):
    """公共请求派发：发出 POST、映射 HTTP 错误，按流/非流返回统一响应对象。

    OpenAI 兼容与 Anthropic Messages 两条协议路由共用本段——请求级超时必须经
    build_request 的 timeout 参数写入 request.extensions（httpx 官方按请求
    超时通道）：Client.send() 的 timeout 形参在 httpx 0.28 已被移除，传入即
    TypeError，会让每个请求都在发出前崩溃（历史 bug：静默降级 litellm）。
    """
    client = _get_shared_client()
    req = client.build_request(
        "POST", url, headers=headers, json=payload, timeout=timeout_arg
    )
    try:
        resp = client.send(req, stream=is_stream)
    except httpx.RequestError as exc:
        raise APIConnectionError(f"无法建立与模型服务端的连接: {exc}") from exc

    if resp.status_code != 200:
        try:
            resp.read()
        except httpx.HTTPError as exc:
            # 错误体读取失败必须包成 NativeLLMError：裸 httpx 异常冒泡出去
            # 会被回合循环按未知错误收口，丢失可诊断的结构化信息。
            raise APIConnectionError(f"错误响应体读取失败: {exc}") from exc
        finally:
            resp.close()
        raise _map_http_error(resp)

    if is_stream:
        return NativeStreamResponse(resp, normalizer)
    try:
        raw_json = resp.json()
    except httpx.HTTPError as exc:
        # 网络层读失败不换通道重发（同请求双发风险），直接按连接错误上抛
        raise APIConnectionError(f"非流式响应体读取失败: {exc}") from exc
    return normalizer.finalize_sync(raw_json)


def _resolve_endpoint_and_headers(
    model: str,
    api_key: Optional[str],
    api_base: Optional[str],
    policy: ModelPolicy,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[str, Dict[str, str], str]:
    """解析出最终的 URL、请求头与实际模型名称。"""
    # 提取实际模型名称（去除 provider 前缀，如 deepseek/deepseek-chat -> deepseek-chat）
    clean_model = model
    if "/" in model:
        prefix, remainder = model.split("/", 1)
        # 若前缀是 provider 标识而非模型名的一部分，予以剥离
        if prefix.lower() in ("deepseek", "moonshot", "zhipu", "openai", "dashscope", "minimax"):
            clean_model = remainder

    # 确定请求 URL
    if api_base:
        base = api_base.rstrip("/")
        if base.endswith("/chat/completions"):
            url = base
        else:
            url = f"{base}/chat/completions"
    else:
        # 按方言默认官方端点
        kind = policy.provider_kind
        if kind == "deepseek":
            url = "https://api.deepseek.com/chat/completions"
        elif kind == "moonshot":
            url = "https://api.moonshot.cn/v1/chat/completions"
        elif kind == "zhipu":
            url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        elif kind == "dashscope":
            url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        elif kind == "minimax":
            # 兜底 URL 用官方现行域名（与 model_catalog 预置一致；旧域名
            # api.minimax.chat 仍可被 llm_policy 识别，但不再作为默认）
            url = "https://api.minimaxi.com/v1/text/chatcompletion_v2"
        else:
            url = "https://api.openai.com/v1/chat/completions"

    # 组装请求头
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    # 从参数或环境变量提取 key
    effective_key = api_key
    if not effective_key:
        env_keys = {
            "deepseek": "DEEPSEEK_API_KEY",
            "moonshot": "MOONSHOT_API_KEY",
            "zhipu": "ZHIPU_API_KEY",
            "dashscope": "DASHSCOPE_API_KEY",
            "minimax": "MINIMAX_API_KEY",
            "openai": "OPENAI_API_KEY",
        }
        env_var = env_keys.get(policy.provider_kind, "OPENAI_API_KEY")
        effective_key = os.environ.get(env_var)

    if effective_key:
        headers["Authorization"] = f"Bearer {effective_key}"

    # 合并方言默认头、基元律动标识头、会话关联头与外部追加头
    headers.update(policy.default_headers)
    headers.update(tokenrhythm_agent_headers(url))
    headers.update(session_id_headers())
    if extra_headers:
        headers.update(extra_headers)

    return url, headers, clean_model


def _map_http_error(resp: httpx.Response) -> NativeLLMError:
    """将 HTTP 错误状态码及响应体映射为标准系统异常。"""
    code = resp.status_code
    body = resp.text
    clean_body = body[:500] if body else ""

    if code == 401 or code == 403:
        return AuthenticationError(
            f"API 凭证无效或未授权 (HTTP {code}): {clean_body}",
            status_code=code,
            response=resp,
            body=body,
        )
    if code == 429:
        return RateLimitError(
            f"触发频率限制或余额耗尽 (HTTP 429): {clean_body}",
            status_code=code,
            response=resp,
            body=body,
        )
    if code in (400, 413):
        lower_body = clean_body.lower()
        if any(k in lower_body for k in ("context", "maximum context", "token limit", "too long")):
            return ContextWindowExceededError(
                f"模型上下文窗口超限 (HTTP {code}): {clean_body}",
                status_code=code,
                response=resp,
                body=body,
            )
    if code >= 500:
        return APIConnectionError(
            f"模型服务提供商内部故障 (HTTP {code}): {clean_body}",
            status_code=code,
            response=resp,
            body=body,
        )

    return APIError(
        f"LLM 请求失败 (HTTP {code}): {clean_body}",
        status_code=code,
        response=resp,
        body=body,
    )


# ==============================================================================
# 5. 核心入口: native_completion
# ==============================================================================

# native 载荷构建直接消费的调用方参数（含 litellm 元参数与传输控制参数）
_NATIVE_KNOWN_KWARGS = frozenset({
    "model", "api_key", "api_base", "messages", "stream",
    "temperature", "tools", "tool_choice",
    "max_tokens", "max_output_tokens", "max_completion_tokens",
    "extra_body", "extra_headers", "timeout",
    "num_retries", "allowed_openai_params", "metadata",
    "stream_options",
})

# OpenAI 兼容标准参数：存在即透传到请求体顶层
_NATIVE_PASSTHROUGH_KEYS = frozenset({
    "reasoning_effort", "output_config", "thinking",
    "top_p", "presence_penalty", "frequency_penalty",
    "seed", "stop", "n", "logit_bias", "user",
    "response_format", "parallel_tool_calls",
})


def native_completion(**kwargs) -> NativeStreamResponse:
    """原生流式请求主入口（OpenAI 兼容 + Anthropic Messages 双协议）。

    替代 litellm.completion，保证 100% 原始字段透传与零黑盒干扰；
    anthropic/ 前缀路由由 _anthropic_completion 翻译为 /v1/messages 协议。
    """
    model = kwargs.get("model") or ""
    if _is_anthropic_route(model):
        return _anthropic_completion(**kwargs)
    api_key = kwargs.get("api_key")
    api_base = kwargs.get("api_base")
    policy = get_model_policy(model_name=model, api_base=api_base)

    url, headers, clean_model = _resolve_endpoint_and_headers(
        model=model,
        api_key=api_key,
        api_base=api_base,
        policy=policy,
        extra_headers=kwargs.get("extra_headers"),
    )

    # litellm 专属元参数：native 侧无对应语义，显式消费避免落进未知参数告警。
    # - num_retries：请求内重试由上层重试循环承担，native 不做；
    # - allowed_openai_params：litellm 客户端参数校验白名单，native 全量透传无需校验；
    # - metadata：litellm pass-through（Kimi Anthropic 路由亲和 user_id），
    #   OpenAI 兼容路由的缓存亲和材料走 extra_body，已在下方合并。
    for litellm_meta_key in ("num_retries", "allowed_openai_params", "metadata"):
        kwargs.pop(litellm_meta_key, None)

    # stream_options：流式 usage 统计由下方 policy.supports_stream_options
    # 统一决定并自加（与调用方契约值等价），显式消费避免落进未知参数告警。
    kwargs.pop("stream_options", None)

    # 与 litellm.completion 契约一致：调用方不传 stream 时为非流式请求，
    # 返回完整 NativeSyncResponse（stream=True 仅由主聊天路径显式传入）。
    is_stream = bool(kwargs.get("stream", False))
    # 构建发送载荷 (严格保真)
    payload: Dict[str, Any] = {
        "model": clean_model,
        "messages": kwargs.get("messages") or [],
        "stream": is_stream,
    }

    # 流式使用量统计 (DeepSeek/Kimi 等前缀缓存必需)
    if is_stream and policy.supports_stream_options:
        payload["stream_options"] = {"include_usage": True}

    # 温度
    if policy.supports_temperature and kwargs.get("temperature") is not None:
        payload["temperature"] = kwargs.get("temperature")

    # 工具参数
    if kwargs.get("tools"):
        payload["tools"] = kwargs["tools"]
        if kwargs.get("tool_choice"):
            payload["tool_choice"] = kwargs["tool_choice"]
        else:
            payload["tool_choice"] = "auto"

    # Token 限制：max_completion_tokens 优先（OpenAI 新式参数，二者互斥）
    max_completion_tokens = kwargs.get("max_completion_tokens")
    if max_completion_tokens is not None:
        payload["max_completion_tokens"] = max_completion_tokens
    else:
        max_tok = kwargs.get("max_tokens") or kwargs.get("max_output_tokens")
        if max_tok is not None:
            payload["max_tokens"] = max_tok

    # OpenAI 兼容标准参数透传（litellm 旧路径同样全量转发）；
    # 缺省不发送，保持与不传参时一致的请求字节。
    for passthrough_key in (
        "reasoning_effort", "output_config", "thinking",
        "top_p", "presence_penalty", "frequency_penalty",
        "seed", "stop", "n", "logit_bias", "user",
        "response_format", "parallel_tool_calls",
    ):
        value = kwargs.get(passthrough_key)
        if value is not None:
            payload[passthrough_key] = value

    # 未识别的顶层参数显式告警：静默丢参会让推理强度、缓存亲和等语义悄悄失效
    known_keys = _NATIVE_KNOWN_KWARGS | _NATIVE_PASSTHROUGH_KEYS
    unknown_keys = sorted(set(kwargs) - known_keys)
    if unknown_keys:
        logger.warning(
            "native 传输忽略未识别的请求参数（不会发送到服务端）: %s",
            ", ".join(unknown_keys),
        )

    # 合并 extra_body (如 thinking、缓存亲和等扩展参数)
    payload.update(policy.default_extra_body)
    extra_body = kwargs.get("extra_body")
    if isinstance(extra_body, dict):
        payload.update(extra_body)

    req_timeout = kwargs.get("timeout")
    timeout_arg = None
    if req_timeout is not None:
        timeout_arg = httpx.Timeout(
            connect=min(15.0, float(req_timeout)),
            # 流式 read 不设 None：httpx 的 read 超时同时覆盖“等待响应头”与
            # “chunk 间隔”，必须有界才能保证首字节 give-up 后请求线程最终
            # 自愈退出，不永久滞留连接池名额。600s 与上层看门狗
            # REQUEST_WAIT_GIVE_UP_SECONDS 对齐——看门狗先/同时触发，
            # 正常长思考流不受影响。
            read=600.0 if is_stream else float(req_timeout),
            write=30.0,
            pool=10.0,
        )

    return _dispatch_request(
        url, headers, payload, is_stream, timeout_arg, ChunkNormalizer(policy)
    )


# ==============================================================================
# 6. 微秒级快速 Token 估算器 (替代 litellm.token_counter)
# ==============================================================================

_RE_CHINESE = re.compile(r"[\u4e00-\u9fff]")


def fast_token_counter(model: str, messages: Any) -> int:
    """微秒级启发式 Token 估算器。

    根据中英文字符比率估算，单次计算仅耗时 ~0.05ms，摆脱 litellm.token_counter
    的查表卡顿。中文按 ~0.75 tokens/字符（约 1.33 字符/token）换算，
    英文/代码/符号按 ~0.28 tokens/字符（约 3.5 字符/token）换算。
    工具调用增量与历史消息的 arguments 文本一并计入，避免长参数低估。
    """
    if not messages:
        return 0

    if isinstance(messages, str):
        text_samples = [messages]
    elif isinstance(messages, list):
        text_samples = []
        for msg in messages:
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str):
                    text_samples.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_samples.append(item.get("text") or "")
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        fn = tc.get("function") or {}
                        text_samples.append(str(fn.get("arguments") or ""))
            elif isinstance(msg, str):
                text_samples.append(msg)
    else:
        text_samples = [str(messages)]

    total_tokens = len(messages) * 4 if isinstance(messages, list) else 4

    for text in text_samples:
        if not text:
            continue
        total_len = len(text)
        cn_chars = len(_RE_CHINESE.findall(text))
        en_chars = total_len - cn_chars
        # 中文 ~0.75 tokens/字符；英文/代码 ~0.28 tokens/字符
        tokens = int(cn_chars * 0.75 + en_chars * 0.28)
        total_tokens += max(1, tokens)

    return max(1, total_tokens)


# ==============================================================================
# 7. Anthropic Messages 协议适配（/v1/messages，anthropic/ 前缀路由）
# ==============================================================================
# 覆盖 anthropic 兼容端点（阶跃 step_plan、api.kimi.com/coding 等）。
# 请求侧把 OpenAI chat 形状（messages/tools/tool_calls/reasoning_content）
# 翻译为 Messages API 形状；响应侧把 SSE 事件流/完整 JSON 归一化回
# StreamChunk / NativeSyncResponse，供上层零改动消费。

_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_DEFAULT_MAX_TOKENS = 4096

# stop_reason → OpenAI finish_reason
_ANTHROPIC_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "stop",
    "pause_turn": "stop",
}

# 流内 error 事件 type → 异常类
_ANTHROPIC_STREAM_ERROR_MAP = {
    "rate_limit_error": RateLimitError,
    "authentication_error": AuthenticationError,
    "permission_error": AuthenticationError,
    "invalid_request_error": APIError,
    "request_too_large": APIError,
    "overloaded_error": APIConnectionError,
    "api_error": APIConnectionError,
    "timeout_error": APIConnectionError,
}


def _is_anthropic_route(model: str) -> bool:
    """判断模型是否走 Anthropic Messages 协议（litellm 时代的 anthropic/ 前缀约定）。"""
    return str(model or "").strip().lower().startswith("anthropic/")


def _anthropic_resolve_endpoint(
    api_base: Optional[str],
    api_key: Optional[str],
    extra_headers: Optional[Dict[str, str]],
) -> Tuple[str, Dict[str, str]]:
    """解析 /v1/messages 端点与请求头（x-api-key + anthropic-version）。"""
    base = (api_base or "https://api.anthropic.com").rstrip("/")
    if base.endswith("/v1/messages"):
        url = base
    elif base.endswith("/messages"):
        url = base
    else:
        url = f"{base}/v1/messages"

    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "anthropic-version": _ANTHROPIC_VERSION,
        "User-Agent": DEFAULT_USER_AGENT,
    }
    effective_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if effective_key:
        headers["x-api-key"] = effective_key
    headers.update(tokenrhythm_agent_headers(url))
    headers.update(session_id_headers())
    if extra_headers:
        headers.update(extra_headers)
    return url, headers


def _anthropic_image_block(url: str) -> Optional[Dict[str, Any]]:
    """OpenAI image_url → Anthropic image 块（base64 data URI 与 http URL 两种来源）。"""
    url = str(url or "")
    if url.startswith("data:"):
        head, _, b64 = url.partition(",")
        media_type = head[5:].split(";", 1)[0] or "image/png"
        if not b64:
            return None
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    if url:
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _anthropic_content_blocks(content: Any) -> List[Dict[str, Any]]:
    """OpenAI content（str 或多模态块列表）→ Anthropic 内容块列表。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: List[Dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in ("text", "input_text"):
            block: Dict[str, Any] = {"type": "text", "text": item.get("text") or ""}
            if item.get("cache_control"):
                block["cache_control"] = item["cache_control"]
            blocks.append(block)
        elif item_type == "image_url":
            image_block = _anthropic_image_block((item.get("image_url") or {}).get("url"))
            if image_block is not None:
                blocks.append(image_block)
        elif item_type == "image":
            blocks.append(item)  # 已是 Anthropic 形状，原样保留
    return blocks


def _anthropic_translate_messages(messages: Any) -> Tuple[Optional[Any], List[Dict[str, Any]]]:
    """OpenAI chat messages → (system, anthropic messages)。

    - system 消息提取为顶层 system（纯文本时用字符串，带 cache_control 时用块列表）
    - role=tool 翻译为 tool_result 块；连续 tool 结果合并进单条 user 消息，
      满足 Anthropic 的角色交替约束
    - assistant.tool_calls → tool_use 块（arguments 解析为 JSON 对象）
    - assistant.reasoning_content → thinking 块（anthropic 路由历史保真，
      对应 llm_dialects 对 anthropic 前缀的 keep_thinking_blocks 语义）
    - 消息级 cache_control（litellm 时代由其翻译）挂到该消息最后一个内容块
    """
    system_blocks: List[Dict[str, Any]] = []
    out: List[Dict[str, Any]] = []

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        cache_ctrl = msg.get("cache_control")

        if role == "system":
            if isinstance(content, list):
                blocks = _anthropic_content_blocks(content)
            elif content:
                blocks = [{"type": "text", "text": str(content)}]
            else:
                blocks = []
            for block in blocks:
                if cache_ctrl and "cache_control" not in block:
                    block["cache_control"] = cache_ctrl
            system_blocks.extend(blocks)
            continue

        if role == "tool":
            if isinstance(content, list):
                result_content: Any = _anthropic_content_blocks(content)
            else:
                result_content = str(content or "")
            tool_result = {
                "type": "tool_result",
                "tool_use_id": str(msg.get("tool_call_id") or ""),
                "content": result_content,
            }
            # 连续 tool 结果并入同一条 user 消息（协议要求角色交替）
            prev = out[-1] if out else None
            if (
                isinstance(prev, dict)
                and prev.get("role") == "user"
                and isinstance(prev.get("content"), list)
                and any(b.get("type") == "tool_result" for b in prev["content"])
            ):
                prev["content"].append(tool_result)
            else:
                out.append({"role": "user", "content": [tool_result]})
            continue

        if role == "assistant":
            blocks: List[Dict[str, Any]] = []
            reasoning = msg.get("reasoning_content")
            if reasoning:
                blocks.append({"type": "thinking", "thinking": str(reasoning)})
            if isinstance(content, list):
                blocks.extend(_anthropic_content_blocks(content))
            elif content:
                blocks.append({"type": "text", "text": str(content)})
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                try:
                    tool_input = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    tool_input = {}
                if not isinstance(tool_input, dict):
                    tool_input = {"value": tool_input}
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "input": tool_input,
                })
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            if cache_ctrl:
                blocks[-1]["cache_control"] = cache_ctrl
            out.append({"role": "assistant", "content": blocks})
            continue

        # user（含多模态）及其他角色
        if isinstance(content, list):
            blocks = _anthropic_content_blocks(content)
        elif content:
            blocks = [{"type": "text", "text": str(content)}]
        else:
            blocks = []
        if not blocks:
            blocks = [{"type": "text", "text": ""}]
        if cache_ctrl:
            blocks[-1]["cache_control"] = cache_ctrl
        # 相邻 user 消息并入前一条：OpenAI 序列 assistant(tool_calls) → tool(...)
        # → user(编排提醒/follow-up 注入) 翻译后会出现 user(tool_result) 与
        # user(text) 相邻，Anthropic 严格角色交替校验直接 400
        # "roles must alternate"，故文本块追加进前一条 user 的块列表
        # （tool_result 在前、文本在后）。
        prev = out[-1] if out else None
        if (
            isinstance(prev, dict)
            and prev.get("role") == "user"
            and isinstance(prev.get("content"), list)
        ):
            prev["content"].extend(blocks)
        else:
            out.append({"role": "user", "content": blocks})

    system: Optional[Any] = None
    if system_blocks:
        if len(system_blocks) == 1 and "cache_control" not in system_blocks[0]:
            system = system_blocks[0]["text"]
        else:
            system = system_blocks
    return system, out


def _anthropic_translate_tools(tools: Any) -> List[Dict[str, Any]]:
    """OpenAI function tools → Anthropic tools（input_schema 形状）。"""
    anthropic_tools: List[Dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        schema = fn.get("parameters")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        anthropic_tools.append({
            "name": str(fn.get("name")),
            "description": str(fn.get("description") or ""),
            "input_schema": schema,
        })
    return anthropic_tools


def _anthropic_translate_tool_choice(tool_choice: Any) -> Optional[Dict[str, Any]]:
    """OpenAI tool_choice → Anthropic tool_choice（{"type": auto|any|tool}）。"""
    if tool_choice is None or tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "required" or tool_choice == "any":
        return {"type": "any"}
    if tool_choice == "none":
        return None
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        if fn.get("name"):
            return {"type": "tool", "name": str(fn["name"])}
    return {"type": "auto"}


def _anthropic_usage_to_openai(usage_raw: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic usage → OpenAI usage 字典（缓存读命中映射到 hit 字段）。

    Anthropic 的 input_tokens 不含缓存部分，prompt_tokens 按
    input + cache_read + cache_creation 汇总，保证界面输入计数与 DeepSeek 语义一致。
    """
    input_tokens = int(usage_raw.get("input_tokens") or 0)
    cache_read = int(usage_raw.get("cache_read_input_tokens") or 0)
    cache_creation = int(usage_raw.get("cache_creation_input_tokens") or 0)
    output_tokens = int(usage_raw.get("output_tokens") or 0)
    prompt_tokens = input_tokens + cache_read + cache_creation
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": prompt_tokens + output_tokens,
        "prompt_tokens_details": {"cached_tokens": cache_read} if cache_read else None,
        "prompt_cache_hit_tokens": cache_read or None,
        "prompt_cache_miss_tokens": input_tokens or None,
        # Task 7（跨渠道缓存命中率修复）：写入量单独暴露给记账层，供读写分列；
        # 未报告 → None。上面 prompt_tokens 的汇总口径保持不变。
        "cache_creation_input_tokens": cache_creation or None,
    }


def _anthropic_sync_to_openai(raw: dict) -> dict:
    """Anthropic 完整响应 JSON → OpenAI chat completion 形状字典。"""
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in raw.get("content") or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            content_parts.append(block.get("text") or "")
        elif block_type == "thinking":
            reasoning_parts.append(block.get("thinking") or "")
        elif block_type == "tool_use":
            tool_calls.append({
                "index": len(tool_calls),
                "id": block.get("id"),
                "type": "function",
                "function": {
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            })
    return {
        "id": raw.get("id"),
        "model": raw.get("model"),
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "".join(content_parts),
                "reasoning_content": "".join(reasoning_parts),
                "tool_calls": tool_calls or None,
            },
            "finish_reason": _ANTHROPIC_STOP_REASON_MAP.get(str(raw.get("stop_reason")), "stop"),
        }],
        "usage": _anthropic_usage_to_openai(raw.get("usage") or {}),
    }


class AnthropicStreamNormalizer:
    """Anthropic /v1/messages SSE 事件流 → StreamChunk 的有状态归一化器。

    事件序：message_start（请求 id/输入 usage）→ 若干 content_block_start/
    content_block_delta/content_block_stop（text/thinking/tool_use）→
    message_delta（stop_reason + 输出 usage）→ message_stop。
    归一化输出与 OpenAI chunk 相同形状（content/reasoning_content/tool_calls/
    finish_reason/usage），上层 chunk 消费方零改动。
    """

    def __init__(self):
        self._request_id: Optional[str] = None
        self._model: Optional[str] = None
        self._start_usage: Dict[str, Any] = {}
        self._output_tokens = 0
        self._tool_index = 0

    def normalize(self, raw: Any) -> Optional[StreamChunk]:
        if not isinstance(raw, dict):
            return None
        event_type = raw.get("type")

        if event_type == "ping":
            return None
        if event_type == "error":
            error = raw.get("error") or {}
            exc_cls = _ANTHROPIC_STREAM_ERROR_MAP.get(
                str(error.get("type") or ""), APIError,
            )
            raise exc_cls(
                f"Anthropic 流内错误 ({error.get('type')}): {error.get('message')}"
            )
        if event_type == "message_start":
            message = raw.get("message") or {}
            self._request_id = message.get("id")
            self._model = message.get("model")
            self._start_usage = dict(message.get("usage") or {})
            return StreamChunk(id=self._request_id, model=self._model, choices=[])
        if event_type == "content_block_start":
            block = raw.get("content_block") or {}
            if block.get("type") == "tool_use":
                tool_call = ChunkToolCall(
                    index=self._tool_index,
                    id=block.get("id"),
                    function=ToolCallFunction(name=str(block.get("name") or ""), arguments=""),
                )
                self._tool_index += 1
                return StreamChunk(
                    id=self._request_id,
                    model=self._model,
                    choices=[ChunkChoice(delta=ChunkDelta(tool_calls=[tool_call]))],
                )
            return None
        if event_type == "content_block_delta":
            delta = raw.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                text = delta.get("text") or ""
                if not text:
                    return None
                return StreamChunk(
                    id=self._request_id,
                    model=self._model,
                    choices=[ChunkChoice(delta=ChunkDelta(content=text))],
                )
            if delta_type == "thinking_delta":
                thinking = delta.get("thinking") or ""
                if not thinking:
                    return None
                return StreamChunk(
                    id=self._request_id,
                    model=self._model,
                    choices=[ChunkChoice(delta=ChunkDelta(reasoning_content=thinking))],
                )
            if delta_type == "input_json_delta":
                partial = delta.get("partial_json")
                if partial is None:
                    return None
                tool_call = ChunkToolCall(
                    index=max(self._tool_index - 1, 0),
                    function=ToolCallFunction(arguments=str(partial)),
                )
                return StreamChunk(
                    id=self._request_id,
                    model=self._model,
                    choices=[ChunkChoice(delta=ChunkDelta(tool_calls=[tool_call]))],
                )
            # signature_delta 等未知增量：忽略
            return None
        if event_type == "message_delta":
            delta = raw.get("delta") or {}
            stop_reason = delta.get("stop_reason")
            usage_raw = raw.get("usage") or {}
            if usage_raw.get("output_tokens") is not None:
                self._output_tokens = int(usage_raw.get("output_tokens") or 0)
            if stop_reason is not None:
                self._finish_reason = _ANTHROPIC_STOP_REASON_MAP.get(str(stop_reason), "stop")
                return StreamChunk(
                    id=self._request_id,
                    model=self._model,
                    choices=[ChunkChoice(delta=ChunkDelta(), finish_reason=self._finish_reason)],
                )
            return None
        if event_type == "message_stop":
            usage_dict = _anthropic_usage_to_openai(self._start_usage)
            usage_dict["completion_tokens"] = self._output_tokens
            usage_dict["total_tokens"] = usage_dict["prompt_tokens"] + self._output_tokens
            return StreamChunk(
                id=self._request_id,
                model=self._model,
                usage=ChunkUsage(
                    prompt_tokens=usage_dict["prompt_tokens"],
                    completion_tokens=usage_dict["completion_tokens"],
                    total_tokens=usage_dict["total_tokens"],
                    prompt_tokens_details=usage_dict["prompt_tokens_details"],
                    prompt_cache_hit_tokens=usage_dict["prompt_cache_hit_tokens"],
                    prompt_cache_miss_tokens=usage_dict["prompt_cache_miss_tokens"],
                ),
            )
        # content_block_stop / 未知事件：忽略
        return None

    def finalize_sync(self, raw: dict) -> "NativeSyncResponse":
        """非流式 Anthropic 响应 JSON → OpenAI 形状 → NativeSyncResponse。"""
        return NativeSyncResponse(
            _anthropic_sync_to_openai(raw),
            ChunkNormalizer(get_model_policy(model_name="openai/standard", api_base="")),
        )


_ANTHROPIC_KNOWN_KWARGS = {
    "model", "messages", "api_key", "api_base", "timeout", "stream",
    "max_tokens", "max_completion_tokens", "max_output_tokens",
    "temperature", "top_p", "top_k", "stop",
    "tools", "tool_choice", "thinking", "output_config", "metadata",
    "extra_body", "extra_headers", "stream_options", "user",
}


def _anthropic_completion(**kwargs):
    """Anthropic Messages 协议请求入口（anthropic/ 前缀路由专用）。"""
    model = str(kwargs.get("model") or "")
    clean_model = model.split("/", 1)[1] if "/" in model else model
    url, headers = _anthropic_resolve_endpoint(
        api_base=kwargs.get("api_base"),
        api_key=kwargs.get("api_key"),
        extra_headers=kwargs.get("extra_headers"),
    )

    for litellm_meta_key in ("num_retries", "allowed_openai_params"):
        kwargs.pop(litellm_meta_key, None)

    unknown_keys = sorted(set(kwargs) - _ANTHROPIC_KNOWN_KWARGS)
    if unknown_keys:
        logger.warning(
            "native anthropic 传输忽略未识别的请求参数（不会发送到服务端）: %s",
            ", ".join(unknown_keys),
        )

    is_stream = bool(kwargs.get("stream", False))
    system, anth_messages = _anthropic_translate_messages(kwargs.get("messages"))
    payload: Dict[str, Any] = {
        "model": clean_model,
        "messages": anth_messages,
        "max_tokens": (
            kwargs.get("max_tokens")
            or kwargs.get("max_completion_tokens")
            or kwargs.get("max_output_tokens")
            or _ANTHROPIC_DEFAULT_MAX_TOKENS
        ),
        "stream": is_stream,
    }
    if system is not None:
        payload["system"] = system

    anthropic_tools = _anthropic_translate_tools(kwargs.get("tools"))
    if anthropic_tools:
        payload["tools"] = anthropic_tools
        tool_choice = _anthropic_translate_tool_choice(kwargs.get("tool_choice"))
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    if kwargs.get("temperature") is not None:
        payload["temperature"] = kwargs.get("temperature")
    if kwargs.get("top_p") is not None:
        payload["top_p"] = kwargs.get("top_p")
    if kwargs.get("top_k") is not None:
        payload["top_k"] = kwargs.get("top_k")
    if kwargs.get("metadata") is not None:
        payload["metadata"] = kwargs.get("metadata")
    # Anthropic 协议原生形状的思考参数与厂商扩展（阶跃 output_config.effort）
    # 由 llm_dialects 按 anthropic_route 组装，这里原样透传。
    for anthropic_key in ("thinking", "output_config"):
        if kwargs.get(anthropic_key) is not None:
            payload[anthropic_key] = kwargs.get(anthropic_key)
    stop = kwargs.get("stop")
    if stop:
        payload["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)

    extra_body = kwargs.get("extra_body")
    if isinstance(extra_body, dict):
        payload.update(extra_body)

    req_timeout = kwargs.get("timeout")
    timeout_arg = None
    if req_timeout is not None:
        timeout_arg = httpx.Timeout(
            connect=min(15.0, float(req_timeout)),
            read=600.0 if is_stream else float(req_timeout),
            write=30.0,
            pool=10.0,
        )

    return _dispatch_request(
        url, headers, payload, is_stream, timeout_arg, AnthropicStreamNormalizer()
    )
