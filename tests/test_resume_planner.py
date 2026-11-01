"""ResumePlanner（断点续跑 S2）单测：纯只读规划层，不写库、不发请求。"""
import pytest

from core.harness.journal import HarnessJournal
from core.harness.models import HarnessEventKind, JournalEventDraft
from core.harness.resume import (
    BLOCK,
    REPLAY_FRESH,
    REPLAY_WITH_RESULT,
    ResumePlanner,
)
from core.memory import MemoryManager


class _Spec:
    def __init__(self, read_only=False, scope="none", source="builtin"):
        self.read_only = read_only
        self.side_effect_scope = scope
        self.source = source


class _Registry:
    """最小注册表替身：只实现 planner 读取的两个方法。"""

    def __init__(self, specs, sources=None):
        self._specs = dict(specs)
        self._sources = dict(sources or {})

    def get(self, name):
        if name not in self._specs:
            raise KeyError(name)
        return self._specs[name]

    def source_of(self, name):
        return self._sources.get(name, "builtin")


def _setup(tmp_path, *, status="interrupted"):
    """父回合：step1 已完成（带四哈希锚点），step2 停在 tools_running。"""
    manager = MemoryManager(str(tmp_path / "resume.db"))
    manager.create_session_explicit("session")
    journal = HarnessJournal(manager, "owner-test", clock_ms=lambda: 10_000)
    journal.heartbeat()
    journal.create_turn("turn-parent", "session")
    journal.create_step("turn-parent:step:1", "turn-parent", 1)
    journal.create_step("turn-parent:step:2", "turn-parent", 2)
    manager.conn.execute(
        "UPDATE harness_turns SET status = ? WHERE turn_id = 'turn-parent'", (status,)
    )
    manager.conn.execute(
        "UPDATE harness_steps SET status = 'completed', messages_sha256 = ?, "
        "system_sha256 = ?, tools_sha256 = ?, cache_lane_sha256 = ?, message_count = 3 "
        "WHERE step_id = 'turn-parent:step:1'",
        ("m" * 64, "s" * 64, "t" * 64, "c" * 64),
    )
    manager.conn.execute(
        "UPDATE harness_steps SET status = 'tools_running' WHERE step_id = 'turn-parent:step:2'"
    )
    manager.conn.commit()
    return manager, journal


def _emit(journal, *, ordinal=0, name="read_file", started=True, finished=True, step="turn-parent:step:2"):
    if started:
        journal.append_event(
            "turn-parent",
            JournalEventDraft(
                kind=HarnessEventKind.TOOL_STARTED,
                step_id=step,
                tool_name=name,
                call_ordinal=ordinal,
                arguments_sha256="a" * 64,
            ),
        )
    if finished:
        journal.append_event(
            "turn-parent",
            JournalEventDraft(
                kind=HarnessEventKind.TOOL_FINISHED,
                step_id=step,
                tool_name=name,
                call_ordinal=ordinal,
                result_sha256="r" * 64,
                result_status="ok",
            ),
        )

def test_plan_returns_none_without_interrupted_turn(tmp_path):
    manager, _journal = _setup(tmp_path, status="running")
    planner = ResumePlanner(manager, _Registry({}))
    assert planner.plan("session") is None
    assert planner.aborts(None) == ["no_resumable_turn"]


def test_plan_replays_completed_read_call(tmp_path):
    manager, journal = _setup(tmp_path)
    _emit(journal, ordinal=0, name="read_file")
    planner = ResumePlanner(
        manager, _Registry({"read_file": _Spec(read_only=True, scope="none")})
    )

    plan = planner.plan("session")

    assert plan is not None
    assert plan.parent_turn_id == "turn-parent"
    assert plan.resume_from_step_ordinal == 2
    assert [call.klass for call in plan.decisions] == [REPLAY_WITH_RESULT]
    assert plan.decisions[0].tool_name == "read_file"
    assert plan.decisions[0].result_sha256 == "r" * 64
    assert plan.blockers == ()
    assert planner.aborts(plan) == []


def test_plan_blocks_write_call(tmp_path):
    manager, journal = _setup(tmp_path)
    _emit(journal, name="write_file")
    planner = ResumePlanner(
        manager, _Registry({"write_file": _Spec(read_only=False, scope="workspace")})
    )

    plan = planner.plan("session")

    assert [call.klass for call in plan.decisions] == [BLOCK]
    assert plan.decisions[0].reason == "write_requires_confirmation"
    assert len(plan.blockers) == 1 and "write_file" in plan.blockers[0]


def test_plan_blocks_uncertain_call(tmp_path):
    """有 STARTED 无 FINISHED = 不确定态，绝不能自动重放（R-T16）。"""
    manager, journal = _setup(tmp_path)
    _emit(journal, name="read_file", started=True, finished=False)
    planner = ResumePlanner(
        manager, _Registry({"read_file": _Spec(read_only=True, scope="none")})
    )

    plan = planner.plan("session")

    assert [call.klass for call in plan.decisions] == [BLOCK]
    assert plan.decisions[0].reason == "uncertain_state"


def test_plan_blocks_unregistered_tool(tmp_path):
    manager, journal = _setup(tmp_path)
    _emit(journal, name="removed_tool")
    planner = ResumePlanner(manager, _Registry({}))

    plan = planner.plan("session")

    assert [call.klass for call in plan.decisions] == [BLOCK]
    assert plan.decisions[0].reason == "tool_unavailable"


def test_plan_blocks_untrusted_plugin_source(tmp_path):
    """插件/MCP 暴露的读工具也不能自动回灌（scope 声明不可信，R-T15）。"""
    manager, journal = _setup(tmp_path)
    _emit(journal, name="plugin_read")
    planner = ResumePlanner(
        manager,
        _Registry(
            {"plugin_read": _Spec(read_only=True, scope="none")},
            sources={"plugin_read": "plugin"},
        ),
    )

    plan = planner.plan("session")

    assert [call.klass for call in plan.decisions] == [BLOCK]
    assert plan.decisions[0].reason == "untrusted_scope"


def test_permission_narrowed_blocks_write_keeps_read(tmp_path):
    manager, journal = _setup(tmp_path)
    _emit(journal, ordinal=0, name="read_file")
    _emit(journal, ordinal=1, name="write_file")
    planner = ResumePlanner(
        manager,
        _Registry(
            {
                "read_file": _Spec(read_only=True, scope="none"),
                "write_file": _Spec(read_only=False, scope="workspace"),
            }
        ),
        permission_narrowed=lambda name: True,
    )

    plan = planner.plan("session")

    klasses = {call.tool_name: call.klass for call in plan.decisions}
    assert klasses["read_file"] == REPLAY_WITH_RESULT
    assert klasses["write_file"] == BLOCK


def test_anchor_comes_from_last_completed_step(tmp_path):
    manager, journal = _setup(tmp_path)
    _emit(journal, name="read_file")
    planner = ResumePlanner(
        manager, _Registry({"read_file": _Spec(read_only=True, scope="none")})
    )

    plan = planner.plan("session")

    assert plan.anchor.messages_sha256 == "m" * 64
    assert plan.anchor.system_sha256 == "s" * 64
    assert plan.anchor.tools_sha256 == "t" * 64
    assert plan.anchor.cache_lane_sha256 == "c" * 64
    assert plan.anchor.message_count == 3
    assert plan.anchor.complete() is True


def test_aborts_reports_missing_anchor(tmp_path):
    """一个 step 都没完成 → 没有锚点，必须让用户手动重来（R-T8 同源）。"""
    manager = MemoryManager(str(tmp_path / "resume.db"))
    manager.create_session_explicit("session")
    journal = HarnessJournal(manager, "owner-test", clock_ms=lambda: 10_000)
    journal.heartbeat()
    journal.create_turn("turn-parent", "session")
    journal.create_step("turn-parent:step:1", "turn-parent", 1)
    manager.conn.execute(
        "UPDATE harness_turns SET status = 'interrupted' WHERE turn_id = 'turn-parent'"
    )
    manager.conn.execute(
        "UPDATE harness_steps SET status = 'tools_running' WHERE step_id = 'turn-parent:step:1'"
    )
    manager.conn.commit()
    planner = ResumePlanner(manager, _Registry({}))

    plan = planner.plan("session")

    assert plan is not None
    assert plan.resume_from_step_ordinal == 1
    assert plan.anchor.complete() is False
    assert "anchor_missing" in planner.aborts(plan)


def test_aborts_reports_owner_archived_and_undo(tmp_path):
    manager, journal = _setup(tmp_path)
    _emit(journal, name="read_file")
    planner = ResumePlanner(
        manager,
        _Registry({"read_file": _Spec(read_only=True, scope="none")}),
        owner_alive=lambda turn_id: True,
        session_archived=lambda name: True,
        undo_recovering=lambda name: True,
    )

    plan = planner.plan("session")

    reasons = planner.aborts(plan)
    assert "owner_still_alive" in reasons
    assert "session_archived" in reasons
    assert "undo_recovering" in reasons


def test_probe_failure_is_treated_as_not_hit(tmp_path):
    """探针自身报错不能让续跑彻底不可用：按未命中处理，兜底交给 S3 哈希断言。"""
    manager, journal = _setup(tmp_path)
    _emit(journal, name="read_file")

    def _boom(_name):
        raise RuntimeError("探针炸了")

    planner = ResumePlanner(
        manager,
        _Registry({"read_file": _Spec(read_only=True, scope="none")}),
        session_archived=_boom,
    )

    plan = planner.plan("session")

    assert plan is not None
    assert "session_archived" not in planner.aborts(plan)