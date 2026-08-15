"""Durable harness lifecycle journal and process-owner leases."""

import os
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

# Journal 依赖的 MemoryManager 存储接口（duck-type 能力面）。真实
# MemoryManager 由 schema 迁移保证全部具备；只实现 add_messages 的
# 鸭子类型管理器（旧测试替身、外部调用方）不进入 Journal，继续走
# legacy 持久化路径。
_JOURNAL_STORAGE_METHODS = (
    "acquire_lock",
    "heartbeat_harness_owner",
    "interrupt_stale_harness_runs",
    "create_harness_turn",
    "get_harness_turn",
    "transition_harness_turn",
    "create_harness_step",
    "get_harness_step",
    "transition_harness_step",
    "append_harness_event",
    "add_messages_with_harness_events",
)


def journal_storage_supported(manager) -> bool:
    """判断 manager 是否实现了 Journal 所需的完整存储接口。"""
    if manager is None:
        return False
    return all(
        callable(getattr(manager, name, None))
        for name in _JOURNAL_STORAGE_METHODS
    )


class _OwnerHeartbeatState:
    def __init__(self, last_heartbeat_ms: int | None):
        self.lock = threading.Lock()
        self.last_heartbeat_ms = last_heartbeat_ms


_OWNER_STATES_LOCK = threading.Lock()
_OWNER_STATES = weakref.WeakValueDictionary()
_MEMORY_DATABASE_DOMAINS = weakref.WeakKeyDictionary()


def _main_database_filename(manager: MemoryManager) -> str:
    with manager.acquire_lock():
        databases = manager.conn.execute("PRAGMA database_list").fetchall()
    for _sequence, name, filename in databases:
        if name == "main":
            return filename
    raise RuntimeError("SQLite connection has no main database")


def _stored_owner_heartbeat(manager: MemoryManager, owner_id: str) -> int | None:
    with manager.acquire_lock():
        row = manager.conn.execute(
            "SELECT heartbeat_at_ms FROM harness_owners WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()
    return None if row is None else int(row[0])


def _owner_heartbeat_state(manager: MemoryManager, owner_id: str):
    filename = _main_database_filename(manager)
    stored_heartbeat_ms = _stored_owner_heartbeat(manager, owner_id)
    with _OWNER_STATES_LOCK:
        if filename:
            database_domain = (
                "file",
                os.path.normcase(os.path.realpath(os.path.abspath(filename))),
            )
        else:
            database_domain = _MEMORY_DATABASE_DOMAINS.get(manager)
            if database_domain is None:
                database_domain = object()
                _MEMORY_DATABASE_DOMAINS[manager] = database_domain
        state_key = (database_domain, owner_id)
        state = _OWNER_STATES.get(state_key)
        if state is None:
            state = _OwnerHeartbeatState(stored_heartbeat_ms)
            _OWNER_STATES[state_key] = state
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

    def commit_messages(
        self,
        session_name: str,
        turn_id: str,
        step_id: str,
        messages: list[tuple[str, object]],
        events: list[JournalEventDraft],
    ) -> tuple[int, ...]:
        messages = list(messages)
        events = list(events)
        self.manager._validate_harness_message_events(
            events,
            len(messages),
            step_id,
        )
        self.manager._validate_harness_commit_ownership(
            session_name,
            turn_id,
            step_id,
        )
        self.heartbeat()
        return self.manager.add_messages_with_harness_events(
            session_name,
            messages,
            turn_id=turn_id,
            step_id=step_id,
            events=events,
            _created_at_ms=int(self.clock_ms()),
        )
