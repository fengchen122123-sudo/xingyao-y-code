"""可选的 provider prompt-cache 保活服务；默认 120 秒间隔（DeepSeek/Kimi 路由生效）。"""

from __future__ import annotations

from copy import deepcopy
import logging
import threading
import time


logger = logging.getLogger(__name__)
MIN_KEEPALIVE_SECONDS = 60
# GLM 强制思考模型的保活快照保留思考时，给思考 token 预留的输出上限；
# 实际按生成量计费，该值只是防 1-token 截断报错的保险丝。
KEEPALIVE_THINKING_MODEL_MAX_TOKENS = 1024

# 保活请求超时随 prompt 规模放大（与主请求路径同口径：基础值 + tokens/1500，
# 上下限共用 core.llm 常量）。写死 30 秒时，大上下文（实测 40 万 token 会话）
# 在缓存恰好过期、需要全量预填充的场景下必然超时；一次 Timeout 即令服务
# 自停，随后 TTL 5 分钟无人续、真实请求全量 miss，形成级联
# （gui-runtime.log 中保活自停绝大多数为 Timeout）。缓存命中时预填充几乎
# 免费、请求很快返回，放大后的超时只是冷预填充的保险丝。
KEEPALIVE_TIMEOUT_TOKENS_PER_SECOND = 1500

# 服务层有界重试：瞬时失败（超时/限流/网络抖动）不再一次失败即自停——
# 自停是级联放大器（服务停 → TTL 过期 → 真实请求全量 miss）。
# 重试期间有新请求注册（generation 变化）立即让位；重试耗尽仍失败才自停，
# 下次主模型请求 register_request 时会重新武装服务。
KEEPALIVE_MAX_ATTEMPTS = 3
KEEPALIVE_RETRY_BACKOFF_SECONDS = 10.0
KEEPALIVE_RETRY_BACKOFF_MAX_SECONDS = 60.0


def load_keepalive_interval() -> int:
    """保活间隔秒数：默认 120；显式配置 0 关闭；0<x<60 拒绝（过于频繁）。"""
    from core.model_manager import load_config

    raw = load_config().get("prompt_cache_keepalive_seconds", 120)
    try:
        interval = int(raw or 0)
    except (TypeError, ValueError):
        # 配置写成 "abc" 这类非法值时不能抛穿 register_request（每轮对话都会调用），
        # 回落默认间隔并告警
        logger.warning(
            "prompt_cache_keepalive_seconds 配置非法: %r，已回落默认 120 秒", raw
        )
        interval = 120
    if interval < 0:
        raise ValueError("prompt_cache_keepalive_seconds 不能为负数")
    if 0 < interval < MIN_KEEPALIVE_SECONDS:
        raise ValueError(
            f"prompt_cache_keepalive_seconds 至少为 {MIN_KEEPALIVE_SECONDS} 秒，0 表示关闭"
        )
    return interval


def _request_thinking_locked(model):
    """判定保活请求是否不得关闭思考（GLM 强制思考模型特例）。"""
    try:
        from core.llm import _glm_keepalive_thinking_locked

        return bool(_glm_keepalive_thinking_locked(str(model or "")))
    except Exception:
        return False


def _qwen_keepalive_default_thinking(model):
    """判定 Qwen 家族「请求未显式表达思考意图」的默认思考档。

    Qwen3.x 系列在未显式配置思考档时默认开启思考（enable_thinking 缺省为 true），
    保活快照 max_tokens=1 会因思考输出受输出上限约束而被 provider 拒绝，
    首次保活报错即令保活服务自我停用。Qwen 能力表各档位均有 off 档，
    可直接补 enable_thinking=False 关闭（与 GLM 强制思考的抬高上限策略相反）。
    """
    try:
        from core.llm import _is_qwen_family

        return bool(_is_qwen_family(str(model or "")))
    except Exception:
        return False


def _estimate_text_tokens(text):
    """粗估文本 token（ASCII bytes/4、CJK 按字计，取大者），仅供超时放大。

    与 tool_result_maintenance 的估算器同款：允许偏差、必须离线快速，
    走离线快速估算而非全量 tokenize（那要把大上下文整体编码一遍，秒级）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) > 0x2E7F)
    return max(len(text.encode("utf-8")) // 4, cjk)


def _estimate_messages_tokens(messages):
    """粗估 messages 的 token 量：只为超时放大服务，数量级正确即可。"""
    total = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            total += _estimate_text_tokens(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") in ("text", "input_text"):
                    total += _estimate_text_tokens(str(item.get("text") or ""))
    return total


def _keepalive_request_timeout(messages):
    """保活请求超时 = 主请求同口径（基础值 + tokens/1500，封顶共用常量）。"""
    from core.llm import (
        API_REQUEST_MAX_TIMEOUT_SECONDS,
        API_REQUEST_TIMEOUT_SECONDS,
    )

    estimated = _estimate_messages_tokens(messages)
    return min(
        API_REQUEST_MAX_TIMEOUT_SECONDS,
        API_REQUEST_TIMEOUT_SECONDS
        + estimated // KEEPALIVE_TIMEOUT_TOKENS_PER_SECOND,
    )


def build_keepalive_request(request):
    """复制缓存相关请求前缀，并把保活输出严格限制为 1 token。

    例外 1：GLM 强制思考模型（glm-5.3/5.3-flash）收到 thinking.type=disabled
    会直接报错，其保活快照保留 thinking 原样并抬高输出上限给思考留预算，
    否则首次保活即失败并令服务自我停用。其余模型行为保持不变。
    例外 2：Qwen 家族默认思考档（请求无 enable_thinking/thinking 任何显式
    配置）补 enable_thinking=False，防止 max_tokens=1 撞上默认开启的思考。
    """
    if not request.get("model") or not request.get("messages"):
        raise ValueError("保活请求缺少 model 或 messages")
    thinking_locked = _request_thinking_locked(request.get("model"))
    qwen_default_thinking = _qwen_keepalive_default_thinking(request.get("model"))
    keepalive = {
        "model": request["model"],
        "api_key": request.get("api_key", ""),
        "api_base": request.get("api_base"),
        "messages": deepcopy(request["messages"]),
        "tools": deepcopy(request.get("tools") or []),
        "stream": False,
        "max_tokens": (
            KEEPALIVE_THINKING_MODEL_MAX_TOKENS if thinking_locked else 1
        ),
        "num_retries": 0,
        # 超时随 prompt 规模放大（与主请求同口径）：大上下文冷预填充需要
        # 分钟级时间，写死 30 秒必然 Timeout 并级联成 TTL 过期全量 miss
        "timeout": _keepalive_request_timeout(request.get("messages")),
    }
    if "tool_choice" in request:
        keepalive["tool_choice"] = "none"
    if request.get("metadata") is not None:
        keepalive["metadata"] = deepcopy(request["metadata"])
    if request.get("extra_headers") is not None:
        keepalive["extra_headers"] = deepcopy(request["extra_headers"])
    extra_body = (
        deepcopy(request["extra_body"])
        if request.get("extra_body") is not None
        else {}
    )
    if extra_body:
        # 推理强度与保活无关；移除后避免输出上限与推理预算冲突。
        extra_body.pop("reasoning_effort", None)
        if (
            not thinking_locked
            and extra_body.get("thinking", {}).get("type") == "enabled"
        ):
            extra_body["thinking"] = {"type": "disabled"}
        if not thinking_locked and extra_body.get("enable_thinking") is True:
            extra_body["enable_thinking"] = False
    if qwen_default_thinking and not thinking_locked:
        # 请求无任何显式思考配置时才补默认关思考；显式配置走上面的翻转分支
        if "enable_thinking" not in extra_body and "thinking" not in extra_body:
            extra_body["enable_thinking"] = False
    if extra_body:
        keepalive["extra_body"] = extra_body
    return keepalive


def account_keepalive_response(response, telemetry):
    """严格记账一次保活响应；缺失或无效 usage 会停止后续保活。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        raise RuntimeError("provider 保活响应未返回 usage")
    from core.llm import _account_server_usage

    record = _account_server_usage(
        usage,
        model_name=str(telemetry.get("model_name") or ""),
        api_base=str(telemetry.get("api_base") or ""),
        session_id=str(telemetry.get("session_id") or ""),
        cache_scope="keepalive",
        invalidation_reason="保活请求",
    )
    if record is None:
        raise RuntimeError("provider 保活 usage 校验失败")
    return record


class CacheKeepaliveService:
    def __init__(self):
        self._condition = threading.Condition()
        self._snapshot = None
        self._interval = 0
        self._last_activity = 0.0
        self._last_error = ""
        self._generation = 0
        self._thread = None
        self._telemetry = None

    def register_request(self, request, interval=None, telemetry=None):
        resolved_interval = load_keepalive_interval() if interval is None else int(interval)
        if resolved_interval < 0 or 0 < resolved_interval < MIN_KEEPALIVE_SECONDS:
            raise ValueError(
                f"prompt_cache_keepalive_seconds 至少为 {MIN_KEEPALIVE_SECONDS} 秒，0 表示关闭"
            )
        snapshot = build_keepalive_request(request) if resolved_interval else None
        with self._condition:
            self._generation += 1
            self._snapshot = snapshot
            self._telemetry = deepcopy(telemetry) if snapshot else None
            self._interval = resolved_interval
            self._last_activity = time.monotonic()
            self._last_error = ""
            if resolved_interval and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(
                    target=self._run,
                    name="prompt-cache-keepalive",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()

    def status(self):
        with self._condition:
            return {
                "enabled": bool(self._interval and self._snapshot),
                "interval": self._interval,
                "error": self._last_error,
            }

    def _wait_backoff(self, backoff_seconds, generation):
        """退避等待；期间有新快照注册/服务停用（generation 变化）立即返回 False。"""
        deadline = time.monotonic() + backoff_seconds
        with self._condition:
            while True:
                if self._generation != generation:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True
                self._condition.wait(timeout=min(remaining, 1.0))

    def _execute_with_retry(self, snapshot, telemetry, generation):
        """执行一次保活并记账：瞬时失败有界退避重试。

        返回空串表示成功；否则为最后错误的类名（含被新注册打断的情况，
        由调用方按 generation 区分是让位还是自停）。每次尝试用快照的
        深拷贝，防止 provider 客户端改写参数污染后续重试。
        """
        last_error = ""
        for attempt in range(KEEPALIVE_MAX_ATTEMPTS):
            try:
                from core.llm import _dispatch_completion

                response = _dispatch_completion(**deepcopy(snapshot))
                account_keepalive_response(response, telemetry or {})
                return ""
            except Exception as exc:
                last_error = type(exc).__name__
                with self._condition:
                    superseded = self._generation != generation
                if superseded:
                    return last_error
                if attempt >= KEEPALIVE_MAX_ATTEMPTS - 1:
                    break
                backoff = min(
                    KEEPALIVE_RETRY_BACKOFF_SECONDS * (2 ** attempt),
                    KEEPALIVE_RETRY_BACKOFF_MAX_SECONDS,
                )
                logger.warning(
                    "prompt cache 保活第 %d/%d 次尝试失败(%s)，%.0f 秒后重试",
                    attempt + 1,
                    KEEPALIVE_MAX_ATTEMPTS,
                    last_error,
                    backoff,
                )
                if not self._wait_backoff(backoff, generation):
                    return last_error
        return last_error

    def _run(self):
        while True:
            with self._condition:
                while not self._interval or self._snapshot is None:
                    self._condition.wait()
                deadline = self._last_activity + self._interval
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                snapshot = deepcopy(self._snapshot)
                telemetry = deepcopy(self._telemetry)
                generation = self._generation
                self._last_activity = time.monotonic()

            error_name = self._execute_with_retry(snapshot, telemetry, generation)
            if not error_name:
                with self._condition:
                    if self._generation == generation:
                        self._last_activity = time.monotonic()
                continue
            with self._condition:
                if self._generation != generation:
                    # 让位：新请求已注册（或服务被重新武装），交给下一轮循环
                    continue
                logger.error(
                    "prompt cache 保活连续 %d 次尝试失败(%s)，服务自停；"
                    "下次主模型请求将重新注册快照",
                    KEEPALIVE_MAX_ATTEMPTS,
                    error_name,
                )
                self._snapshot = None
                self._telemetry = None
                self._interval = 0
                self._last_error = error_name


cache_keepalive_service = CacheKeepaliveService()
