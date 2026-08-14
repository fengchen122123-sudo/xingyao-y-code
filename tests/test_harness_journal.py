import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.harness.journal import HarnessJournal
from core.harness.models import (
    HarnessEventKind,
    JournalEventDraft,
    StepStatus,
    TurnStatus,
)
from core.memory import MemoryManager


def _journal(tmp_path, now_ms):
    manager = MemoryManager(str(tmp_path / "journal.db"))
    manager.create_session_explicit("session")
    journal = HarnessJournal(manager, "owner-test", clock_ms=lambda: now_ms)
    journal.heartbeat()
    journal.create_turn("turn-1", "session")
    return manager, journal


def test_harness_tables_are_additive(tmp_path):
    manager = MemoryManager(str(tmp_path / "journal.db"))
    names = {
        row[0]
        for row in manager.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"harness_owners", "harness_turns", "harness_steps", "harness_events"} <= names


def test_existing_messages_are_not_backfilled_as_harness_turns(tmp_path):
    path = tmp_path / "upgrade.db"
    before = MemoryManager(str(path))
    before.add_message("legacy", "user", "old message")
    before.close()

    after = MemoryManager(str(path))
    assert after.get_history("legacy") == [{"role": "user", "content": "old message"}]
    assert after.conn.execute("SELECT COUNT(*) FROM harness_turns").fetchone() == (0,)


def test_event_table_has_no_generic_or_raw_content_columns(tmp_path):
    manager, _journal_instance = _journal(tmp_path, now_ms=10_000)
    columns = {
        row[1] for row in manager.conn.execute("PRAGMA table_info(harness_events)")
    }
    assert {"payload", "content", "arguments", "result", "reasoning"}.isdisjoint(columns)


def test_parallel_event_appends_allocate_unique_monotonic_sequences(tmp_path):
    _manager, journal = _journal(tmp_path, now_ms=10_000)
    barrier = threading.Barrier(8)

    def append(index):
        barrier.wait()
        return journal.append_event(
            "turn-1",
            JournalEventDraft(
                kind=HarnessEventKind.TOOL_STARTED,
                tool_name=f"tool-{index}",
                call_ordinal=index,
            ),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(append, range(8)))
    assert sorted(sequences) == list(range(1, 9))


def test_transition_rejects_stale_expected_status(tmp_path):
    manager, journal = _journal(tmp_path, now_ms=10_000)
    journal.transition_turn("turn-1", TurnStatus.CREATED, TurnStatus.PREPARING)
    with pytest.raises(RuntimeError, match="stale harness turn transition"):
        journal.transition_turn("turn-1", TurnStatus.CREATED, TurnStatus.RUNNING)
    assert manager.get_harness_turn("turn-1")["status"] == "preparing"


def test_step_transition_rejects_stale_expected_status(tmp_path):
    manager, journal = _journal(tmp_path, now_ms=10_000)
    journal.create_step("step-1", "turn-1", 1)
    journal.transition_step("step-1", StepStatus.CREATED, StepStatus.REQUESTING)
    with pytest.raises(RuntimeError, match="stale harness step transition"):
        journal.transition_step("step-1", StepStatus.CREATED, StepStatus.FAILED)
    assert manager.get_harness_step("step-1")["status"] == "requesting"


def test_append_event_rejects_unresolved_message_index(tmp_path):
    _manager, journal = _journal(tmp_path, now_ms=10_000)
    draft = JournalEventDraft(
        kind=HarnessEventKind.ASSISTANT_COMMITTED,
        message_index=0,
    )
    with pytest.raises(ValueError, match="message_index"):
        journal.append_event("turn-1", draft)


def test_only_expired_owner_turns_become_interrupted(tmp_path):
    manager = MemoryManager(str(tmp_path / "lease.db"))
    manager.create_session_explicit("session")
    live = HarnessJournal(manager, "owner-live", clock_ms=lambda: 19_500)
    dead = HarnessJournal(manager, "owner-dead", clock_ms=lambda: 1_000)
    live.create_turn("turn-live", "session")
    dead.create_turn("turn-dead", "session")

    live.recover_stale(now_ms=20_000)

    assert manager.get_harness_turn("turn-live")["status"] == "created"
    assert manager.get_harness_turn("turn-dead")["status"] == "interrupted"


def test_recovery_interrupts_steps_before_their_turn(tmp_path):
    manager = MemoryManager(str(tmp_path / "lease.db"))
    manager.create_session_explicit("session")
    dead = HarnessJournal(manager, "owner-dead", clock_ms=lambda: 1_000)
    dead.create_turn("turn-dead", "session")
    dead.create_step("step-dead", "turn-dead", 1)

    assert dead.recover_stale(now_ms=20_000) == 2
    assert manager.get_harness_step("step-dead")["status"] == "interrupted"
    assert manager.get_harness_turn("turn-dead")["status"] == "interrupted"
    assert manager.conn.execute("SELECT COUNT(*) FROM harness_events").fetchone() == (0,)


def test_heartbeat_is_throttled_for_two_seconds(tmp_path):
    manager = MemoryManager(str(tmp_path / "lease.db"))
    now = [1_000]
    journal = HarnessJournal(manager, "owner-test", clock_ms=lambda: now[0])
    first_heartbeat = manager.conn.execute(
        "SELECT heartbeat_at_ms FROM harness_owners WHERE owner_id = 'owner-test'"
    ).fetchone()

    now[0] = 2_999
    journal.heartbeat()
    throttled_heartbeat = manager.conn.execute(
        "SELECT heartbeat_at_ms FROM harness_owners WHERE owner_id = 'owner-test'"
    ).fetchone()
    now[0] = 3_000
    journal.heartbeat()
    refreshed_heartbeat = manager.conn.execute(
        "SELECT heartbeat_at_ms FROM harness_owners WHERE owner_id = 'owner-test'"
    ).fetchone()

    assert first_heartbeat == (1_000,)
    assert throttled_heartbeat == (1_000,)
    assert refreshed_heartbeat == (3_000,)
