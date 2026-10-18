"""断点续跑判定层：纯函数，零 I/O、零项目内依赖。

判定顺序即安全优先级：任何"不确定"一律先 BLOCK，绝不猜。
本模块只回答"这一步能不能自动重放"，不做任何执行。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ReplayDecision(str, Enum):
    """续跑时对单个工具调用的处置方式。"""

    REPLAY = "replay"              # 只读 + 已完成 + 哈希一致 → 回灌结果，不重新执行
    REPLAY_FRESH = "replay_fresh"  # 只读但结果哈希不符 → 允许重新执行该只读调用
    CONFIRM = "confirm"            # 写操作，需用户逐项确认
    BLOCK = "block"                # 绝不自动执行


SCOPE_NONE = "none"
UNTRUSTED_SOURCES = frozenset({"plugin", "mcp"})
# 声明 scope=none 但实际出网的黑名单：scope 声明不可信，按真实副作用归类。
KNOWN_NETWORK_SIDE_EFFECT_TOOLS = frozenset({"get_weather"})


@dataclass(frozen=True)
class ToolReplayFacts:
    """判定所需的全部事实。由调用方从 journal / 注册表 / 权限层收集。"""

    tool_name: str
    side_effect_scope: str
    read_only: bool
    source: str = "builtin"            # builtin | plugin | mcp | secretary
    started: bool = True
    finished: bool = False
    result_hash_ok: bool = True
    tool_available: bool = True
    hash_anchors_ok: bool = True
    permission_narrowed: bool = False
    session_archived: bool = False
    undo_recovering: bool = False


def _text(value: Any) -> str:
    try:
        return str(value or "").strip().lower()
    except Exception:
        return ""


def _flag(facts: Any, name: str, default: bool = False) -> bool:
    try:
        return bool(getattr(facts, name, default))
    except Exception:
        return default


def _is_pure_read(scope: str, read_only: bool) -> bool:
    return scope == SCOPE_NONE and bool(read_only)


def decide_replay(facts: Any) -> tuple[ReplayDecision, str]:
    """返回 (决策, 原因码)。对任何输入都不抛异常。"""
    try:
        scope = _text(getattr(facts, "side_effect_scope", ""))
        read_only = _flag(facts, "read_only")
        source = _text(getattr(facts, "source", "builtin")) or "builtin"
        tool_name = _text(getattr(facts, "tool_name", ""))
        started = _flag(facts, "started", True)
        finished = _flag(facts, "finished")

        if _flag(facts, "session_archived"):
            return ReplayDecision.BLOCK, "session_archived"
        if _flag(facts, "undo_recovering"):
            return ReplayDecision.BLOCK, "undo_recovering"
        if not _flag(facts, "hash_anchors_ok", True):
            return ReplayDecision.BLOCK, "hash_anchor_mismatch"
        if not _flag(facts, "tool_available", True):
            return ReplayDecision.BLOCK, "tool_unavailable"
        # 只有"已开始但未完成"才是不确定态；从未开始不算。
        if started and not finished:
            return ReplayDecision.BLOCK, "uncertain_state"
        if source in UNTRUSTED_SOURCES:
            return ReplayDecision.BLOCK, "untrusted_scope"
        if tool_name in KNOWN_NETWORK_SIDE_EFFECT_TOOLS:
            return ReplayDecision.BLOCK, "known_network_side_effect"
        if _flag(facts, "permission_narrowed") and not _is_pure_read(scope, read_only):
            return ReplayDecision.BLOCK, "permission_narrowed"
        if _is_pure_read(scope, read_only):
            if _flag(facts, "result_hash_ok", True):
                return ReplayDecision.REPLAY, "read_replay"
            return ReplayDecision.REPLAY_FRESH, "read_hash_mismatch"
        # workspace / git / network / system 以及未知 scope 一律走人工确认。
        return ReplayDecision.CONFIRM, "write_requires_confirmation"
    except Exception:
        return ReplayDecision.CONFIRM, "write_requires_confirmation"


_BUCKETS = {
    ReplayDecision.REPLAY: "replay",
    ReplayDecision.REPLAY_FRESH: "fresh",
    ReplayDecision.CONFIRM: "confirm",
    ReplayDecision.BLOCK: "block",
}


def plan_step_resume(facts_list: Any) -> dict:
    """按决策分组，元素为 (ordinal, tool_name, reason)，保持入参顺序。"""
    groups: dict[str, list] = {"replay": [], "fresh": [], "confirm": [], "block": []}
    for item in facts_list or []:
        try:
            ordinal, facts = item
        except Exception:
            continue
        decision, reason = decide_replay(facts)
        try:
            name = str(getattr(facts, "tool_name", ""))
        except Exception:
            name = ""
        groups[_BUCKETS[decision]].append((ordinal, name, reason))
    return groups
