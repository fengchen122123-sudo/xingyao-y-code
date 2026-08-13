"""可选的 provider prompt-cache 保活服务；默认 120 秒间隔（DeepSeek/Kimi 路由生效）。"""

from __future__ import annotations

from copy import deepcopy
import logging
import threading
import time


logger = logging.getLogger(__name__)
MIN_KEEPALIVE_SECONDS = 60


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


def build_keepalive_request(request):
    """复制缓存相关请求前缀，并把保活输出严格限制为 1 token。"""
    if not request.get("model") or not request.get("messages"):
        raise ValueError("保活请求缺少 model 或 messages")
    keepalive = {
        "model": request["model"],
        "api_key": request.get("api_key", ""),
        "api_base": request.get("api_base"),
        "messages": deepcopy(request["messages"]),
        "tools": deepcopy(request.get("tools") or []),
        "stream": False,
        "max_tokens": 1,
        "num_retries": 0,
        "timeout": 30,
    }
    if "tool_choice" in request:
        keepalive["tool_choice"] = "none"
    if request.get("metadata") is not None:
        keepalive["metadata"] = deepcopy(request["metadata"])
    if request.get("extra_body") is not None:
        extra_body = deepcopy(request["extra_body"])
        # 推理强度与保活无关；移除后避免 1-token 上限与推理预算冲突。
        extra_body.pop("reasoning_effort", None)
        if extra_body.get("thinking", {}).get("type") == "enabled":
            extra_body["thinking"] = {"type": "disabled"}
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

            try:
                from core.llm import _litellm_completion
                response = _litellm_completion(**snapshot)
                account_keepalive_response(response, telemetry or {})
            except Exception as exc:
                error_name = type(exc).__name__
                logger.error("prompt cache 保活失败并已停止: %s", error_name)
                with self._condition:
                    if self._generation == generation:
                        self._snapshot = None
                        self._telemetry = None
                        self._interval = 0
                        self._last_error = error_name
            else:
                with self._condition:
                    if self._generation == generation:
                        self._last_activity = time.monotonic()


cache_keepalive_service = CacheKeepaliveService()
