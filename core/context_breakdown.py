from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

VALID_SOURCES: tuple[str, ...] = (
    "messages",
    "system_prompt",
    "skills",
    "runtime_context",
    "system_tools",
    "mcp_tools",
)

_lock = threading.Lock()
_breakdown: dict[str, int] = {}


def _normalize_item(item) -> tuple[str, int] | None:
    if not isinstance(item, dict):
        return None
    source = item.get("source")
    if not isinstance(source, str) or source not in VALID_SOURCES:
        return None
    try:
        # chars 必须可整数化且非负：负数/None/非数字字符串一律过滤；
        # 浮点按 int() 截断（“非负整数化”）。
        chars = int(item.get("chars"))
    except Exception:
        return None
    if chars < 0:
        return None
    return source, chars


def set_breakdown(items: list[dict]) -> None:
    """整体替换快照（每项 {"source": str, "chars": int}）。

    非法项被过滤；items 为 None / 不可迭代时替换为空快照。
    任何输入异常均被吞掉，绝不抛出。
    """
    try:
        try:
            iterable = list(items) if items is not None else []
        except Exception:
            iterable = []
        normalized: dict[str, int] = {}
        for item in iterable:
            pair = _normalize_item(item)
            if pair is not None:
                normalized[pair[0]] = pair[1]
        with _lock:
            _breakdown.clear()
            _breakdown.update(normalized)
    except Exception:
        logger.debug("set_breakdown 异常（旁路，已忽略）", exc_info=True)


def update_source(source: str, chars: int) -> None:
    try:
        pair = _normalize_item({"source": source, "chars": chars})
        if pair is None:
            return
        with _lock:
            _breakdown[pair[0]] = pair[1]
    except Exception:
        logger.debug("update_source 异常（旁路，已忽略）", exc_info=True)


def get_breakdown() -> list[dict]:
    """返回快照副本（按白名单固定顺序）；调用方修改不得影响内部状态。"""
    try:
        with _lock:
            snapshot = dict(_breakdown)
        return [
            {"source": source, "chars": snapshot[source]}
            for source in VALID_SOURCES
            if source in snapshot
        ]
    except Exception:
        logger.debug("get_breakdown 异常（旁路，已忽略）", exc_info=True)
        return []


def clear_breakdown() -> None:
    """清空快照（切会话 / 清空历史等与 token 记账同生命周期的清理点调用）。"""
    try:
        with _lock:
            _breakdown.clear()
    except Exception:
        logger.debug("clear_breakdown 异常（旁路，已忽略）", exc_info=True)
