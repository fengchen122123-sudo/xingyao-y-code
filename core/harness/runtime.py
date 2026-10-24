"""In-process and durable lifecycle coordination for one model turn."""

from __future__ import annotations

import logging
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


_LOGGER = logging.getLogger(__name__)


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

# 回合存续期间的心跳兜底节拍（与 journal._HEARTBEAT_INTERVAL_MS 对齐；
# journal.heartbeat 内部再按同一间隔节流，双保险不多写）。
_HEARTBEAT_SIDEKAR_INTERVAL_SECONDS = 2.0


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
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None

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
            runtime._start_heartbeat_sidecar()
        runtime._transition(TurnStatus.PREPARING)
        return runtime

    def _start_heartbeat_sidecar(self) -> None:
        """回合存续期间的 owner 心跳兜底线程。

        此前心跳只搭车在 journal 操作与 raise_if_cancelled 上：单个工具执行
        超过 _OWNER_STALE_AFTER_MS（10s）期间零心跳，另一进程（GUI 与 CLI
        共享 ycode.db）的任意 journal 操作即把本进程在飞 Turn/Step 误标
        interrupted（stale owner 误中断，回合随后 CAS 失败莫名报错）。
        本线程按节流间隔续租，turn 进入终态即退出；journal 为 None 的
        非持久回合不启动（保持原行为）。
        """
        if self.journal is None:
            return
        stop = threading.Event()
        self._heartbeat_stop = stop
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(stop,),
            name=f"harness-heartbeat-{self.turn_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self, stop: threading.Event) -> None:
        while not stop.wait(_HEARTBEAT_SIDEKAR_INTERVAL_SECONDS):
            try:
                self.journal.heartbeat()
            except Exception:
                # 单次心跳失败不影响回合；下一拍重试（DB 锁竞争等瞬态）
                _LOGGER.debug("harness heartbeat sidecar beat failed", exc_info=True)

    def _stop_heartbeat_sidecar(self) -> None:
        stop = self._heartbeat_stop
        self._heartbeat_stop = None
        self._heartbeat_thread = None
        if stop is not None:
            stop.set()

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
        if target in _TURN_TERMINAL:
            self._stop_heartbeat_sidecar()
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
            try:
                self.journal.append_event(
                    self.turn_id,
                    JournalEventDraft(
                        HarnessEventKind.STEP_CREATED,
                        step_id=step.step_id,
                    ),
                )
            except Exception:
                # 行已建、事件失败：补一个 created→interrupted 收敛，否则该行
                # 停在 created 且本进程 owner 一直活跃，崩溃恢复永远不会收它
                try:
                    self.journal.transition_step(
                        step.step_id, StepStatus.CREATED, StepStatus.INTERRUPTED
                    )
                except Exception:
                    _LOGGER.warning(
                        "orphan step compensating transition failed: %s",
                        step.step_id,
                        exc_info=True,
                    )
                raise
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

    def mark_tool_call_count(self, count: int) -> None:
        """记录本轮工具调用数（续跑需要知道"原本要执行几个"）。

        只在 tools_running 状态下更新；非该状态（批处理尾部、已提交）静默跳过，
        避免把调用方的收尾逻辑打断。状态正确但更新行数异常时抛错，与
        capture_request 的并发防护口径一致。
        """
        try:
            total = int(count)
        except (TypeError, ValueError):
            raise ValueError("tool call count must be an integer")
        if total < 0:
            raise ValueError("tool call count must be non-negative")
        if self.turn.manager is None:
            return
        with self.turn.manager.acquire_lock():
            row = self.turn.manager.get_harness_step(self.step_id)
            status = "" if row is None else str(row.get("status") or "")
            if status != StepStatus.TOOLS_RUNNING.value:
                _LOGGER.warning(
                    "skip tool call count for step %s in status %s",
                    self.step_id,
                    status or "unknown",
                )
                return
            cursor = self.turn.manager.conn.cursor()
            try:
                cursor.execute(
                    "UPDATE harness_steps SET tool_call_count = ? "
                    "WHERE step_id = ? AND status = 'tools_running'",
                    (total, self.step_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"stale harness tool count: {self.step_id}"
                    )
                self.turn.manager.conn.commit()
            except Exception:
                self.turn.manager.conn.rollback()
                raise

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
