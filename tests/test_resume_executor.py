"""ResumeExecutor（断点续跑 S3 最小可用版）单测：fake writer / planner / request_fn。

执行层只做四件事：门禁 -> 建 resume turn -> 写事件 -> 调注入的请求函数。
"已完成调用的结果"由 messages 表提供，本层绝不伪造工具结果。
"""
import threading

import pytest

from core.harness.journal import HarnessJournal
from core.harness.models import HarnessEventKind, TurnStatus
from core.harness.resume import (
    BLOCK,
    REPLAY_FRESH,
    REPLAY_WITH_RESULT,
    ReplayCall,
    ResumeAnchor,
    ResumeExecutor,
    ResumePlan,
)
from core.memory import MemoryManager


class _FakeWriter:
    """收集 (turn_id, draft)，并记录 resume turn 的建表字段。"""

    def __init__(self):
        self.turns = []
        self.events = []

    def create_resume_turn(self, **fields):
        self.turns.append(dict(fields))

    def append_event(self, turn_id, draft):
        self.events.append((turn_id, draft))
        return len(self.events)

    def drafts(self, kind):
        return [draft for _turn_id, draft in self.events if draft.kind is kind]

    def event_turns(self, kind):
        return [turn_id for turn_id, draft in self.events if draft.kind is kind]


class _FakePlanner:
    def __init__(self, reasons=()):
        self._reasons = list(reasons)
        self.seen = []

    def aborts(self, plan):
        self.seen.append(plan)
        return list(self._reasons)


class _RequestRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        return "requested"


_ANCHOR = ResumeAnchor(
    messages_sha256="m" * 64,
    system_sha256="s" * 64,
    tools_sha256="t" * 64,
    cache_lane_sha256="c" * 64,
    message_count=7,
)


def _read_call(ordinal, name="read_file", step_id="turn-parent:step:2"):
    return ReplayCall(
        call_ordinal=ordinal,
        tool_name=name,
        klass=REPLAY_WITH_RESULT,
        arguments_sha256="a" * 64,
        result_sha256=str(ordinal) * 64,
        message_row_present=True,
        step_id=step_id,
    )


def _write_call(ordinal, name="write_file", step_id="turn-parent:step:3"):
    return ReplayCall(
        call_ordinal=ordinal,
        tool_name=name,
        klass=BLOCK,
        arguments_sha256="w" * 64,
        reason="write_requires_confirmation",
        step_id=step_id,
    )


def _fresh_call(ordinal, name="grep"):
    return ReplayCall(
        call_ordinal=ordinal,
        tool_name=name,
        klass=REPLAY_FRESH,
        arguments_sha256="f" * 64,
        reason="result_hash_mismatch",
        step_id="turn-parent:step:2",
    )


def _plan(*decisions, anchor=None, resume_from=2):
    return ResumePlan(
        session_name="session",
        parent_turn_id="turn-parent",
        resume_from_step_ordinal=resume_from,
        decisions=tuple(decisions),
        anchor=_ANCHOR if anchor is None else anchor,
        blockers=tuple(
            f"第 {call.call_ordinal} 个调用 {call.tool_name} 需要确认后才能继续"
            for call in decisions
            if call.klass == BLOCK
        ),
    )

def test_unconfirmed_block_requires_confirm_and_sends_no_request():
    writer = _FakeWriter()
    request = _RequestRecorder()
    executor = ResumeExecutor(None, _FakePlanner(), writer=writer, request_fn=request)

    result = executor.run(_plan(_read_call(0), _write_call(1)))

    assert result["ok"] is False
    assert result["reason"] == "confirm_required"
    assert [item["call_ordinal"] for item in result["blockers"]] == [1]
    assert result["blockers"][0]["tool_name"] == "write_file"
    assert result["blockers"][0]["arguments_sha256"] == "w" * 64
    assert request.calls == []
    assert writer.turns == []
    confirms = writer.drafts(HarnessEventKind.RESUME_WRITE_CONFIRM_REQUIRED)
    assert len(confirms) == 1
    assert confirms[0].call_ordinal == 1
    assert confirms[0].tool_name == "write_file"
    assert confirms[0].arguments_sha256 == "w" * 64
    # 未建 turn：确认事件挂父回合上，且绝无 resume_started
    assert writer.event_turns(HarnessEventKind.RESUME_WRITE_CONFIRM_REQUIRED) == ["turn-parent"]
    assert writer.drafts(HarnessEventKind.RESUME_STARTED) == []


@pytest.mark.parametrize("confirmed", [frozenset({1}), frozenset({"1"})])
def test_confirmed_blockers_are_released(confirmed):
    writer = _FakeWriter()
    request = _RequestRecorder()
    executor = ResumeExecutor(None, _FakePlanner(), writer=writer, request_fn=request)

    result = executor.run(_plan(_read_call(0), _write_call(1)), confirmed=confirmed)

    assert result["ok"] is True
    assert result["dry_run"] is False
    assert request.calls and request.calls[0]["request_reason"] == "resume"
    assert writer.drafts(HarnessEventKind.RESUME_WRITE_CONFIRM_REQUIRED) == []
    assert len(writer.turns) == 1


def test_aborts_reason_writes_resume_aborted_without_request():
    writer = _FakeWriter()
    request = _RequestRecorder()
    executor = ResumeExecutor(
        None, _FakePlanner(["session_archived"]), writer=writer, request_fn=request
    )

    result = executor.run(_plan(_read_call(0)))

    assert result == {
        "ok": False,
        "reason": "session_archived",
        "reasons": ["session_archived"],
    }
    assert request.calls == []
    assert writer.turns == []
    aborted = writer.drafts(HarnessEventKind.RESUME_ABORTED)
    assert len(aborted) == 1
    assert aborted[0].error_code == "session_archived"
    assert writer.drafts(HarnessEventKind.RESUME_STARTED) == []


def test_none_plan_aborts_without_turn_or_request():
    writer = _FakeWriter()
    request = _RequestRecorder()
    executor = ResumeExecutor(None, _FakePlanner(), writer=writer, request_fn=request)

    result = executor.run(None)

    assert result == {
        "ok": False,
        "reason": "no_resumable_turn",
        "reasons": ["no_resumable_turn"],
    }
    assert request.calls == []
    assert writer.turns == []
    assert writer.events == []


def test_anchor_mismatch_aborts_without_request():
    writer = _FakeWriter()
    request = _RequestRecorder()
    drifted = ResumeAnchor(
        messages_sha256="x" * 64,
        system_sha256="s" * 64,
        tools_sha256="t" * 64,
        cache_lane_sha256="c" * 64,
        message_count=7,
    )
    executor = ResumeExecutor(
        None,
        _FakePlanner(),
        writer=writer,
        request_fn=request,
        anchor_builder=lambda: drifted,
    )

    result = executor.run(_plan(_read_call(0)))

    assert result["ok"] is False
    assert result["reason"] == "hash_anchor_mismatch"
    assert request.calls == []
    assert writer.turns == []
    assert [
        draft.error_code for draft in writer.drafts(HarnessEventKind.RESUME_ABORTED)
    ] == ["hash_anchor_mismatch"]

def test_normal_path_builds_resume_turn_and_replays_reads():
    writer = _FakeWriter()
    request = _RequestRecorder()
    executor = ResumeExecutor(
        None,
        _FakePlanner(),
        writer=writer,
        request_fn=request,
        anchor_builder=lambda: _ANCHOR,
    )

    result = executor.run(_plan(_read_call(0), _read_call(1, name="grep"), _fresh_call(2)))

    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["anchored"] is True
    assert result["replayed"] == 2
    turn = writer.turns[0]
    turn_id = turn["turn_id"]
    assert turn_id.startswith("resume-")
    assert len(turn_id) == len("resume-") + 8
    assert turn["parent_turn_id"] == "turn-parent"
    assert turn["turn_kind"] == "resume"
    assert turn["resume_from_step_ordinal"] == 2
    assert turn["session_name"] == "session"
    assert result["turn_id"] == turn_id

    assert writer.event_turns(HarnessEventKind.RESUME_STARTED) == [turn_id]
    replayed = writer.drafts(HarnessEventKind.RESUME_READ_REPLAYED)
    assert [
        (draft.step_id, draft.call_ordinal, draft.tool_name, draft.result_sha256)
        for draft in replayed
    ] == [
        ("turn-parent:step:2", 0, "read_file", str(0) * 64),
        ("turn-parent:step:2", 1, "grep", str(1) * 64),
    ]
    # 只有两个 REPLAY_WITH_RESULT 回灌；REPLAY_FRESH 不写事件
    assert len(replayed) == 2
    # 成功路径不在父回合上留任何事件（父回合零改动）
    assert {event_turn for event_turn, _draft in writer.events} == {turn_id}
    assert request.calls == [
        {"turn_id": turn_id, "parent_turn_id": "turn-parent", "request_reason": "resume"}
    ]


def test_missing_request_fn_returns_dry_run_but_still_writes_events():
    writer = _FakeWriter()
    executor = ResumeExecutor(None, _FakePlanner(), writer=writer)

    result = executor.run(_plan(_read_call(0)))

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["anchored"] is False
    assert result["turn_id"] == writer.turns[0]["turn_id"]
    assert len(writer.drafts(HarnessEventKind.RESUME_STARTED)) == 1
    assert len(writer.drafts(HarnessEventKind.RESUME_READ_REPLAYED)) == 1


def test_cancel_event_aborts_without_request():
    writer = _FakeWriter()
    request = _RequestRecorder()
    executor = ResumeExecutor(None, _FakePlanner(), writer=writer, request_fn=request)
    cancel_event = threading.Event()
    cancel_event.set()

    result = executor.run(_plan(_read_call(0)), cancel_event=cancel_event)

    assert result["reason"] == "resume_cancelled"
    assert request.calls == []
    assert writer.turns == []

def _turn_row(manager, turn_id):
    return manager.conn.execute(
        "SELECT turn_id, session_id, owner_id, status, started_at_ms, "
        "finished_at_ms, last_sequence, error_code FROM harness_turns WHERE turn_id = ?",
        (turn_id,),
    ).fetchone()


def test_real_journal_default_writer_keeps_parent_interrupted(tmp_path):
    """默认 writer（HarnessJournal）真跑：三个字段落库，父回合一个字节都不动。"""
    manager = MemoryManager(str(tmp_path / "resume-exec.db"))
    manager.create_session_explicit("session")
    journal = HarnessJournal(manager, "owner-exec", clock_ms=lambda: 10_000)
    journal.heartbeat()
    journal.create_turn("turn-parent", "session")
    journal.create_step("turn-parent:step:2", "turn-parent", 2)
    journal.transition_turn("turn-parent", TurnStatus.CREATED, TurnStatus.PREPARING)
    journal.transition_turn("turn-parent", TurnStatus.PREPARING, TurnStatus.INTERRUPTED)
    parent_before = _turn_row(manager, "turn-parent")

    request = _RequestRecorder()
    executor = ResumeExecutor(
        manager, _FakePlanner(), request_fn=request, anchor_builder=lambda: _ANCHOR
    )

    result = executor.run(_plan(_read_call(0)))

    assert result["ok"] is True
    turn_id = result["turn_id"]
    row = manager.conn.execute(
        "SELECT parent_turn_id, turn_kind, resume_from_step_ordinal, status "
        "FROM harness_turns WHERE turn_id = ?",
        (turn_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "turn-parent"
    assert row[1] == "resume"
    assert row[2] == 2
    assert row[3] == "created"
    # 父回合仍是 interrupted，且所有列逐字段未变（§4.3）
    assert parent_before[3] == "interrupted"
    assert _turn_row(manager, "turn-parent") == parent_before

    events = manager.conn.execute(
        "SELECT kind, step_id, call_ordinal, result_sha256, error_code, tool_name "
        "FROM harness_events WHERE turn_id = ? ORDER BY sequence",
        (turn_id,),
    ).fetchall()
    assert [event[0] for event in events] == ["resume_started", "resume_read_replayed"]
    assert events[1][1] == "turn-parent:step:2"
    assert events[1][2] == 0
    assert events[1][3] == str(0) * 64
    assert events[1][4] is None
    assert events[1][5] == "read_file"
    assert request.calls == [
        {"turn_id": turn_id, "parent_turn_id": "turn-parent", "request_reason": "resume"}
    ]
