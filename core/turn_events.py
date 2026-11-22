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

    def __init__(self, turn_id: str):
        if not turn_id:
            raise ValueError("turn_id must not be empty")
        self.turn_id = turn_id
        self._sequence = 0
        self._sequence_lock = threading.Lock()

    def emit(self, kind: str, payload: Mapping[str, Any] | None = None) -> TurnEvent:
        if not kind:
            raise ValueError("event kind must not be empty")
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        try:
            snapshot = deepcopy(dict(payload or {}))
        except Exception as exc:
            raise TypeError(
                f"TurnEvent payload deepcopy failed for event kind {kind!r}"
            ) from exc
        return TurnEvent(
            turn_id=self.turn_id,
            sequence=sequence,
            kind=kind,
            payload=MappingProxyType(snapshot),
        )
