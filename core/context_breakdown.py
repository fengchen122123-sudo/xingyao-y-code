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
_breakdowns_by_session: dict[str, dict[str, int]] = {}


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


def set_breakdown(items: list[dict], session_id: str | None = None) -> None:
    """整体替换快照（每项 {"source": str, "chars": int}）。

    非法项被过滤；items 为 None / 不可迭代时替换为空快照。
    传 session_id 时按会话隔离存储；不传时写入全局默认快照（兼容单会话/测试）。
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
        sid = str(session_id or "").strip()
        with _lock:
            if sid:
                _breakdowns_by_session[sid] = normalized
            _breakdown.clear()
            _breakdown.update(normalized)
    except Exception:
        logger.debug("set_breakdown 异常（旁路，已忽略）", exc_info=True)


def update_source(source: str, chars: int, session_id: str | None = None) -> None:
    try:
        pair = _normalize_item({"source": source, "chars": chars})
        if pair is None:
            return
        sid = str(session_id or "").strip()
        with _lock:
            if sid:
                target = _breakdowns_by_session.setdefault(sid, {})
                target[pair[0]] = pair[1]
            _breakdown[pair[0]] = pair[1]
    except Exception:
        logger.debug("update_source 异常（旁路，已忽略）", exc_info=True)


def get_breakdown(session_id: str | None = None) -> list[dict]:
    """返回快照副本（按白名单固定顺序）；调用方修改不得影响内部状态。"""
    try:
        sid = str(session_id or "").strip()
        with _lock:
            if sid and sid in _breakdowns_by_session:
                snapshot = dict(_breakdowns_by_session[sid])
            else:
                snapshot = dict(_breakdown)
        return [
            {"source": source, "chars": snapshot[source]}
            for source in VALID_SOURCES
            if source in snapshot
        ]
    except Exception:
        logger.debug("get_breakdown 异常（旁路，已忽略）", exc_info=True)
        return []


def clear_breakdown(session_id: str | None = None, *, clear_global: bool = True) -> None:
    """清空快照（切会话 / 清空历史等与 token 记账同生命周期的清理点调用）。"""
    try:
        sid = str(session_id or "").strip()
        with _lock:
            if sid:
                _breakdowns_by_session[sid] = {}
                if clear_global:
                    _breakdown.clear()
            else:
                _breakdown.clear()
                _breakdowns_by_session.clear()
    except Exception:
        logger.debug("clear_breakdown 异常（旁路，已忽略）", exc_info=True)


def rename_breakdown_session(old_name: str, new_name: str) -> None:
    """会话改名时迁移占比快照，避免残留旧名或丢失新名快照。"""
    try:
        old_sid = str(old_name or "").strip()
        new_sid = str(new_name or "").strip()
        if not old_sid or not new_sid or old_sid == new_sid:
            return
        with _lock:
            if old_sid in _breakdowns_by_session:
                _breakdowns_by_session[new_sid] = _breakdowns_by_session.pop(old_sid)
    except Exception:
        logger.debug("rename_breakdown_session 异常（旁路，已忽略）", exc_info=True)
