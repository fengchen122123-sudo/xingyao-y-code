import pytest

from core.harness.models import (
    HarnessEventKind,
    InvalidHarnessTransition,
    JournalEventDraft,
    StepStatus,
    TurnStatus,
    transition_step,
    transition_turn,
)


def test_turn_terminal_state_cannot_change():
    with pytest.raises(InvalidHarnessTransition):
        transition_turn(TurnStatus.COMPLETED, TurnStatus.RUNNING)


def test_step_tool_path_is_explicit():
    state = transition_step(StepStatus.ASSISTANT_READY, StepStatus.TOOLS_RUNNING)
    assert transition_step(state, StepStatus.TOOLS_COMMITTED) is StepStatus.TOOLS_COMMITTED


def test_journal_event_rejects_raw_payload():
    with pytest.raises(TypeError):
        JournalEventDraft(kind=HarnessEventKind.TOOL_STARTED, payload={"secret": "x"})
