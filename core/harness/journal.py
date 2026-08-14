"""Durable harness lifecycle journal and process-owner leases."""

import threading
import uuid
import weakref
from collections.abc import Callable

from core.harness.models import (
    JournalEventDraft,
    StepStatus,
    TurnStatus,
    transition_step as validate_step_transition,
    transition_turn as validate_turn_transition,
)
from core.memory import MemoryManager


PROCESS_OWNER_ID = f"harness-{uuid.uuid4().hex}"

_HEARTBEAT_INTERVAL_MS = 2_000
_OWNER_STALE_AFTER_MS = 10_000


class _OwnerHeartbeatState:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_heartbeat_ms: int | None = None


_OWNER_STATES_LOCK = threading.Lock()
_OWNER_STATES = weakref.WeakKeyDictionary()


def _owner_heartbeat_state(manager: MemoryManager, owner_id: str):
    with _OWNER_STATES_LOCK:
        manager_states = _OWNER_STATES.get(manager)
        if manager_states is None:
            manager_states = {}
            _OWNER_STATES[manager] = manager_states
        state = manager_states.get(owner_id)
        if state is None:
            state = _OwnerHeartbeatState()
            manager_states[owner_id] = state
        return state


class HarnessJournal:
    def __init__(
        self,
        manager: MemoryManager,
        owner_id: str,
        clock_ms: Callable[[], int],
    ):
        self.manager = manager
        self.owner_id = owner_id
        self.clock_ms = clock_ms
        self._heartbeat_state = _owner_heartbeat_state(manager, owner_id)
        self.heartbeat()

    def heartbeat(self) -> bool:
        state = self._heartbeat_state
        with state.lock:
            now_ms = int(self.clock_ms())
            if (
                state.last_heartbeat_ms is not None
                and now_ms - state.last_heartbeat_ms < _HEARTBEAT_INTERVAL_MS
            ):
                return False
            self.manager.heartbeat_harness_owner(self.owner_id, now_ms)
            state.last_heartbeat_ms = now_ms
            self.recover_stale(now_ms=now_ms)
            return True

    def recover_stale(self, *, now_ms: int | None = None) -> int:
        current_ms = int(self.clock_ms()) if now_ms is None else int(now_ms)
        return self.manager.interrupt_stale_harness_runs(
            current_ms - _OWNER_STALE_AFTER_MS
        )

    def create_turn(self, turn_id: str, session_name: str) -> None:
        self.heartbeat()
        self.manager.create_harness_turn(
            turn_id,
            session_name,
            self.owner_id,
            int(self.clock_ms()),
        )

    def create_step(self, step_id: str, turn_id: str, ordinal: int) -> None:
        self.heartbeat()
        self.manager.create_harness_step(
            step_id,
            turn_id,
            ordinal,
            int(self.clock_ms()),
        )

    def transition_turn(
        self,
        turn_id: str,
        expected: TurnStatus,
        target: TurnStatus,
        *,
        error_code: str | None = None,
    ) -> None:
        self.heartbeat()
        current = self.manager.get_harness_turn(turn_id)
        if current is None or current["status"] != expected.value:
            raise RuntimeError(
                f"stale harness turn transition: {turn_id} expected {expected.value}"
            )
        validate_turn_transition(expected, target)
        self.manager.transition_harness_turn(
            turn_id,
            expected.value,
            target.value,
            int(self.clock_ms()),
            error_code=error_code,
        )

    def transition_step(
        self,
        step_id: str,
        expected: StepStatus,
        target: StepStatus,
        *,
        duration_ms: int | None = None,
        finish_reason: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self.heartbeat()
        current = self.manager.get_harness_step(step_id)
        if current is None or current["status"] != expected.value:
            raise RuntimeError(
                f"stale harness step transition: {step_id} expected {expected.value}"
            )
        validate_step_transition(expected, target)
        self.manager.transition_harness_step(
            step_id,
            expected.value,
            target.value,
            int(self.clock_ms()),
            duration_ms=duration_ms,
            finish_reason=finish_reason,
            error_code=error_code,
        )

    def append_event(self, turn_id: str, draft: JournalEventDraft) -> int:
        if draft.message_index is not None:
            raise ValueError("message_index requires atomic message-id resolution")
        self.heartbeat()
        return self.manager.append_harness_event(
            turn_id,
            draft,
            int(self.clock_ms()),
        )
