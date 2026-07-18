"""Structured events emitted while an agent turn is running."""

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

    def emit(self, kind: str, payload: Mapping[str, Any] | None = None) -> TurnEvent:
        if not kind:
            raise ValueError("event kind must not be empty")
        self._sequence += 1
        snapshot = deepcopy(dict(payload or {}))
        return TurnEvent(
            turn_id=self.turn_id,
            sequence=self._sequence,
            kind=kind,
            payload=MappingProxyType(snapshot),
        )
