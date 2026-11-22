from __future__ import annotations

from copy import deepcopy
import logging
import threading
import time


logger = logging.getLogger(__name__)
MIN_KEEPALIVE_SECONDS = 60
KEEPALIVE_THINKING_MODEL_MAX_TOKENS = 1024

KEEPALIVE_TIMEOUT_TOKENS_PER_SECOND = 1500

KEEPALIVE_MAX_ATTEMPTS = 3
KEEPALIVE_RETRY_BACKOFF_SECONDS = 10.0
KEEPALIVE_RETRY_BACKOFF_MAX_SECONDS = 60.0

MAX_KEEPALIVE_SESSIONS = 4
# 条目空闲过期：last_activity 超过 30 分钟没有主请求/保活活动的会话直接清理，
# 避免线程被早已离开的会话长期占用（缓存 TTL 5 分钟，30 分钟远大于 TTL）。
KEEPALIVE_IDLE_EXPIRE_SECONDS = 30 * 60
# 无会话身份时的固定键：兼容单会话调用方（旧签名 register_request(...)）。
DEFAULT_KEEPALIVE_SESSION_KEY = "__default__"


def _resolve_keepalive_session_key(session_key=None, telemetry=None):
    key = str(session_key or "").strip()
    if not key:
        key = str((telemetry or {}).get("session_id") or "").strip()
    return key or DEFAULT_KEEPALIVE_SESSION_KEY


def load_keepalive_interval() -> int:
    """保活间隔秒数：默认 120；显式配置 0 关闭；越界值回落默认并告警。

    本函数在每轮主对话请求的前置区被裸调（llm.py cache_scope == "main"），
    任何 raise 都会把全部会话打穿成 turn_error——非数字与数字越界（负数、
    0<x<60）一律回落默认 120 并告警，绝不抛出。
    """
    from core.model_manager import load_config

    raw = load_config().get("prompt_cache_keepalive_seconds", 120)
    try:
        interval = int(raw or 0)
    except (TypeError, ValueError):
        logger.warning(
            "prompt_cache_keepalive_seconds 配置非法: %r，已回落默认 120 秒", raw
        )
        interval = 120
    if interval < 0:
        logger.warning(
            "prompt_cache_keepalive_seconds 配置为负数: %r，已回落默认 120 秒", raw
        )
        interval = 120
    elif 0 < interval < MIN_KEEPALIVE_SECONDS:
        logger.warning(
            "prompt_cache_keepalive_seconds 配置为 %r（小于最小值 %d 秒，0 表示关闭），"
            "已回落默认 120 秒",
            raw, MIN_KEEPALIVE_SECONDS,
        )
        interval = 120
    return interval


def _request_thinking_locked(model):
    """判定保活请求是否不得关闭思考（GLM 强制思考模型特例）。"""
    try:
        from core.llm import _glm_keepalive_thinking_locked

        return bool(_glm_keepalive_thinking_locked(str(model or "")))
    except Exception:
        return False


def _qwen_keepalive_default_thinking(model):
    try:
        from core.llm import _is_qwen_family

        return bool(_is_qwen_family(str(model or "")))
    except Exception:
        return False


def _estimate_text_tokens(text):
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) > 0x2E7F)
    return max(len(text.encode("utf-8")) // 4, cjk)


def _estimate_messages_tokens(messages):
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
    # 保活车道证据（Task 5）：已报告缓存明细的轮次计入 keepalive 桶的轮次与
    # 累计 token，供 _supports_cache_keepalive 的车道证据门读取；未报告轮次
    # 不进桶（未报告 ≠ 0，绝不进分母）。补记失败只记日志，绝不影响保活主流程。
    if record.get("cache_reported"):
        try:
            from core.llm_diagnostics import record_keepalive_lane_usage

            record_keepalive_lane_usage(
                str(telemetry.get("session_id") or ""),
                str(telemetry.get("model_name") or ""),
                str(telemetry.get("api_base") or ""),
                record.get("prompt_tokens") or 0,
                record.get("cached_tokens") or 0,
            )
        except Exception:
            logger.debug("保活车道证据记账失败（已忽略）", exc_info=True)
    return record


class CacheKeepaliveService:

    def __init__(self):
        self._condition = threading.Condition()
        self._entries = {}
        self._last_error = ""
        self._thread = None

    def register_request(self, request, interval=None, telemetry=None, session_key=None):
        resolved_interval = load_keepalive_interval() if interval is None else int(interval)
        if resolved_interval < 0 or 0 < resolved_interval < MIN_KEEPALIVE_SECONDS:
            raise ValueError(
                f"prompt_cache_keepalive_seconds 至少为 {MIN_KEEPALIVE_SECONDS} 秒，0 表示关闭"
            )
        resolved_key = _resolve_keepalive_session_key(session_key, telemetry)
        snapshot = build_keepalive_request(request) if resolved_interval else None
        with self._condition:
            if resolved_interval:
                entry = self._entries.get(resolved_key)
                if entry is None:
                    entry = {
                        "snapshot": None,
                        "telemetry": {},
                        "last_activity": 0.0,
                        "interval": resolved_interval,
                        "revision": 0,
                    }
                    self._entries[resolved_key] = entry
                entry["snapshot"] = snapshot
                entry["telemetry"] = deepcopy(telemetry) if telemetry else {}
                entry["interval"] = resolved_interval
                entry["last_activity"] = time.monotonic()
                entry["revision"] += 1
                self._last_error = ""
                self._evict_oldest_locked(exclude=resolved_key)
            else:
                self._entries.pop(resolved_key, None)
            if self._entries and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(
                    target=self._run,
                    name="prompt-cache-keepalive",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()

    def status(self):
        with self._condition:
            intervals = [entry["interval"] for entry in self._entries.values()]
            return {
                "enabled": bool(self._entries),
                "interval": max(intervals) if intervals else 0,
                "error": self._last_error,
                "session_count": len(self._entries),
            }

    def get_session_snapshot(self, session_key=None):
        """只读返回会话最后一次主请求快照的深拷贝；无条目返回 None。

        供插入式压缩复用主请求字节（ledger 消息 + tools + 亲和参数原样）。
        绝不改动调度状态：不刷新 last_activity、不创建/唤醒保活线程。
        """
        key = _resolve_keepalive_session_key(session_key)
        with self._condition:
            entry = self._entries.get(key)
            if not entry:
                return None
            snapshot = entry.get("snapshot")
            return deepcopy(snapshot) if snapshot else None

    def _evict_oldest_locked(self, exclude=None):
        if len(self._entries) <= MAX_KEEPALIVE_SESSIONS:
            return
        candidates = [key for key in self._entries if key != exclude]
        if not candidates:
            return
        oldest_key = min(
            candidates, key=lambda key: self._entries[key]["last_activity"]
        )
        del self._entries[oldest_key]
        logger.info(
            "prompt cache 保活会话数超出上限 %d，淘汰最久未活动的会话保活",
            MAX_KEEPALIVE_SESSIONS,
        )

    def _prune_idle_locked(self):
        now = time.monotonic()
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry["last_activity"] > KEEPALIVE_IDLE_EXPIRE_SECONDS
        ]
        for key in expired:
            del self._entries[key]
            logger.info(
                "prompt cache 保活会话空闲超过 %d 秒，停止该会话保活",
                KEEPALIVE_IDLE_EXPIRE_SECONDS,
            )

    def _entry_superseded_locked(self, session_key, revision):
        entry = self._entries.get(session_key)
        return entry is None or entry["revision"] != revision

    def _wait_backoff(self, backoff_seconds, session_key, revision):
        deadline = time.monotonic() + backoff_seconds
        with self._condition:
            while True:
                if self._entry_superseded_locked(session_key, revision):
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True
                self._condition.wait(timeout=min(remaining, 1.0))

    def _execute_with_retry(
        self, session_key, snapshot, telemetry, revision, retry_template=None
    ):
        last_error = ""
        for attempt in range(KEEPALIVE_MAX_ATTEMPTS):
            try:
                from core.llm import _dispatch_completion

                if attempt == 0:
                    payload = snapshot
                else:
                    payload = deepcopy(
                        snapshot if retry_template is None else retry_template
                    )
                response = _dispatch_completion(**payload)
                account_keepalive_response(response, telemetry or {})
                return ""
            except Exception as exc:
                last_error = type(exc).__name__
                with self._condition:
                    superseded = self._entry_superseded_locked(
                        session_key, revision
                    )
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
                if not self._wait_backoff(backoff, session_key, revision):
                    return last_error
        return last_error

    def _run(self):
        while True:
            with self._condition:
                self._prune_idle_locked()
                while not self._entries:
                    self._condition.wait()
                    self._prune_idle_locked()
                session_key, entry = min(
                    self._entries.items(),
                    key=lambda item: item[1]["last_activity"] + item[1]["interval"],
                )
                remaining = (
                    entry["last_activity"] + entry["interval"] - time.monotonic()
                )
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                snapshot = deepcopy(entry["snapshot"])
                telemetry = deepcopy(entry["telemetry"])
                revision = entry["revision"]
                retry_template = entry["snapshot"]
                # 立即回拨该条目，避免重试期间被自身重复触发
                entry["last_activity"] = time.monotonic()

            error_name = self._execute_with_retry(
                session_key, snapshot, telemetry, revision, retry_template
            )
            if not error_name:
                with self._condition:
                    current = self._entries.get(session_key)
                    if current is not None and current["revision"] == revision:
                        current["last_activity"] = time.monotonic()
                    self._last_error = ""
                continue
            with self._condition:
                if self._entry_superseded_locked(session_key, revision):
                    continue
                logger.error(
                    "prompt cache 保活连续 %d 次尝试失败(%s)，该会话保活自停；"
                    "下次主模型请求将重新注册快照（其余会话不受影响）",
                    KEEPALIVE_MAX_ATTEMPTS,
                    error_name,
                )
                self._entries.pop(session_key, None)
                self._last_error = error_name


cache_keepalive_service = CacheKeepaliveService()
