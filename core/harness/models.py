"""Immutable models and explicit state transitions for the harness kernel."""

from dataclasses import dataclass
from enum import Enum


class TurnStatus(str, Enum):
    CREATED = "created"
    PREPARING = "preparing"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class StepStatus(str, Enum):
    CREATED = "created"
    REQUESTING = "requesting"
    STREAMING = "streaming"
    ASSISTANT_READY = "assistant_ready"
    TOOLS_RUNNING = "tools_running"
    TOOLS_COMMITTED = "tools_committed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class HarnessEventKind(str, Enum):
    TURN_CREATED = "turn_created"
    TURN_STATUS = "turn_status"
    STEP_CREATED = "step_created"
    STEP_STATUS = "step_status"
    REQUEST_CAPTURED = "request_captured"
    ASSISTANT_COMMITTED = "assistant_committed"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    TOOL_RESULT_COMMITTED = "tool_result_committed"
    TURN_ERROR = "turn_error"


class InvalidHarnessTransition(ValueError):
    """Raised when a harness state machine is asked to make an illegal move."""


_TURN_TRANSITIONS: dict[TurnStatus, frozenset[TurnStatus]] = {
    TurnStatus.CREATED: frozenset({TurnStatus.PREPARING}),
    TurnStatus.PREPARING: frozenset(
        {TurnStatus.RUNNING, TurnStatus.CANCELLING, TurnStatus.FAILED, TurnStatus.INTERRUPTED}
    ),
    TurnStatus.RUNNING: frozenset(
        {
            TurnStatus.CANCELLING,
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
            TurnStatus.INTERRUPTED,
        }
    ),
    TurnStatus.CANCELLING: frozenset({TurnStatus.CANCELLED}),
    TurnStatus.COMPLETED: frozenset(),
    TurnStatus.CANCELLED: frozenset(),
    TurnStatus.FAILED: frozenset(),
    TurnStatus.INTERRUPTED: frozenset(),
}

_STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.CREATED: frozenset(
        {StepStatus.REQUESTING, StepStatus.CANCELLED, StepStatus.FAILED, StepStatus.INTERRUPTED}
    ),
    StepStatus.REQUESTING: frozenset(
        {StepStatus.STREAMING, StepStatus.CANCELLED, StepStatus.FAILED, StepStatus.INTERRUPTED}
    ),
    StepStatus.STREAMING: frozenset(
        {
            StepStatus.ASSISTANT_READY,
            StepStatus.CANCELLED,
            StepStatus.FAILED,
            StepStatus.INTERRUPTED,
        }
    ),
    StepStatus.ASSISTANT_READY: frozenset(
        {
            StepStatus.TOOLS_RUNNING,
            StepStatus.COMPLETED,
            StepStatus.CANCELLED,
            StepStatus.FAILED,
            StepStatus.INTERRUPTED,
        }
    ),
    StepStatus.TOOLS_RUNNING: frozenset(
        {
            StepStatus.TOOLS_COMMITTED,
            StepStatus.CANCELLED,
            StepStatus.FAILED,
            StepStatus.INTERRUPTED,
        }
    ),
    StepStatus.TOOLS_COMMITTED: frozenset(
        {StepStatus.COMPLETED, StepStatus.CANCELLED, StepStatus.FAILED, StepStatus.INTERRUPTED}
    ),
    StepStatus.COMPLETED: frozenset(),
    StepStatus.CANCELLED: frozenset(),
    StepStatus.FAILED: frozenset(),
    StepStatus.INTERRUPTED: frozenset(),
}


def _state_value(state: object) -> object:
    return state.value if isinstance(state, Enum) else state


def transition_turn(current: TurnStatus, target: TurnStatus) -> TurnStatus:
    """Validate and apply one explicitly allowed turn status transition."""
    if target not in _TURN_TRANSITIONS.get(current, frozenset()):
        raise InvalidHarnessTransition(
            f"Invalid turn transition: {_state_value(current)} -> {_state_value(target)}"
        )
    return target


def transition_step(current: StepStatus, target: StepStatus) -> StepStatus:
    """Validate and apply one explicitly allowed step status transition."""
    if target not in _STEP_TRANSITIONS.get(current, frozenset()):
        raise InvalidHarnessTransition(
            f"Invalid step transition: {_state_value(current)} -> {_state_value(target)}"
        )
    return target


@dataclass(frozen=True)
class JournalEventDraft:
    kind: HarnessEventKind
    step_id: str | None = None
    message_index: int | None = None
    tool_name: str | None = None
    call_ordinal: int | None = None
    arguments_sha256: str | None = None
    result_sha256: str | None = None
    duration_ms: int | None = None
    result_status: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class RequestFingerprint:
    messages_sha256: str
    system_sha256: str
    tools_sha256: str
    options_sha256: str
    cache_lane_sha256: str
    message_count: int
    system_message_count: int
    request_bytes: int
    system_bytes: int
    tools_bytes: int
    options_bytes: int
