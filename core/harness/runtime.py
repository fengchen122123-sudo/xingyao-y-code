"""In-process and durable lifecycle coordination for one model turn."""

from __future__ import annotations

import threading
from collections.abc import Callable

from core.harness.journal import HarnessJournal, PROCESS_OWNER_ID
from core.harness.models import (
    HarnessEventKind,
    JournalEventDraft,
    StepStatus,
    TurnStatus,
    transition_step,
    transition_turn,
)
from core.harness.request_envelope import RequestEnvelope
from core.memory import MemoryManager


_STEP_TERMINAL = frozenset(
    {
        StepStatus.COMPLETED,
        StepStatus.CANCELLED,
        StepStatus.FAILED,
        StepStatus.INTERRUPTED,
    }
)
_TURN_TERMINAL = frozenset(
    {
        TurnStatus.COMPLETED,
        TurnStatus.CANCELLED,
        TurnStatus.FAILED,
        TurnStatus.INTERRUPTED,
    }
)


class TurnRuntime:
    """Own the strict state machine and optional Journal for one turn."""

    def __init__(
        self,
        turn_id: str,
        session_name: str | None,
        *,
        persist: bool,
        manager: MemoryManager | None,
        cancel_event: threading.Event | None,
        clock_ms: Callable[[], int],
        journal: HarnessJournal | None,
    ):
        self.turn_id = turn_id
        self.session_name = session_name
        self.persist = persist
        self.manager = manager
        self.cancel_event = cancel_event
        self.clock_ms = clock_ms
        self.journal = journal
        self.status = TurnStatus.CREATED
        self.current_step: StepRuntime | None = None
        self._ordinal = 0

    @classmethod
    def start(
        cls,
        turn_id: str,
        session_name: str | None,
        *,
        persist: bool,
        manager: MemoryManager | None,
        cancel_event: threading.Event | None,
        clock_ms: Callable[[], int],
    ) -> "TurnRuntime":
        if not turn_id:
            raise ValueError("turn_id must not be empty")
        if persist and manager is None:
            raise RuntimeError("persistent harness runtime requires a manager")
        if persist and not session_name:
            raise RuntimeError("persistent harness runtime requires a session")

        journal = (
            HarnessJournal(manager, PROCESS_OWNER_ID, clock_ms)
            if persist and manager is not None
            else None
        )
        runtime = cls(
            turn_id,
            session_name,
            persist=persist,
            manager=manager,
            cancel_event=cancel_event,
            clock_ms=clock_ms,
            journal=journal,
        )
        if journal is not None:
            journal.create_turn(turn_id, session_name)
            journal.append_event(
                turn_id,
                JournalEventDraft(HarnessEventKind.TURN_CREATED),
            )
        runtime._transition(TurnStatus.PREPARING)
        return runtime

    def heartbeat(self) -> bool:
        return False if self.journal is None else self.journal.heartbeat()

    def _append_status_event(self, *, error_code: str | None = None) -> None:
        if self.journal is not None:
            self.journal.append_event(
                self.turn_id,
                JournalEventDraft(
                    HarnessEventKind.TURN_STATUS,
                    error_code=error_code,
                ),
            )

    def _transition(
        self,
        target: TurnStatus,
        *,
        error_code: str | None = None,
    ) -> None:
        current = self.status
        transition_turn(current, target)
        if self.journal is not None:
            self.journal.transition_turn(
                self.turn_id,
                current,
                target,
                error_code=error_code,
            )
        self.status = target
        self._append_status_event(error_code=error_code)

    def mark_running(self) -> None:
        self._transition(TurnStatus.RUNNING)

    def begin_step(self) -> "StepRuntime":
        if self.status is not TurnStatus.RUNNING:
            raise RuntimeError("a harness step requires a running turn")
        if self.current_step is not None and self.current_step.status not in _STEP_TERMINAL:
            raise RuntimeError("the previous harness step is still active")
        self._ordinal += 1
        step = StepRuntime(
            self,
            f"{self.turn_id}:step:{self._ordinal}",
            self._ordinal,
        )
        if self.journal is not None:
            self.journal.create_step(step.step_id, self.turn_id, step.ordinal)
            self.journal.append_event(
                self.turn_id,
                JournalEventDraft(
                    HarnessEventKind.STEP_CREATED,
                    step_id=step.step_id,
                ),
            )
        self.current_step = step
        step._transition(StepStatus.REQUESTING)
        return step

    def complete(self) -> None:
        if self.current_step is not None and self.current_step.status not in _STEP_TERMINAL:
            raise RuntimeError("cannot complete a turn with an active step")
        self._transition(TurnStatus.COMPLETED)

    def cancel(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
        if self.current_step is not None and self.current_step.status not in _STEP_TERMINAL:
            self.current_step.cancel()
        if self.status in _TURN_TERMINAL:
            return
        if self.status is not TurnStatus.CANCELLING:
            self._transition(TurnStatus.CANCELLING)
        self._transition(TurnStatus.CANCELLED)

    def fail(self, code: str) -> None:
        if self.status in _TURN_TERMINAL:
            return
        if self.current_step is not None and self.current_step.status not in _STEP_TERMINAL:
            self.current_step.fail(code)
        self._transition(TurnStatus.FAILED, error_code=code)

    def interrupt(self) -> None:
        if self.status in _TURN_TERMINAL:
            return
        if self.current_step is not None and self.current_step.status not in _STEP_TERMINAL:
            self.current_step.interrupt()
        self._transition(TurnStatus.INTERRUPTED)


class StepRuntime:
    """State and request metadata for one outer model/tool loop."""

    def __init__(self, turn: TurnRuntime, step_id: str, ordinal: int):
        self.turn = turn
        self.step_id = step_id
        self.ordinal = ordinal
        self.status = StepStatus.CREATED
        self.request: RequestEnvelope | None = None

    def _append_status_event(self, *, error_code: str | None = None) -> None:
        if self.turn.journal is not None:
            self.turn.journal.append_event(
                self.turn.turn_id,
                JournalEventDraft(
                    HarnessEventKind.STEP_STATUS,
                    step_id=self.step_id,
                    error_code=error_code,
                ),
            )

    def _transition(
        self,
        target: StepStatus,
        *,
        error_code: str | None = None,
    ) -> None:
        current = self.status
        transition_step(current, target)
        if self.turn.journal is not None:
            duration_ms = None
            if target in _STEP_TERMINAL:
                duration_ms = max(
                    0,
                    int(self.turn.clock_ms())
                    - int(
                        self.turn.manager.get_harness_step(self.step_id)[
                            "started_at_ms"
                        ]
                    ),
                )
            self.turn.journal.transition_step(
                self.step_id,
                current,
                target,
                duration_ms=duration_ms,
                error_code=error_code,
            )
        self.status = target
        self._append_status_event(error_code=error_code)

    def capture_request(self, envelope: RequestEnvelope) -> None:
        if self.status is not StepStatus.REQUESTING:
            raise RuntimeError("request capture requires a requesting step")
        if not isinstance(envelope, RequestEnvelope):
            raise TypeError("envelope must be a RequestEnvelope")
        if self.request is not None and envelope.attempt <= self.request.attempt:
            raise ValueError("request attempts must increase within a step")

        if self.turn.manager is not None:
            fingerprint = envelope.fingerprint
            with self.turn.manager.acquire_lock():
                cursor = self.turn.manager.conn.cursor()
                try:
                    cursor.execute(
                        "UPDATE harness_steps SET protocol = ?, cache_scope = ?, "
                        "messages_sha256 = ?, system_sha256 = ?, tools_sha256 = ?, "
                        "options_sha256 = ?, cache_lane_sha256 = ?, message_count = ?, "
                        "system_message_count = ?, request_bytes = ?, system_bytes = ?, "
                        "tools_bytes = ?, options_bytes = ?, request_reason = ?, "
                        "attempt_count = ?, deepseek_generation_id = ? "
                        "WHERE step_id = ? AND status = 'requesting'",
                        (
                            envelope.protocol,
                            envelope.cache_scope,
                            fingerprint.messages_sha256,
                            fingerprint.system_sha256,
                            fingerprint.tools_sha256,
                            fingerprint.options_sha256,
                            fingerprint.cache_lane_sha256,
                            fingerprint.message_count,
                            fingerprint.system_message_count,
                            fingerprint.request_bytes,
                            fingerprint.system_bytes,
                            fingerprint.tools_bytes,
                            fingerprint.options_bytes,
                            envelope.request_reason,
                            envelope.attempt,
                            envelope.deepseek_generation_id,
                            self.step_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            f"stale harness request capture: {self.step_id}"
                        )
                    self.turn.manager.conn.commit()
                except Exception:
                    self.turn.manager.conn.rollback()
                    raise
            self.turn.journal.append_event(
                self.turn.turn_id,
                JournalEventDraft(
                    HarnessEventKind.REQUEST_CAPTURED,
                    step_id=self.step_id,
                ),
            )
        self.request = envelope

    def mark_streaming(self) -> None:
        self._transition(StepStatus.STREAMING)

    def mark_assistant_ready(self) -> None:
        self._transition(StepStatus.ASSISTANT_READY)

    def mark_tools_running(self) -> None:
        self._transition(StepStatus.TOOLS_RUNNING)

    def mark_tools_committed(self) -> None:
        self._transition(StepStatus.TOOLS_COMMITTED)

    def complete(self) -> None:
        self._transition(StepStatus.COMPLETED)

    def cancel(self) -> None:
        self._transition(StepStatus.CANCELLED)

    def fail(self, code: str) -> None:
        self._transition(StepStatus.FAILED, error_code=code)

    def interrupt(self) -> None:
        self._transition(StepStatus.INTERRUPTED)

    def commit_messages(self, messages: list[tuple[str, object]]) -> tuple[int, ...]:
        if not messages or self.turn.journal is None:
            return ()
        events = []
        for index, (role, _content) in enumerate(messages):
            if role == "assistant":
                kind = HarnessEventKind.ASSISTANT_COMMITTED
            elif role == "tool":
                kind = HarnessEventKind.TOOL_RESULT_COMMITTED
            else:
                continue
            events.append(
                JournalEventDraft(
                    kind,
                    step_id=self.step_id,
                    message_index=index,
                )
            )
        return self.turn.journal.commit_messages(
            self.turn.session_name,
            self.turn.turn_id,
            self.step_id,
            messages,
            events,
        )
