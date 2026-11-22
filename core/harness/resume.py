from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from core.harness import resume_policy
from core.harness.models import HarnessEventKind, JournalEventDraft

REPLAY_WITH_RESULT = "replay_with_result"
REPLAY_FRESH = "replay_fresh"
BLOCK = "block"

_KLASS_BY_DECISION = {
    resume_policy.ReplayDecision.REPLAY: REPLAY_WITH_RESULT,
    resume_policy.ReplayDecision.REPLAY_FRESH: REPLAY_FRESH,
    resume_policy.ReplayDecision.CONFIRM: BLOCK,
    resume_policy.ReplayDecision.BLOCK: BLOCK,
}

_TURN_INTERRUPTED = "interrupted"
_STEP_COMPLETED = "completed"

_EV_STARTED = "tool_started"
_EV_FINISHED = "tool_finished"


@dataclass(frozen=True)
class ReplayCall:
    """一个待定工具调用的处置结果。"""

    call_ordinal: int
    tool_name: str
    klass: str
    arguments_sha256: str | None = None
    result_sha256: str | None = None
    message_row_present: bool = False
    reason: str = ""
    step_id: str = ""


@dataclass(frozen=True)
class ResumeAnchor:

    messages_sha256: str = ""
    system_sha256: str = ""
    tools_sha256: str = ""
    cache_lane_sha256: str = ""
    message_count: int = 0

    def complete(self) -> bool:
        return bool(self.messages_sha256 and self.system_sha256 and self.tools_sha256)


@dataclass(frozen=True)
class ResumePlan:
    session_name: str
    parent_turn_id: str
    resume_from_step_ordinal: int
    decisions: tuple[ReplayCall, ...] = ()
    anchor: ResumeAnchor = field(default_factory=ResumeAnchor)
    blockers: tuple[str, ...] = ()

    def by_klass(self, klass: str) -> tuple[ReplayCall, ...]:
        return tuple(call for call in self.decisions if call.klass == klass)

    @property
    def replayable(self) -> tuple[ReplayCall, ...]:
        return self.by_klass(REPLAY_WITH_RESULT)

    @property
    def fresh(self) -> tuple[ReplayCall, ...]:
        return self.by_klass(REPLAY_FRESH)

    @property
    def blocked(self) -> tuple[ReplayCall, ...]:
        return self.by_klass(BLOCK)

class ResumePlanner:

    def __init__(
        self,
        manager: Any,
        registry: Any = None,
        *,
        owner_alive: Callable[[str], bool] | None = None,
        session_archived: Callable[[str], bool] | None = None,
        undo_recovering: Callable[[str], bool] | None = None,
        permission_narrowed: Callable[[str], bool] | None = None,
    ) -> None:
        self._manager = manager
        self._registry = registry
        self._owner_alive = owner_alive
        self._session_archived = session_archived
        self._undo_recovering = undo_recovering
        self._permission_narrowed = permission_narrowed

    def plan(self, session_name: str) -> ResumePlan | None:
        """产出续跑计划；没有可续跑的 interrupted 回合时返回 None。"""
        rows = self._export(session_name)
        if not rows:
            return None
        parent = self._latest_interrupted_turn(rows)
        if parent is None:
            return None

        parent_id = str(parent.get("turn_id") or "")
        steps = self._steps_of(rows, parent_id)
        last_done = self._last_completed_step(steps)
        resume_from = int(last_done.get("ordinal") or 0) + 1 if last_done else 1

        decisions: list[ReplayCall] = []
        for step in steps:
            if int(step.get("ordinal") or 0) < resume_from:
                continue
            decisions.extend(self._classify_step(rows, step, session_name))

        blockers = tuple(
            f"第 {call.call_ordinal} 个调用 {call.tool_name} 需要确认后才能继续（{call.reason}）"
            for call in decisions
            if call.klass == BLOCK
        )
        return ResumePlan(
            session_name=session_name,
            parent_turn_id=parent_id,
            resume_from_step_ordinal=resume_from,
            decisions=tuple(decisions),
            anchor=self._anchor(last_done),
            blockers=blockers,
        )

    def aborts(self, plan: ResumePlan | None) -> list[str]:
        if plan is None:
            return ["no_resumable_turn"]
        reasons: list[str] = []
        if not plan.anchor.complete():
            reasons.append("anchor_missing")
        if self._probe(self._session_archived, plan.session_name):
            reasons.append("session_archived")
        if self._probe(self._undo_recovering, plan.session_name):
            reasons.append("undo_recovering")
        if self._probe(self._owner_alive, plan.parent_turn_id):
            reasons.append("owner_still_alive")
        return reasons
    def _export(self, session_name: str) -> list[dict]:
        try:
            rows = self._manager.export_harness_journal(session_name=session_name)
        except Exception:
            return []
        return list(rows or [])

    @staticmethod
    def _latest_interrupted_turn(rows: list[dict]) -> dict | None:
        turns = [
            row
            for row in rows
            if row.get("type") == "turn" and str(row.get("status") or "") == _TURN_INTERRUPTED
        ]
        if not turns:
            return None
        turns.sort(
            key=lambda row: (int(row.get("started_at_ms") or 0), str(row.get("turn_id") or ""))
        )
        return turns[-1]

    @staticmethod
    def _steps_of(rows: list[dict], turn_id: str) -> list[dict]:
        steps = [
            row
            for row in rows
            if row.get("type") == "step" and str(row.get("turn_id") or "") == turn_id
        ]
        steps.sort(key=lambda row: int(row.get("ordinal") or 0))
        return steps

    @staticmethod
    def _last_completed_step(steps: list[dict]) -> dict | None:
        done = [step for step in steps if str(step.get("status") or "") == _STEP_COMPLETED]
        return done[-1] if done else None

    @staticmethod
    def _anchor(last_done: dict | None) -> ResumeAnchor:
        if not last_done:
            return ResumeAnchor()
        return ResumeAnchor(
            messages_sha256=str(last_done.get("messages_sha256") or ""),
            system_sha256=str(last_done.get("system_sha256") or ""),
            tools_sha256=str(last_done.get("tools_sha256") or ""),
            cache_lane_sha256=str(last_done.get("cache_lane_sha256") or ""),
            message_count=int(last_done.get("message_count") or 0),
        )
    def _classify_step(
        self, rows: list[dict], step: dict, session_name: str
    ) -> list[ReplayCall]:
        step_id = str(step.get("step_id") or "")
        calls = self._calls_of_step(rows, step_id)
        results: list[ReplayCall] = []
        for ordinal in sorted(calls):
            info = calls[ordinal]
            facts = self._facts(info, session_name)
            decision, reason = resume_policy.decide_replay(facts)
            results.append(
                ReplayCall(
                    call_ordinal=ordinal,
                    step_id=step_id,
                    tool_name=info["tool_name"],
                    klass=_KLASS_BY_DECISION.get(decision, BLOCK),
                    arguments_sha256=info["arguments_sha256"],
                    result_sha256=info["result_sha256"],
                    message_row_present=info["message_row_present"],
                    reason=reason,
                )
            )
        return results

    @staticmethod
    def _calls_of_step(rows: list[dict], step_id: str) -> dict[int, dict]:
        calls: dict[int, dict] = {}
        for row in rows:
            if row.get("type") != "event" or str(row.get("step_id") or "") != step_id:
                continue
            ordinal = row.get("call_ordinal")
            if ordinal is None:
                continue
            key = int(ordinal)
            entry = calls.setdefault(
                key,
                {
                    "tool_name": "",
                    "started": False,
                    "finished": False,
                    "arguments_sha256": None,
                    "result_sha256": None,
                    "message_row_present": False,
                },
            )
            if row.get("tool_name"):
                entry["tool_name"] = str(row["tool_name"])
            kind = str(row.get("kind") or "")
            if kind == _EV_STARTED:
                entry["started"] = True
                entry["arguments_sha256"] = (
                    row.get("arguments_sha256") or entry["arguments_sha256"]
                )
            elif kind == _EV_FINISHED:
                entry["finished"] = True
                entry["result_sha256"] = row.get("result_sha256") or entry["result_sha256"]
                # 结果正文只在 messages 表：没有 message_id 说明"结果没落正文"，
                # S3 无法逐字节回灌，必须让计划知情（记录下来交给执行层判断）。
                entry["message_row_present"] = bool(row.get("message_id"))
        return calls
    def _facts(self, info: dict, session_name: str) -> "resume_policy.ToolReplayFacts":
        tool_name = str(info.get("tool_name") or "")
        spec = self._lookup_tool(tool_name)
        if spec is None:
            return resume_policy.ToolReplayFacts(
                tool_name=tool_name,
                side_effect_scope="system",
                read_only=False,
                source="builtin",
                started=bool(info.get("started")),
                finished=bool(info.get("finished")),
                tool_available=False,
            )
        return resume_policy.ToolReplayFacts(
            tool_name=tool_name,
            side_effect_scope=str(getattr(spec, "side_effect_scope", "none") or "none"),
            read_only=bool(getattr(spec, "read_only", False)),
            source=self._source_of(tool_name),
            started=bool(info.get("started")),
            finished=bool(info.get("finished")),
            result_hash_ok=True,
            tool_available=True,
            hash_anchors_ok=True,
            permission_narrowed=self._probe(self._permission_narrowed, tool_name),
            session_archived=self._probe(self._session_archived, session_name),
            undo_recovering=self._probe(self._undo_recovering, session_name),
        )

    def _lookup_tool(self, name: str):
        if not name or self._registry is None:
            return None
        getter = getattr(self._registry, "get", None)
        if not callable(getter):
            return None
        try:
            return getter(name)
        except Exception:
            return None

    def _source_of(self, name: str) -> str:
        probe = getattr(self._registry, "source_of", None)
        if not callable(probe):
            return "builtin"
        try:
            return str(probe(name) or "builtin")
        except Exception:
            return "builtin"

    @staticmethod
    def _probe(fn: Callable[[str], bool] | None, arg: str) -> bool:
        if fn is None:
            return False
        try:
            return bool(fn(arg))
        except Exception:
            # 探针故障按"该条件未命中"处理：宁可让 S3 的四哈希断言兜底，
            # 也不要因为一个可选依赖挂了就把用户永久挡在续跑之外。
            return False


_TURN_KIND_RESUME = "resume"
_RESUME_TURN_PREFIX = "resume-"

ERR_NO_RESUMABLE_TURN = "no_resumable_turn"
ERR_CONFIRM_REQUIRED = "confirm_required"
ERR_ANCHOR_MISMATCH = "hash_anchor_mismatch"
ERR_CANCELLED = "resume_cancelled"
ERR_PLANNER_FAILED = "planner_failed"

_ANCHOR_HASH_FIELDS = (
    "messages_sha256",
    "system_sha256",
    "tools_sha256",
    "cache_lane_sha256",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_resume_turn_id() -> str:
    return f"{_RESUME_TURN_PREFIX}{uuid.uuid4().hex[:8]}"


def _confirmed_keys(confirmed: Any) -> set[str]:
    keys: set[str] = set()
    for item in confirmed or ():
        text = str(item).strip()
        if text:
            keys.add(text)
    return keys


def _anchors_match(expected: ResumeAnchor, current: ResumeAnchor | None) -> bool:
    if current is None:
        return False
    for name in _ANCHOR_HASH_FIELDS:
        if str(getattr(expected, name, "") or "") != str(getattr(current, name, "") or ""):
            return False
    want_count = int(getattr(expected, "message_count", 0) or 0)
    got_count = int(getattr(current, "message_count", 0) or 0)
    if want_count and got_count and want_count != got_count:
        return False
    return True

class ResumeExecutor:

    def __init__(
        self,
        manager: Any,
        planner: Any = None,
        *,
        writer: Any = None,
        request_fn: Callable[..., Any] | None = None,
        anchor_builder: Callable[[], ResumeAnchor | None] | None = None,
    ) -> None:
        self._manager = manager
        self._planner = planner
        self._writer = writer
        self._request_fn = request_fn
        self._anchor_builder = anchor_builder
        self._journal: Any = None

    def run(
        self,
        plan: ResumePlan | None,
        *,
        confirmed: frozenset = frozenset(),
        cancel_event: Any = None,
    ) -> dict:
        """执行续跑；返回值是给入口层做用户可见展示的纯字典，不含任何正文。"""
        reasons = self._abort_reasons(plan)
        if reasons:
            self._emit_abort(plan, reasons[0])
            return {"ok": False, "reason": reasons[0], "reasons": reasons}

        keys = _confirmed_keys(confirmed)
        unconfirmed = tuple(
            call for call in plan.blocked if str(call.call_ordinal) not in keys
        )
        if unconfirmed:
            self._emit_confirm_required(plan, unconfirmed)
            return {
                "ok": False,
                "reason": ERR_CONFIRM_REQUIRED,
                "blockers": [
                    {
                        "call_ordinal": int(call.call_ordinal),
                        "tool_name": call.tool_name,
                        "arguments_sha256": call.arguments_sha256,
                    }
                    for call in unconfirmed
                ],
            }

        matched, anchored = self._check_anchor(plan)
        if not matched:
            self._emit_abort(plan, ERR_ANCHOR_MISMATCH)
            return {
                "ok": False,
                "reason": ERR_ANCHOR_MISMATCH,
                "reasons": [ERR_ANCHOR_MISMATCH],
            }

        if self._cancelled(cancel_event):
            self._emit_abort(plan, ERR_CANCELLED)
            return {"ok": False, "reason": ERR_CANCELLED, "reasons": [ERR_CANCELLED]}

        turn_id = _new_resume_turn_id()
        self._open_turn(plan, turn_id)
        self._writer_or_journal().append_event(
            turn_id, JournalEventDraft(kind=HarnessEventKind.RESUME_STARTED)
        )
        replayed = self._emit_replayed(turn_id, plan)

        outcome = {
            "ok": True,
            "turn_id": turn_id,
            "parent_turn_id": plan.parent_turn_id,
            "dry_run": self._request_fn is None,
            "replayed": replayed,
            "anchored": anchored,
        }
        if self._request_fn is None:
            return outcome
        self._request_fn(
            turn_id=turn_id,
            parent_turn_id=plan.parent_turn_id,
            request_reason="resume",
        )
        return outcome

    def _abort_reasons(self, plan: ResumePlan | None) -> list[str]:
        if plan is None:
            if self._planner is None:
                return [ERR_NO_RESUMABLE_TURN]
            return self._planner_aborts(None) or [ERR_NO_RESUMABLE_TURN]
        if self._planner is None:
            return []
        return self._planner_aborts(plan)

    def _planner_aborts(self, plan: ResumePlan | None) -> list[str]:
        try:
            reasons = self._planner.aborts(plan)
        except Exception:
            return [ERR_PLANNER_FAILED]
        return [str(reason) for reason in (reasons or ())]

    def _check_anchor(self, plan: ResumePlan) -> tuple[bool, bool]:
        if self._anchor_builder is None:
            return True, False
        try:
            current = self._anchor_builder()
        except Exception:
            return False, True
        return _anchors_match(plan.anchor, current), True

    @staticmethod
    def _cancelled(cancel_event: Any) -> bool:
        if cancel_event is None:
            return False
        probe = getattr(cancel_event, "is_set", None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:
                return False
        return bool(cancel_event)

    def _open_turn(self, plan: ResumePlan, turn_id: str) -> None:
        fields = {
            "turn_id": turn_id,
            "session_name": plan.session_name,
            "parent_turn_id": plan.parent_turn_id,
            "turn_kind": _TURN_KIND_RESUME,
            "resume_from_step_ordinal": int(plan.resume_from_step_ordinal or 0),
        }
        writer = self._writer_or_journal()
        creator = getattr(writer, "create_resume_turn", None)
        if callable(creator):
            creator(**fields)
            return
        writer.create_turn(turn_id, plan.session_name)
        self._stamp_turn_fields(writer, fields)

    def _stamp_turn_fields(self, writer: Any, fields: dict) -> None:
        args = (
            fields["turn_id"],
            fields["parent_turn_id"],
            fields["turn_kind"],
            fields["resume_from_step_ordinal"],
        )
        for owner, name in (
            (self._manager, "set_harness_turn_resume"),
            (writer, "stamp_resume"),
        ):
            hook = getattr(owner, name, None)
            if callable(hook):
                hook(*args)
                return
        conn = getattr(self._manager, "conn", None)
        if conn is None:
            return
        sql = (
            "UPDATE harness_turns SET parent_turn_id = ?, turn_kind = ?, "
            "resume_from_step_ordinal = ? WHERE turn_id = ?"
        )
        params = (args[1], args[2], args[3], args[0])

        def _apply() -> None:
            conn.execute(sql, params)
            conn.commit()

        lock = getattr(self._manager, "acquire_lock", None)
        if callable(lock):
            with lock():
                _apply()
        else:
            _apply()

    def _emit_abort(self, plan: ResumePlan | None, error_code: str) -> None:
        turn_id = str(getattr(plan, "parent_turn_id", "") or "") if plan is not None else ""
        if not turn_id:
            return
        self._append(
            turn_id,
            JournalEventDraft(kind=HarnessEventKind.RESUME_ABORTED, error_code=error_code),
        )

    def _emit_confirm_required(
        self, plan: ResumePlan, calls: tuple[ReplayCall, ...]
    ) -> None:
        for call in calls:
            self._append(
                str(plan.parent_turn_id),
                JournalEventDraft(
                    kind=HarnessEventKind.RESUME_WRITE_CONFIRM_REQUIRED,
                    step_id=call.step_id or None,
                    tool_name=call.tool_name or None,
                    call_ordinal=int(call.call_ordinal),
                    arguments_sha256=call.arguments_sha256,
                ),
            )

    def _emit_replayed(self, turn_id: str, plan: ResumePlan) -> int:
        writer = self._writer_or_journal()
        count = 0
        for call in plan.replayable:
            writer.append_event(
                turn_id,
                JournalEventDraft(
                    kind=HarnessEventKind.RESUME_READ_REPLAYED,
                    step_id=call.step_id or None,
                    tool_name=call.tool_name or None,
                    call_ordinal=int(call.call_ordinal),
                    result_sha256=call.result_sha256,
                ),
            )
            count += 1
        return count

    def _append(self, turn_id: str, draft: JournalEventDraft) -> None:
        self._writer_or_journal().append_event(turn_id, draft)

    def _writer_or_journal(self) -> Any:
        if self._writer is not None:
            return self._writer
        if self._journal is None:
            from core.harness.journal import HarnessJournal

            owner = f"harness-resume-{uuid.uuid4().hex[:8]}"
            self._journal = HarnessJournal(self._manager, owner, clock_ms=_now_ms)
            self._writer = self._journal
        return self._writer
