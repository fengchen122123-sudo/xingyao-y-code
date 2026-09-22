"""输入框上下文用量面板：上下文来源字符占比快照（纯旁路统计）。

设计规格：`待开发/输入框上下文用量面板/设计规格.md` §3.3/§3.4。

口径为**字符数估算**，仅用于 GUI 悬停面板展示来源占比，不进 token 账本。
所有函数遵循旁路原则：内部 try/except 吞掉一切异常、非法输入静默过滤，
统计绝不打扰对话主流程；快照写入与读取均持锁，GUI/后台线程并发安全。

来源白名单与 `get_breakdown` 的稳定输出顺序一致：
messages（对话历史）/ system_prompt（系统提示词）/ skills（技能目录）/
runtime_context（运行上下文）/ tools（工具定义）。
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

#: 允许的来源；元组顺序即 get_breakdown 的稳定输出顺序（前端按此顺序渲染）
VALID_SOURCES: tuple[str, ...] = (
    "messages",
    "system_prompt",
    "skills",
    "runtime_context",
    "tools",
)

_lock = threading.Lock()
_breakdown: dict[str, int] = {}


def _normalize_item(item) -> tuple[str, int] | None:
    """把单项规范化为 (source, chars)；任何非法项返回 None（过滤）。"""
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
    """更新/追加单个来源的字符数；非法 source/chars 静默忽略。"""
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
