"""Turn lifecycle state used to cancel and roll back one model turn."""

from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any


class TurnController:
    def __init__(self, messages: list[dict[str, Any]] | None = None):
        self.messages = deepcopy(messages or [])
        self.cancel_event = threading.Event()
        self._turn_start_len: int | None = None

    def begin_turn(self) -> int:
        self.cancel_event.clear()
        self._turn_start_len = len(self.messages)
        return self._turn_start_len

    def append(self, message: dict[str, Any]) -> None:
        if self._turn_start_len is None:
            raise RuntimeError("begin_turn must be called before append")
        self.messages.append(deepcopy(message))

    def cancel(self) -> None:
        self.cancel_event.set()
        if self._turn_start_len is not None:
            del self.messages[self._turn_start_len:]
        self._turn_start_len = None

    def complete(self) -> None:
        self._turn_start_len = None
