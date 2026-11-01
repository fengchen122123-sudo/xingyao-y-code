"""断点续跑 S4「入口与开关」单测：GUI 桥接 resume_plan / resume_execute。

覆盖四件事：
1. 开关关闭（默认）时两个入口都回 disabled，且零副作用（不写库、不发请求）；
2. 开关打开且存在可续跑回合时，plan 快照字段齐全（父回合 / 断点位置 / 锚点 /
   判定 / blockers / aborts）；
3. aborts 非空时 resume_execute 直接回原因，绝不构造/调用执行层；
4. 桥接内部异常被吞成 {"ok": False, "msg": ...}，不冒到前端。
"""
import pytest

import config
import core.harness.resume as resume_mod
import core.tool_registry as tool_registry
from core.harness.journal import HarnessJournal
from core.harness.models import HarnessEventKind, JournalEventDraft
from core.memory import MemoryManager
from gui_bridge import ApiBridge


class _Spec:
    """工具规格替身：只读、无副作用、内置来源。"""

    read_only = True
    side_effect_scope = "none"
    source = "builtin"


class _Registry:
    def __init__(self, specs):
        self._specs = dict(specs)

    def get(self, name):
        if name not in self._specs:
            raise KeyError(name)
        return self._specs[name]

    def source_of(self, name):
        return "builtin"


def _bridge(manager):
    """不走 __init__（会拉起窗口队列等依赖），只注入续跑需要的管理器。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = manager
    return bridge


def _switch(monkeypatch, enabled):
    payload = {"resume": {"enabled": bool(enabled)}}
    monkeypatch.setattr(config, "_load", lambda managed=True: payload)


def _setup(tmp_path, *, completed_step=True):
    """父回合：step1 已完成（带四哈希锚点），step2 停在 tools_running。"""
    manager = MemoryManager(str(tmp_path / "resume-entry.db"))
    manager.create_session_explicit("session")
    journal = HarnessJournal(manager, "owner-test", clock_ms=lambda: 10_000)
    journal.heartbeat()
    journal.create_turn("turn-parent", "session")
    journal.create_step("turn-parent:step:1", "turn-parent", 1)
    journal.create_step("turn-parent:step:2", "turn-parent", 2)
    manager.conn.execute(
        "UPDATE harness_turns SET status = 'interrupted' WHERE turn_id = 'turn-parent'"
    )
    if completed_step:
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
    journal.append_event(
        "turn-parent",
        JournalEventDraft(
            kind=HarnessEventKind.TOOL_STARTED,
            step_id="turn-parent:step:2",
            tool_name="read_file",
            call_ordinal=0,
            arguments_sha256="a" * 64,
        ),
    )
    journal.append_event(
        "turn-parent",
        JournalEventDraft(
            kind=HarnessEventKind.TOOL_FINISHED,
            step_id="turn-parent:step:2",
            tool_name="read_file",
            call_ordinal=0,
            result_sha256="r" * 64,
            result_status="ok",
        ),
    )
    return manager


def _counts(manager):
    turns = manager.conn.execute("SELECT COUNT(*) FROM harness_turns").fetchone()[0]
    steps = manager.conn.execute("SELECT COUNT(*) FROM harness_steps").fetchone()[0]
    events = manager.conn.execute("SELECT COUNT(*) FROM harness_events").fetchone()[0]
    return turns, steps, events


def test_switch_off_returns_disabled_without_side_effects(tmp_path, monkeypatch):
    manager = _setup(tmp_path)
    _switch(monkeypatch, False)
    bridge = _bridge(manager)
    before = _counts(manager)

    plan_payload = bridge.resume_plan("session")
    exec_payload = bridge.resume_execute("session", [])
    exec_payload_confirmed = bridge.resume_execute("session", [0])

    assert plan_payload == {"ok": False, "reason": "disabled"}
    assert exec_payload == {"ok": False, "reason": "disabled"}
    assert exec_payload_confirmed == {"ok": False, "reason": "disabled"}
    # 零副作用：不建 turn、不写 step、不写事件、不发请求（executor 未接线）
    assert _counts(manager) == before


def test_switch_read_failure_falls_back_to_disabled(tmp_path, monkeypatch):
    manager = _setup(tmp_path)

    def _boom(managed=True):
        raise RuntimeError("config broken")

    monkeypatch.setattr(config, "_load", _boom)
    bridge = _bridge(manager)

    assert bridge.resume_plan("session") == {"ok": False, "reason": "disabled"}
    assert bridge.resume_execute("session", []) == {"ok": False, "reason": "disabled"}


def test_switch_on_plan_snapshot_is_complete(tmp_path, monkeypatch):
    manager = _setup(tmp_path)
    _switch(monkeypatch, True)
    monkeypatch.setattr(
        tool_registry, "build_default_registry", lambda *a, **k: _Registry({"read_file": _Spec()})
    )
    bridge = _bridge(manager)
    before = _counts(manager)

    payload = bridge.resume_plan("session")

    assert payload["ok"] is True
    assert payload["resumable"] is True
    assert payload["aborts"] == []
    plan = payload["plan"]
    for key in (
        "session_name",
        "parent_turn_id",
        "resume_from_step_ordinal",
        "anchor",
        "decisions",
        "blockers",
        "aborts",
    ):
        assert key in plan
    assert plan["session_name"] == "session"
    assert plan["parent_turn_id"] == "turn-parent"
    assert plan["resume_from_step_ordinal"] == 2
    assert plan["anchor"]["complete"] is True
    assert plan["anchor"]["message_count"] == 3
    assert plan["decisions"][0]["call_ordinal"] == 0
    assert plan["decisions"][0]["tool_name"] == "read_file"
    assert plan["decisions"][0]["klass"] == "replay_with_result"
    assert plan["decisions"][0]["reason"] == "read_replay"
    assert plan["replayable_count"] == 1
    assert plan["blocked_count"] == 0
    assert plan["blockers"] == []
    # 规划接口只读：不建 turn、不写事件
    assert _counts(manager) == before


def test_plan_without_interrupted_turn_is_not_resumable(tmp_path, monkeypatch):
    manager = MemoryManager(str(tmp_path / "resume-empty.db"))
    manager.create_session_explicit("session")
    _switch(monkeypatch, True)
    bridge = _bridge(manager)

    payload = bridge.resume_plan("session")

    assert payload["ok"] is True
    assert payload["resumable"] is False
    assert payload["reason"] == "no_resumable_turn"
    assert payload["aborts"] == ["no_resumable_turn"]


def test_execute_returns_abort_reason_without_touching_executor(tmp_path, monkeypatch):
    manager = _setup(tmp_path, completed_step=False)  # 没有已完成 Step -> 无锚点
    _switch(monkeypatch, True)
    monkeypatch.setattr(
        tool_registry, "build_default_registry", lambda *a, **k: _Registry({"read_file": _Spec()})
    )
    calls = []

    class _ExplodingExecutor:
        def __init__(self, *args, **kwargs):
            calls.append("init")

        def run(self, *args, **kwargs):
            calls.append("run")
            raise AssertionError("aborts 非空时不得构造/调用执行层")

    monkeypatch.setattr(resume_mod, "ResumeExecutor", _ExplodingExecutor)
    bridge = _bridge(manager)

    payload = bridge.resume_execute("session", [])

    assert payload["ok"] is False
    assert payload["reason"] == "anchor_missing"
    assert payload["reasons"] == ["anchor_missing"]
    assert calls == []


def test_bridge_exceptions_are_swallowed(tmp_path, monkeypatch):
    manager = _setup(tmp_path)
    _switch(monkeypatch, True)

    class _BoomPlanner:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("planner boom")

    monkeypatch.setattr(resume_mod, "ResumePlanner", _BoomPlanner)
    bridge = _bridge(manager)

    plan_payload = bridge.resume_plan("session")
    exec_payload = bridge.resume_execute("session", [])

    assert plan_payload["ok"] is False
    assert "断点续跑规划失败" in plan_payload["msg"]
    assert exec_payload["ok"] is False
    assert "断点续跑执行失败" in exec_payload["msg"]