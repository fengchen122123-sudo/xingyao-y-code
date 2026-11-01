import gc
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


def test_owner_heartbeat_throttle_is_shared_per_manager_owner(tmp_path):
    now = [1_000]
    manager_a = MemoryManager(str(tmp_path / "lease-a.db"))
    manager_b = MemoryManager(str(tmp_path / "lease-b.db"))
    writes_a = []
    writes_b = []
    writes_lock = threading.Lock()
    original_a = manager_a.heartbeat_harness_owner
    original_b = manager_b.heartbeat_harness_owner

    def record_a(owner_id, now_ms):
        with writes_lock:
            writes_a.append((owner_id, now_ms))
        return original_a(owner_id, now_ms)

    def record_b(owner_id, now_ms):
        with writes_lock:
            writes_b.append((owner_id, now_ms))
        return original_b(owner_id, now_ms)

    manager_a.heartbeat_harness_owner = record_a
    manager_b.heartbeat_harness_owner = record_b
    journals = [
        HarnessJournal(manager_a, "owner-shared", clock_ms=lambda: now[0]),
        HarnessJournal(manager_a, "owner-shared", clock_ms=lambda: now[0]),
        HarnessJournal(manager_b, "owner-shared", clock_ms=lambda: now[0]),
    ]
    writes_a.clear()
    writes_b.clear()
    now[0] = 3_000
    barrier = threading.Barrier(3)

    def heartbeat(journal):
        barrier.wait()
        return journal.heartbeat()

    with ThreadPoolExecutor(max_workers=3) as pool:
        refreshed = list(pool.map(heartbeat, journals))

    assert sorted(refreshed) == [False, True, True]
    assert writes_a == [("owner-shared", 3_000)]
    assert writes_b == [("owner-shared", 3_000)]


def test_owner_heartbeat_throttle_is_shared_by_connections_to_same_database(tmp_path):
    path = tmp_path / "shared-lease.db"
    managers = [MemoryManager(str(path)), MemoryManager(str(path.resolve()))]
    now = [1_000]
    writes = []
    writes_lock = threading.Lock()

    for manager in managers:
        original = manager.heartbeat_harness_owner

        def record(owner_id, now_ms, *, original=original):
            with writes_lock:
                writes.append((owner_id, now_ms))
            return original(owner_id, now_ms)

        manager.heartbeat_harness_owner = record

    journals = [
        HarnessJournal(manager, "owner-shared", clock_ms=lambda: now[0])
        for manager in managers
    ]
    writes.clear()
    now[0] = 3_000
    barrier = threading.Barrier(2)

    def heartbeat(journal):
        barrier.wait()
        return journal.heartbeat()

    with ThreadPoolExecutor(max_workers=2) as pool:
        refreshed = list(pool.map(heartbeat, journals))

    assert sorted(refreshed) == [False, True]
    assert writes == [("owner-shared", 3_000)]


def test_in_memory_databases_have_independent_owner_throttles():
    managers = [MemoryManager(":memory:"), MemoryManager(":memory:")]
    now = [1_000]
    journals = [
        HarnessJournal(manager, "owner-shared", clock_ms=lambda: now[0])
        for manager in managers
    ]
    now[0] = 3_000
    barrier = threading.Barrier(2)

    def heartbeat(journal):
        barrier.wait()
        return journal.heartbeat()

    with ThreadPoolExecutor(max_workers=2) as pool:
        refreshed = list(pool.map(heartbeat, journals))

    assert refreshed == [True, True]
    for manager in managers:
        assert manager.conn.execute(
            "SELECT heartbeat_at_ms FROM harness_owners "
            "WHERE owner_id = 'owner-shared'"
        ).fetchone() == (3_000,)


def test_owner_throttle_survives_journal_recreation_across_connections(tmp_path):
    path = tmp_path / "shared-lease.db"
    managers = [MemoryManager(str(path)), MemoryManager(str(path.resolve()))]
    now = [1_000]
    writes = []

    for manager in managers:
        original = manager.heartbeat_harness_owner

        def record(owner_id, now_ms, *, original=original):
            writes.append((owner_id, now_ms))
            return original(owner_id, now_ms)

        manager.heartbeat_harness_owner = record

    journal = HarnessJournal(
        managers[0],
        "owner-shared",
        clock_ms=lambda: now[0],
    )
    del journal
    gc.collect()
    now[0] = 1_500

    replacement = HarnessJournal(
        managers[1],
        "owner-shared",
        clock_ms=lambda: now[0],
    )

    assert replacement.heartbeat() is False
    assert writes == [("owner-shared", 1_000)]


def test_export_harness_journal_carries_resume_columns_without_body_text(tmp_path):
    """导出形状：turn/step 带上续跑列，且任何记录都不出现正文键。"""
    manager, journal = _journal(tmp_path, now_ms=10_000)
    journal.create_step("turn-1:step:1", "turn-1", 1)
    manager.conn.execute(
        "UPDATE harness_turns SET parent_turn_id = ?, turn_kind = ?, "
        "resume_from_step_ordinal = ? WHERE turn_id = ?",
        ("turn-0", "resume", 3, "turn-1"),
    )
    manager.conn.execute(
        "UPDATE harness_steps SET tool_call_count = ?, replay_of_step_id = ? "
        "WHERE step_id = ?",
        (5, "turn-0:step:1", "turn-1:step:1"),
    )
    manager.conn.commit()
    journal.append_event(
        "turn-1",
        JournalEventDraft(
            kind=HarnessEventKind.TOOL_STARTED,
            step_id="turn-1:step:1",
            tool_name="read_file",
            call_ordinal=0,
        ),
    )

    rows = manager.export_harness_journal(session_name="session")

    turn_row = next(row for row in rows if row["type"] == "turn")
    step_row = next(row for row in rows if row["type"] == "step")
    assert turn_row["parent_turn_id"] == "turn-0"
    assert turn_row["turn_kind"] == "resume"
    assert turn_row["resume_from_step_ordinal"] == 3
    assert step_row["tool_call_count"] == 5
    assert step_row["replay_of_step_id"] == "turn-0:step:1"
    # journal 只允许状态/ID/序号/时间/计数/sha256：正文键一个都不能出现。
    forbidden = {"content", "arguments", "result", "payload"}
    for row in rows:
        assert forbidden.isdisjoint(row), row
