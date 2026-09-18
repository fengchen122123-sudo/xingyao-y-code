"""LiteLLM transport, inflight request interruption, and retry backoff."""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future
from typing import Dict, List

# 本地内置离线映射，避免每次启动请求 GitHub
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# 屏蔽 LiteLLM 启动时的调试噪音，但保留 WARNING 及以上的有用告警
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
logging.getLogger("litellm").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# 单次 API 请求超时（秒）：长思考模型的首个 token 可能较慢，不宜过紧
API_REQUEST_TIMEOUT_SECONDS = 90

# 单次 API 请求超时上限（秒）：超时随载荷 token 数放大（大上下文 + 深度思考
# 本来就需要几分钟），但必须有上限，避免挂起的连接无限等待
API_REQUEST_MAX_TIMEOUT_SECONDS = 600

# 单次请求等待反馈：超过阈值后按间隔向 UI 报一次“模型响应中”，
# 避免大载荷请求长时间无反馈被误判为卡死
WAIT_NOTICE_THRESHOLD_SECONDS = 120.0
WAIT_NOTICE_INTERVAL_SECONDS = 30.0

# 等待硬上限（秒）：首字节等待或流中段连续空闲达到该值即放弃本轮请求并结束回合。
# litellm num_retries 会把单次超时在同一个 Step 内放大数倍（重试留在同 Step），
# 等待提示里的“超时上限”不等于客户端真实等待上限；与其无限等，不如到点主动
# 结束回合让用户决定重试。
REQUEST_WAIT_GIVE_UP_SECONDS = 600.0


class RequestWaitGiveUpError(RuntimeError):
    """等待模型响应达到硬上限（REQUEST_WAIT_GIVE_UP_SECONDS）：结束本轮而非重试。

    继承 RuntimeError 以落入轮次级 except Exception 的统一失败收口
    （回滚 + error/turn_error 事件）；API 重试处理器对其直接 re-raise，
    绝不进入重试循环（重试只会再等一个上限时长）。
    """


def _submit_daemon_call(function, *args, **kwargs):
    """Run a blocking call without adding a non-daemon interpreter-exit hook."""
    future = Future()

    def runner():
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)

    threading.Thread(target=runner, daemon=True, name="ycode-blocking-call").start()
    return future


# ---------------------------------------------------------------------------
# 在飞 LLM 请求硬中断注册表
# ---------------------------------------------------------------------------
_inflight_lock = threading.Lock()
_inflight_streams: Dict["threading.Event", List["_InflightStream"]] = {}


def _close_stream_quietly(stream):
    """尽力关闭单个流对象（不触碰共享 client/连接池），吞掉一切异常。

    litellm 的 CustomStreamWrapper 没有 close()，真正的可关闭对象藏在
    .completion_stream（openai SDK Stream，close() 会关底层 httpx response）
    或 .response（httpx.Response）里，逐层探测。
    """
    seen = set()
    queue = [stream]
    while queue:
        target = queue.pop(0)
        if target is None or id(target) in seen:
            continue
        seen.add(id(target))
        close = getattr(target, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
            return
        queue.append(getattr(target, "completion_stream", None))
        queue.append(getattr(target, "response", None))


class _InflightStream:
    """一个在飞 LLM 请求的可关闭句柄；attach 前/后调用 close 都安全且幂等。"""

    __slots__ = ("stream", "closed")

    def __init__(self):
        self.stream = None
        self.closed = False

    def attach(self, stream):
        with _inflight_lock:
            if not self.closed:
                self.stream = stream
                return
        # attach 前已被 abort：立即关闭新建流，避免泄漏连接
        _close_stream_quietly(stream)

    def close(self):
        with _inflight_lock:
            if self.closed:
                return
            self.closed = True
            stream = self.stream
        if stream is not None:
            _close_stream_quietly(stream)


def _register_inflight_stream(cancel_event):
    """请求发出前登记；返回句柄，由流消费方在结束后 _unregister_inflight_stream。"""
    if cancel_event is None:
        return None
    holder = _InflightStream()
    with _inflight_lock:
        _inflight_streams.setdefault(cancel_event, []).append(holder)
    return holder


def _unregister_inflight_stream(cancel_event, holder):
    if cancel_event is None or holder is None:
        return
    with _inflight_lock:
        holders = _inflight_streams.get(cancel_event)
        if not holders:
            return
        try:
            holders.remove(holder)
        except ValueError:
            pass
        if not holders:
            _inflight_streams.pop(cancel_event, None)


def abort_inflight_streams(cancel_event):
    """硬中断指定取消事件名下的全部在飞 LLM 请求（停止按钮/Ctrl+C 路径调用）。

    调用方必须先 set cancel_event 再调用本函数：这样被关闭一侧的读循环
    会把中断识别为用户取消（KeyboardInterrupt 打捞路径），而非网络错误重试。
    """
    if cancel_event is None:
        return
    with _inflight_lock:
        holders = list(_inflight_streams.get(cancel_event, ()))
    for holder in holders:
        try:
            holder.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# litellm 懒加载与补丁
# ---------------------------------------------------------------------------
_litellm_module = None
_litellm_lock = threading.Lock()
_STREAM_USAGE_PATCH_FLAG = "_ycode_stream_usage_patched"


def _patch_litellm_stream_usage_details(_litellm):
    """修复 litellm 1.83.7 流式 usage 丢失 prompt_tokens_details 的问题。"""
    try:
        from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
    except Exception:
        return
    if getattr(CustomStreamWrapper, _STREAM_USAGE_PATCH_FLAG, False):
        return
    _original = CustomStreamWrapper.handle_openai_chat_completion_chunk

    def _patched(self, chunk):
        response_obj = _original(self, chunk)
        if response_obj is None:
            return response_obj
        usage = response_obj.get("usage")
        if isinstance(usage, dict) and (
            "prompt_tokens_details" in usage or "prompt_cache_hit_tokens" in usage
        ):
            try:
                response_obj["usage"] = _litellm.Usage(**usage)
            except Exception:
                pass
        return response_obj

    CustomStreamWrapper.handle_openai_chat_completion_chunk = _patched
    setattr(CustomStreamWrapper, _STREAM_USAGE_PATCH_FLAG, True)


def _get_litellm():
    """懒加载 litellm 模块，首次调用时 import 并执行一次性配置，后续调用直接返回缓存。"""
    global _litellm_module
    if _litellm_module is not None:
        return _litellm_module
    with _litellm_lock:
        if _litellm_module is not None:
            return _litellm_module
        import litellm as _litellm
        _litellm.suppress_debug_info = True
        _patch_litellm_stream_usage_details(_litellm)
        _litellm_module = _litellm
        return _litellm


def _is_native_llm_enabled() -> bool:
    """检查是否启用原生 HTTPX 传输引擎（可通过环境变量 YCODE_USE_NATIVE_LLM=0 临时回退 LiteLLM）。"""
    flag = os.environ.get("YCODE_USE_NATIVE_LLM", "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


# native 引擎可正确路由的 provider 前缀（与 _resolve_endpoint_and_headers 的
# 前缀剥离白名单一致）。azure/ gemini/ vertex_ai/ bedrock/ 等 litellm 专属
# provider 路由 native 无法表达，继续留在 litellm。
_NATIVE_PROVIDER_PREFIXES = frozenset(
    ("deepseek", "moonshot", "zhipu", "openai", "dashscope", "minimax")
)


def _is_native_supported_model(model) -> bool:
    """判断模型是否属于 native 引擎可接手的 OpenAI 兼容 chat 路由。

    OpenAI 思考模型（o1/o3 系列）需要 max_completion_tokens 与 developer 角色
    改写，native 尚未实现，必须留在 litellm。
    """
    name = str(model or "").strip().lower()
    if not name:
        return False
    if "/" in name:
        prefix, remainder = name.split("/", 1)
        if prefix not in _NATIVE_PROVIDER_PREFIXES:
            return False
    else:
        remainder = name
    if remainder.startswith("o1") or remainder.startswith("o3"):
        return False
    return True


def _litellm_completion(**kwargs):
    """主请求 completion 包装。

    在启用原生引擎且模型属于 native 支持范围时，优先使用
    core.llm_native_transport.native_completion；否则走 litellm.completion。
    native 抛出的非业务未知异常平滑降级至 litellm 重发。
    """
    if _is_native_llm_enabled() and _is_native_supported_model(kwargs.get("model")):
        from core.llm_native_transport import native_completion, NativeLLMError
        try:
            return native_completion(**kwargs)
        except NativeLLMError:
            raise
        except Exception as exc:
            logger.warning("原生传输调用失败，降级至 LiteLLM: %s", exc)
            return _get_litellm().completion(**kwargs)

    return _get_litellm().completion(**kwargs)


def _litellm_responses(**kwargs):
    """litellm.responses 的懒加载包装。"""
    return _get_litellm().responses(**kwargs)


def _litellm_token_counter(model, messages):
    """Token 估算包装。支持原生极速估算与 litellm 兜底。"""
    try:
        from core.llm_native_transport import fast_token_counter
        return fast_token_counter(model=model, messages=messages)
    except Exception:
        return _get_litellm().token_counter(model=model, messages=messages)


def _litellm_supports_vision(model):
    """litellm.supports_vision 的懒加载包装。"""
    try:
        return _get_litellm().supports_vision(model=model)
    except Exception:
        return False


def _litellm_context_overflow_error():
    """ContextWindowExceededError 异常类包装（同时兼容原生异常与 LiteLLM 异常）。"""
    try:
        from core.llm_native_transport import ContextWindowExceededError as NativeOverflowError
        litellm_cls = _get_litellm().ContextWindowExceededError
        return (litellm_cls, NativeOverflowError)
    except Exception:
        from core.llm_native_transport import ContextWindowExceededError
        return ContextWindowExceededError


def is_rate_limit_error(exc: Exception) -> tuple[bool, float | None]:
    """判断异常是否属于速率限制（429 / RateLimit / TooManyRequests）。
    返回 (is_rate_limit, retry_after_seconds)。
    """
    if exc is None:
        return False, None
    retry_after = None
    # 1. 尝试从 response headers 读取 retry-after
    try:
        resp = getattr(exc, "response", None)
        if resp is not None and hasattr(resp, "headers"):
            raw_ra = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
            if raw_ra:
                val = float(raw_ra)
                if 0 < val <= 60.0:
                    retry_after = val
    except Exception:
        pass

    # 2. 检查 status_code
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True, retry_after

    # 3. 检查异常类型
    exc_type = type(exc).__name__.lower()
    if "ratelimit" in exc_type:
        return True, retry_after

    try:
        litellm_rate_err = _get_litellm().exceptions.RateLimitError
        if isinstance(exc, litellm_rate_err):
            return True, retry_after
    except Exception:
        pass

    # 4. 字符串内容匹配（不区分大小写，包含常见中文/英文关键词与无空格形式）
    err_str = str(exc).lower()
    if any(k in err_str for k in (
        "rate limit", "ratelimit", "too many requests",
        "too frequent", "请求过于频繁", "accountratelimitexceeded",
        "throttl",
    )):
        return True, retry_after

    return False, None
