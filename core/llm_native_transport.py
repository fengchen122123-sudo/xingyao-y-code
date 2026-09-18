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
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx

from core.llm_policy import ModelPolicy, get_model_policy

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


class ChunkChoice:
    __slots__ = ("index", "delta", "finish_reason")

    def __init__(
        self,
        delta: ChunkDelta,
        finish_reason: Optional[str] = None,
        index: int = 0,
    ):
        self.delta = delta
        self.finish_reason = finish_reason
        self.index = index

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
            url = "https://api.minimax.chat/v1/text/chatcompletion_v2"
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

    # 合并方言默认头与外部追加头
    headers.update(policy.default_headers)
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
})

# OpenAI 兼容标准参数：存在即透传到请求体顶层
_NATIVE_PASSTHROUGH_KEYS = frozenset({
    "reasoning_effort", "output_config", "thinking",
    "top_p", "presence_penalty", "frequency_penalty",
    "seed", "stop", "n", "logit_bias", "user",
    "response_format", "parallel_tool_calls",
})


def native_completion(**kwargs) -> NativeStreamResponse:
    """原生 OpenAI 兼容流式请求主入口。

    替代 litellm.completion，保证 100% 原始字段透传与零黑盒干扰。
    """
    model = kwargs.get("model") or ""
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

    # 序列化并发送请求
    client = _get_shared_client()
    req = client.build_request("POST", url, headers=headers, json=payload)
    try:
        if is_stream:
            resp = client.send(req, stream=True, timeout=timeout_arg)
        else:
            resp = client.send(req, stream=False, timeout=timeout_arg)
    except httpx.RequestError as exc:
        raise APIConnectionError(f"无法建立与模型服务端的连接: {exc}") from exc

    if resp.status_code != 200:
        try:
            resp.read()
        except httpx.HTTPError as exc:
            # 错误体读取失败必须包成 NativeLLMError：裸 httpx 异常冒泡出去
            # 会命中 _litellm_completion 的降级分支，同一请求对上游双发。
            raise APIConnectionError(f"错误响应体读取失败: {exc}") from exc
        finally:
            resp.close()
        raise _map_http_error(resp)

    normalizer = ChunkNormalizer(policy)
    if is_stream:
        return NativeStreamResponse(resp, normalizer)
    try:
        raw_json = resp.json()
    except httpx.HTTPError as exc:
        # 同上：网络层读失败不降级双发，直接按连接错误上抛
        raise APIConnectionError(f"非流式响应体读取失败: {exc}") from exc
    return NativeSyncResponse(raw_json, normalizer)


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
