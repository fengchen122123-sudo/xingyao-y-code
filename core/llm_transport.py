from __future__ import annotations

import logging
import threading
from concurrent.futures import Future
from typing import Dict, List

logger = logging.getLogger(__name__)

API_REQUEST_TIMEOUT_SECONDS = 90

# 单次 API 请求超时上限（秒）：超时随载荷 token 数放大（大上下文 + 深度思考
# 本来就需要几分钟），但必须有上限，避免挂起的连接无限等待
API_REQUEST_MAX_TIMEOUT_SECONDS = 600

# 单次请求等待反馈：超过阈值后按间隔向 UI 报一次“模型响应中”，
# 避免大载荷请求长时间无反馈被误判为卡死
WAIT_NOTICE_THRESHOLD_SECONDS = 120.0
WAIT_NOTICE_INTERVAL_SECONDS = 30.0

REQUEST_WAIT_GIVE_UP_SECONDS = 600.0


class RequestWaitGiveUpError(RuntimeError):
    """等待模型响应达到硬上限（REQUEST_WAIT_GIVE_UP_SECONDS）：结束本轮而非重试。

    继承 RuntimeError 以落入轮次级 except Exception 的统一失败收口
    （回滚 + error/turn_error 事件）；API 重试处理器对其直接 re-raise，
    绝不进入重试循环（重试只会再等一个上限时长）。
    """


def _submit_daemon_call(function, *args, **kwargs):
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


_inflight_lock = threading.Lock()
_inflight_streams: Dict["threading.Event", List["_InflightStream"]] = {}


def _close_stream_quietly(stream):
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
    if cancel_event is None:
        return
    with _inflight_lock:
        holders = list(_inflight_streams.get(cancel_event, ()))
    for holder in holders:
        try:
            holder.close()
        except Exception:
            pass


_NATIVE_PROVIDER_PREFIXES = frozenset(
    ("deepseek", "moonshot", "zhipu", "openai", "dashscope", "minimax", "anthropic")
)


def _is_native_supported_model(model) -> bool:
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


def _dispatch_completion(**kwargs):
    if not _is_native_supported_model(kwargs.get("model")):
        from core.llm_native_transport import NativeLLMError

        raise NativeLLMError(
            f"模型 {kwargs.get('model')!r} 不在自研引擎支持范围内："
            "o1/o3 思考模型与厂商云路由（azure/gemini 等）暂未实现，"
            "请改用 OpenAI 兼容或 anthropic 协议的模型路由。"
        )
    from core.llm_native_transport import native_completion

    return native_completion(**kwargs)


def _dispatch_responses(**kwargs):
    from core.llm_native_transport import NativeLLMError

    raise NativeLLMError(
        "自研引擎暂不支持 OpenAI Responses 协议："
        "请将该 provider 的协议改为 openai 兼容（chat completions）。"
    )


def _token_counter(model, messages):
    from core.llm_native_transport import fast_token_counter

    return fast_token_counter(model=model, messages=messages)


def _context_overflow_error():
    from core.llm_native_transport import ContextWindowExceededError

    return ContextWindowExceededError


def is_rate_limit_error(exc: Exception) -> tuple[bool, float | None]:
    if exc is None:
        return False, None
    retry_after = None
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

    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True, retry_after

    exc_type = type(exc).__name__.lower()
    if "ratelimit" in exc_type:
        return True, retry_after

    err_str = str(exc).lower()
    if any(k in err_str for k in (
        "rate limit", "ratelimit", "too many requests",
        "too frequent", "请求过于频繁", "accountratelimitexceeded",
        "throttl",
    )):
        return True, retry_after

    return False, None
