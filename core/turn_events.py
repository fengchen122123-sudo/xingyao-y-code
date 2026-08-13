"""Structured events emitted while an agent turn is running."""

import logging
import threading
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class TurnEvent:
    """Immutable event snapshot delivered to the UI or another consumer."""

    turn_id: str
    sequence: int
    kind: str
    payload: Mapping[str, Any]


class TurnEventFactory:
    """Create ordered event snapshots for one turn."""

    def __init__(self, turn_id: str):
        if not turn_id:
            raise ValueError("turn_id must not be empty")
        self.turn_id = turn_id
        self._sequence = 0
        # 并行工具批次会跨线程 emit，序号自增必须串行化。
        # 注意：序号只保证唯一且单调递增，不保证跨线程的实际交付顺序
        # （先拿到序号的线程可能后把事件交给消费者）。
        self._sequence_lock = threading.Lock()

    def emit(self, kind: str, payload: Mapping[str, Any] | None = None) -> TurnEvent:
        if not kind:
            raise ValueError("event kind must not be empty")
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        # T3 修复：payload 含锁/句柄/generator 等不可深拷贝对象时，
        # deepcopy 异常会冒泡中断整个 turn；退回浅拷贝并记日志。
        try:
            snapshot = deepcopy(dict(payload or {}))
        except Exception:
            logging.getLogger("py.warnings").warning(
                "TurnEvent payload deepcopy failed (kind=%s), fallback to shallow copy",
                kind,
                exc_info=True,
            )
            snapshot = dict(payload or {})
        return TurnEvent(
            turn_id=self.turn_id,
            sequence=sequence,
            kind=kind,
            payload=MappingProxyType(snapshot),
        )
