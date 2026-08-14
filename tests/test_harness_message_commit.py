import sqlite3

import pytest

from core.harness.journal import HarnessJournal
from core.harness.models import HarnessEventKind, JournalEventDraft
from core.memory import MemoryManager


def _attach_started_step(manager, session_name):
    manager.create_session_explicit(session_name)
    journal = HarnessJournal(manager, "owner-test", clock_ms=lambda: 10_000)
    turn_id = "turn-1"
    step_id = "step-1"
    journal.create_turn(turn_id, session_name)
    journal.create_step(step_id, turn_id, 1)
    return journal, turn_id, step_id


def _started_step(tmp_path):
    manager = MemoryManager(str(tmp_path / "atomic.db"))
    journal, turn_id, step_id = _attach_started_step(manager, "session")
    return manager, journal, turn_id, step_id


def _committed_event(kind, step_id, message_index):
    return JournalEventDraft(
        kind,
        step_id=step_id,
        message_index=message_index,
    )


def test_message_ids_and_committed_events_share_one_transaction(tmp_path):
    manager, journal, turn_id, step_id = _started_step(tmp_path)
    messages = [
        ("assistant", {"content": "a"}),
        (
            "tool",
            {"tool_call_id": "c1", "name": "read_file", "content": "r"},
        ),
    ]

    ids = journal.commit_messages(
        "session",
        turn_id,
        step_id,
        messages,
        [
            _committed_event(
                HarnessEventKind.ASSISTANT_COMMITTED,
                step_id,
                0,
            ),
            _committed_event(
                HarnessEventKind.TOOL_RESULT_COMMITTED,
                step_id,
                1,
            ),
        ],
    )

    linked = [
        row[0]
        for row in manager.conn.execute(
            "SELECT message_id FROM harness_events "
            "WHERE message_id IS NOT NULL ORDER BY sequence"
        )
    ]
    assert tuple(linked) == ids
    assert manager.conn.execute(
        "SELECT DISTINCT created_at_ms FROM harness_events"
    ).fetchall() == [(10_000,)]
    assert manager.get_history("session") == [
        {"role": "assistant", "content": "a"},
        {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "read_file",
            "content": "r",
        },
    ]


def test_event_insert_failure_rolls_back_message_batch(tmp_path):
    manager = MemoryManager(str(tmp_path / "atomic.db"))
    journal, turn_id, step_id = _attach_started_step(manager, "session")
    manager.conn.execute(
        "CREATE TRIGGER fail_harness_event BEFORE INSERT ON harness_events "
        "BEGIN SELECT RAISE(ABORT, 'event write failed'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="event write failed"):
        journal.commit_messages(
            "session",
            turn_id,
            step_id,
            [("assistant", "answer")],
            [
                _committed_event(
                    HarnessEventKind.ASSISTANT_COMMITTED,
                    step_id,
                    0,
                )
            ],
        )

    assert manager.get_history("session") == []
    assert manager.get_harness_turn(turn_id)["last_sequence"] == 0


@pytest.mark.parametrize("message_index", [None, True, "0", -1, 1])
def test_invalid_message_index_is_rejected_without_database_writes(
    tmp_path, message_index
):
    manager, journal, turn_id, step_id = _started_step(tmp_path)
    changes_before = manager.conn.total_changes
    journal.clock_ms = lambda: 12_000

    with pytest.raises(ValueError, match="message_index"):
        journal.commit_messages(
            "session",
            turn_id,
            step_id,
            [("assistant", "answer")],
            [
                _committed_event(
                    HarnessEventKind.ASSISTANT_COMMITTED,
                    step_id,
                    message_index,
                )
            ],
        )

    assert manager.conn.total_changes == changes_before
    assert manager.get_history("session") == []


def test_mismatched_step_is_rejected_without_database_writes(tmp_path):
    manager, journal, turn_id, step_id = _started_step(tmp_path)
    changes_before = manager.conn.total_changes
    journal.clock_ms = lambda: 12_000

    with pytest.raises(ValueError, match="step_id"):
        journal.commit_messages(
            "session",
            turn_id,
            step_id,
            [("assistant", "answer")],
            [
                _committed_event(
                    HarnessEventKind.ASSISTANT_COMMITTED,
                    "another-step",
                    0,
                )
            ],
        )

    assert manager.conn.total_changes == changes_before
    assert manager.get_history("session") == []


def test_clear_session_removes_turn_journal(tmp_path):
    manager, journal, turn_id, step_id = _started_step(tmp_path)
    journal.commit_messages(
        "session",
        turn_id,
        step_id,
        [("assistant", "answer")],
        [
            _committed_event(
                HarnessEventKind.ASSISTANT_COMMITTED,
                step_id,
                0,
            )
        ],
    )

    manager.clear_session_messages("session")

    assert manager.get_history("session") == []
    assert manager.get_harness_turn(turn_id) is None


def test_delete_session_cascades_turn_journal(tmp_path):
    manager, journal, turn_id, step_id = _started_step(tmp_path)
    journal.commit_messages(
        "session",
        turn_id,
        step_id,
        [("assistant", "answer")],
        [
            _committed_event(
                HarnessEventKind.ASSISTANT_COMMITTED,
                step_id,
                0,
            )
        ],
    )

    manager.delete_session("session")

    assert manager.get_history("session") == []
    assert manager.get_harness_turn(turn_id) is None


def test_rename_session_keeps_turn_attached_by_session_id(tmp_path):
    manager, journal, turn_id, _step_id = _started_step(tmp_path)
    session_id_before = manager.get_harness_turn(turn_id)["session_id"]

    assert manager.rename_session("session", "renamed") == (True, "renamed")

    turn = manager.get_harness_turn(turn_id)
    assert turn["session_id"] == session_id_before
    assert manager.conn.execute(
        "SELECT sessions.session_name FROM harness_turns "
        "JOIN sessions ON sessions.id = harness_turns.session_id "
        "WHERE harness_turns.turn_id = ?",
        (turn_id,),
    ).fetchone() == ("renamed",)


def test_undo_keeps_event_but_nulls_deleted_message_reference(tmp_path):
    manager, journal, turn_id, step_id = _started_step(tmp_path)
    manager.add_message("session", "user", "question")
    journal.commit_messages(
        "session",
        turn_id,
        step_id,
        [("assistant", "answer")],
        [
            _committed_event(
                HarnessEventKind.ASSISTANT_COMMITTED,
                step_id,
                0,
            )
        ],
    )

    manager.undo_last_interaction("session")

    row = manager.conn.execute(
        "SELECT message_id FROM harness_events WHERE kind='assistant_committed'"
    ).fetchone()
    assert row == (None,)
