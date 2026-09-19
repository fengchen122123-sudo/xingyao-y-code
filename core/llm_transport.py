"""自研 HTTPX 传输层：请求入口、在飞请求硬中断注册表与重试退避常量。

OpenAI 兼容 chat 路由一律由 core.llm_native_transport.native_completion
发出，任何异常直接向上抛出（回合循环统一失败收口并即时反馈到 UI），
不存在静默降级通道。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future
from typing import Dict, List

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
# 上层重试循环会把单次超时在同一个 Step 内放大数倍（重试留在同 Step），
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

    流包装对象自身可能没有 close()，真正的可关闭对象藏在里层
    （openai SDK Stream，close() 会关底层 httpx response，或 httpx.Response）
    ，逐层探测。
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
# native 引擎路由支持范围
# ---------------------------------------------------------------------------
# native 引擎可正确路由的 provider 前缀（与各协议路由的前缀剥离白名单一致）。
# anthropic/ 走 _anthropic_completion 的 /v1/messages 协议；azure/ gemini/
# vertex_ai/ bedrock/ 等厂商专属云路由自研引擎未实现，仍不支持。
_NATIVE_PROVIDER_PREFIXES = frozenset(
    ("deepseek", "moonshot", "zhipu", "openai", "dashscope", "minimax", "anthropic")
)


def _is_native_supported_model(model) -> bool:
    """判断模型是否属于 native 引擎可接手的路由。

    OpenAI 兼容 chat 路由与 anthropic/ 前缀（Messages 协议）均已支持；
    OpenAI 思考模型（o1/o3 系列）需要 max_completion_tokens 与 developer 角色
    改写，native 尚未实现，不支持。
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


def _dispatch_completion(**kwargs):
    """主请求 completion 入口（自研引擎唯一路径）。

    OpenAI 兼容 chat 路由一律由 core.llm_native_transport.native_completion
    发出；任何异常——包括 native 内部的非预期 bug——都直接向上抛出，由回合
    循环统一失败收口并即时反馈到 UI。绝不静默降级重发：那既会掩盖
    自研引擎的 bug，也有同一请求对上游双发的风险。

    自研引擎不支持的协议路由（o1/o3 思考模型——需要 max_completion_tokens 与
    developer 角色改写；azure/gemini 等厂商云路由）显式报错，不支持的暗路径
    一律关闭。anthropic/ 前缀已由 native 的 Messages 协议适配层接管。
    """
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
    """Responses 协议入口。

    自研引擎尚未实现 OpenAI Responses API；配置为 responses 协议的 provider
    在发起请求时立即显式报错（不支持的路由不走任何暗路径）。
    """
    from core.llm_native_transport import NativeLLMError

    raise NativeLLMError(
        "自研引擎暂不支持 OpenAI Responses 协议："
        "请将该 provider 的协议改为 openai 兼容（chat completions）。"
    )


def _token_counter(model, messages):
    """Token 估算入口：自研启发式估算器（fast_token_counter）。

    纯本地计算、对任意模型可用、永不抛错，无需兜底。
    """
    from core.llm_native_transport import fast_token_counter

    return fast_token_counter(model=model, messages=messages)


def _context_overflow_error():
    """上下文超限异常类：自研引擎异常体系（native 请求只会抛自有异常）。"""
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

    # 3. 检查异常类型（自研引擎的 RateLimitError 命中此分支与上面的 429 状态码）
    exc_type = type(exc).__name__.lower()
    if "ratelimit" in exc_type:
        return True, retry_after

    # 4. 字符串内容匹配（不区分大小写，包含常见中文/英文关键词与无空格形式）
    err_str = str(exc).lower()
    if any(k in err_str for k in (
        "rate limit", "ratelimit", "too many requests",
        "too frequent", "请求过于频繁", "accountratelimitexceeded",
        "throttl",
    )):
        return True, retry_after

    return False, None
