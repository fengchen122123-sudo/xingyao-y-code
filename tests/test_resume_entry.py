"""断点续跑 S4「入口与开关」单测：GUI 桥接 resume_plan / resume_execute。

覆盖四件事：
1. 开关关闭（默认）时两个入口都回 disabled，且零副作用（不写库、不发请求）；
2. 开关打开且存在可续跑回合时，plan 快照字段齐全（父回合 / 断点位置 / 锚点 /
   判定 / blockers / aborts）；
3. aborts 非空时 resume_execute 直接回原因，绝不构造/调用执行层；
4. 桥接内部异常被吞成 {"ok": False, "msg": ...}，不冒到前端。

另有「真实接线」回归（S4 收尾）：
5. 门禁通过时 resume_execute 返回 dry_run=False，request_fn 被调用一次且关键字为
   turn_id / parent_turn_id / request_reason="resume"，线程起点收到同一 turn_id；
6. 续跑不改写父回合历史（messages 零新增、父 turn 行逐字段未变）；
7. ResumeExecutor 拿到的是同一 owner 租约的 HarnessJournal；manager 不支持
   journal 存储时 writer 回退 None 且不报错；
8. 续跑 worker 内部抛异常不吞：turn_error + notice + turn_finish 都可见。
"""
import threading
import types

import pytest

import config
import core.agent_service as agent_service
import core.harness.resume as resume_mod
import core.tool_registry as tool_registry
import gui_bridge as gui_bridge_mod
from core.harness.journal import PROCESS_OWNER_ID, HarnessJournal
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


# ---------------------------------------------------------------------------
# 真实接线回归（S4 收尾）：resume_execute -> ResumeExecutor(writer, request_fn)
# -> daemon 线程起点。全程同步替身，绝不发真实模型请求、不 sleep。
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    import time

    return time.time_ns() // 1_000_000


def _refresh_owner_heartbeat(manager, owner_id="owner-test"):
    """把测试 owner 的心跳刷到当前时刻。

    resume_execute 注入的 writer 用真实时钟且构造即 recover_stale；不刷新的话
    10s 过期判定会把父回合 tools_running 的 step 踩成 interrupted（与本用例
    无关的噪声，掩盖真正要断言的"父回合未被改动"）。
    """
    HarnessJournal(manager, owner_id, clock_ms=_now_ms)


class _SyncThread:
    """threading.Thread 同步替身：start() 立刻在当前线程执行 target。"""

    created: list = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target = target
        self.args = tuple(args)
        self.kwargs = dict(kwargs or {})
        self.daemon = daemon
        self.name = name
        _SyncThread.created.append(self)

    def start(self):
        self.target(*self.args, **self.kwargs)


def _sync_threads(monkeypatch):
    """只替换 gui_bridge 模块内的 threading 引用，不动全局 threading 模块。"""
    _SyncThread.created = []
    monkeypatch.setattr(
        gui_bridge_mod, "threading", types.SimpleNamespace(Thread=_SyncThread)
    )


class _RecordingExecutor(resume_mod.ResumeExecutor):
    """真执行层 + 记录 request_fn 收到的关键字（不改变任何原有行为）。"""

    instances: list = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.request_calls = []
        forward = self._request_fn
        self._request_fn = self._recording_request(forward)
        _RecordingExecutor.instances.append(self)

    def _recording_request(self, forward):
        def _request(**kwargs):
            self.request_calls.append(dict(kwargs))
            return forward(**kwargs)

        return _request


def _recording_executor(monkeypatch):
    _RecordingExecutor.instances = []
    monkeypatch.setattr(resume_mod, "ResumeExecutor", _RecordingExecutor)


def _patch_registry(monkeypatch):
    monkeypatch.setattr(
        tool_registry,
        "build_default_registry",
        lambda *a, **k: _Registry({"read_file": _Spec()}),
    )


def _stub_resume_worker(bridge, calls):
    """worker 替身：只记录线程起点收到的参数，绝不发模型请求。"""

    def _worker(session_name, *, turn_id, parent_turn_id):
        calls.append(
            {
                "session_name": session_name,
                "turn_id": turn_id,
                "parent_turn_id": parent_turn_id,
            }
        )

    bridge._run_resume_turn_worker = _worker


def _parent_turn_row(manager):
    return manager.conn.execute(
        "SELECT turn_id, session_id, owner_id, status, started_at_ms, finished_at_ms, "
        "last_sequence, error_code, parent_turn_id, turn_kind, resume_from_step_ordinal "
        "FROM harness_turns WHERE turn_id = 'turn-parent'"
    ).fetchone()


def test_execute_wires_request_fn_and_returns_dry_run_false(tmp_path, monkeypatch):
    manager = _setup(tmp_path)
    _refresh_owner_heartbeat(manager)
    _switch(monkeypatch, True)
    _patch_registry(monkeypatch)
    _recording_executor(monkeypatch)
    _sync_threads(monkeypatch)
    worker_calls = []
    bridge = _bridge(manager)
    _stub_resume_worker(bridge, worker_calls)

    payload = bridge.resume_execute("session", [])

    assert payload["ok"] is True
    # 真实接线后不再是 dry-run
    assert payload["dry_run"] is False
    assert payload["parent_turn_id"] == "turn-parent"
    assert payload["replayed"] == 1

    assert len(_RecordingExecutor.instances) == 1
    executor = _RecordingExecutor.instances[0]
    # 执行层只发一次请求，关键字锁定续跑口径
    assert executor.request_calls == [
        {
            "turn_id": payload["turn_id"],
            "parent_turn_id": "turn-parent",
            "request_reason": "resume",
        }
    ]
    # 请求经一个 daemon 线程起，线程起点拿到同一个 turn_id / parent_turn_id
    assert len(_SyncThread.created) == 1
    assert _SyncThread.created[0].daemon is True
    assert worker_calls == [
        {
            "session_name": "session",
            "turn_id": payload["turn_id"],
            "parent_turn_id": "turn-parent",
        }
    ]


def test_execute_passes_owner_shared_journal_writer(tmp_path, monkeypatch):
    manager = _setup(tmp_path)
    _refresh_owner_heartbeat(manager)
    _switch(monkeypatch, True)
    _patch_registry(monkeypatch)
    _recording_executor(monkeypatch)
    _sync_threads(monkeypatch)
    bridge = _bridge(manager)
    _stub_resume_worker(bridge, [])

    bridge.resume_execute("session", [])

    executor = _RecordingExecutor.instances[0]
    # writer 不能是执行层自建的 harness-resume-<uuid>：必须与回合运行时同一 owner，
    # 否则 10s 后 recover_stale 会把刚起的续跑 turn 自己踩断
    assert isinstance(executor._writer, HarnessJournal)
    assert executor._writer.owner_id == PROCESS_OWNER_ID


def test_resume_journal_writer_falls_back_to_none_without_journal_storage():
    bridge = _bridge(object())  # 鸭子类型 manager：没有任何 harness_* 存储接口

    # 不支持 journal 时回退 None（交执行层按原逻辑自建），绝不抛错
    assert bridge._resume_journal_writer() is None


def test_execute_does_not_touch_parent_history(tmp_path, monkeypatch):
    manager = _setup(tmp_path)
    manager.add_messages(
        "session", [("user", "原始问题"), ("assistant", "原始回答")]
    )
    _refresh_owner_heartbeat(manager)
    _switch(monkeypatch, True)
    _patch_registry(monkeypatch)
    _recording_executor(monkeypatch)
    _sync_threads(monkeypatch)
    bridge = _bridge(manager)
    _stub_resume_worker(bridge, [])

    history_before = manager.get_history("session")
    llm_history_before = manager.get_history_for_llm("session")
    parent_before = _parent_turn_row(manager)
    assert parent_before is not None
    assert parent_before[3] == "interrupted"
    assert [item["role"] for item in history_before] == ["user", "assistant"]

    payload = bridge.resume_execute("session", [])

    assert payload["ok"] is True
    # 写 user 消息是明确的违规：历史条数、内容、LLM 口径都必须逐条不变
    assert manager.get_history("session") == history_before
    assert manager.get_history_for_llm("session") == llm_history_before
    # 父回合行逐字段未变（仍 interrupted），续跑另起一行新 turn
    assert _parent_turn_row(manager) == parent_before
    assert payload["turn_id"] != "turn-parent"


def test_resume_worker_surfaces_exception_as_visible_error(tmp_path, monkeypatch):
    manager = MemoryManager(str(tmp_path / "resume-worker.db"))
    manager.create_session_explicit("session")
    manager.add_messages("session", [("user", "原始问题")])
    events = []
    bridge = _bridge(manager)
    bridge._active_turns = {}
    bridge._active_turn_ids = {}
    bridge._active_turn_snapshots = {}
    bridge._active_turn_lock = threading.Lock()
    bridge._turn_owner_lock = threading.Lock()
    bridge._turn_owner_sessions = {}
    bridge._cancel_events = {}
    bridge.emit_event = lambda kind, payload=None: events.append((kind, dict(payload or {})))
    bridge._get_cancel_event = lambda session_name: threading.Event()
    bridge._session_workdir = lambda session_name: str(tmp_path)
    bridge._effective_permission_mode = lambda session_name: "confirm"
    bridge._sync_active_model_context = lambda *a, **k: None
    bridge._emit_message_final = lambda *a, **k: None
    bridge._recalc_token_usage = lambda *a, **k: None
    bridge._emit_token_usage = lambda *a, **k: None
    bridge._clear_session_last_error = lambda *a, **k: None
    bridge._record_session_last_error = lambda *a, **k: None
    bridge._finalize_session_deltas = lambda *a, **k: None
    monkeypatch.setattr(
        gui_bridge_mod,
        "get_active_model",
        lambda session_name: {
            "litellm_model": "openai/model-one",
            "api_key": "",
            "base_url": "",
            "id": "m1",
        },
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("组装爆炸")

    monkeypatch.setattr(agent_service, "build_resume_messages", _boom)

    bridge._run_resume_turn_worker(
        "session", turn_id="resume-0000abcd", parent_turn_id="turn-parent"
    )

    kinds = [kind for kind, _payload in events]
    terminals = [payload for kind, payload in events if kind == "turn_error"]
    assert terminals and "组装爆炸" in terminals[0]["message"]
    notices = [payload.get("message", "") for kind, payload in events if kind == "notice"]
    assert any("断点续跑失败" in message and "组装爆炸" in message for message in notices)
    # 前端不会卡在 running：终态与解锁事件必须补发
    assert "turn_finish" in kinds


def test_consume_turn_events_passes_resume_kwargs_only_on_resume_path(monkeypatch):
    """resume_turn / request_reason 只在续跑路径显式传入，普通回合逐字不变。"""
    import core.llm as llm_mod

    captured = []

    def _fake_chat(*args, **kwargs):
        captured.append(dict(kwargs))
        return iter(())  # 零事件：只关心透传给模型入口的关键字

    monkeypatch.setattr(llm_mod, "chat_with_model_events", _fake_chat)
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._cancel_events = {}
    bridge._get_cancel_event = lambda session_name: threading.Event()
    bridge.emit_event = lambda *a, **k: None

    resumed = bridge._consume_turn_events(
        [],
        "openai/model-one",
        "",
        "",
        "session",
        "resume-abc123",
        parent_turn_id="turn-parent",
        request_reason="resume",
        resume_turn=True,
    )
    plain = bridge._consume_turn_events([], "openai/model-one", "", "", "session", "turn-plain")

    assert resumed is False and plain is False
    assert captured[0]["parent_turn_id"] == "turn-parent"
    assert captured[0]["request_reason"] == "resume"
    # resume_turn=True 让 TurnRuntime.start 跳过 create_turn（turn 行已由执行层建好）
    assert captured[0]["resume_turn"] is True
    # 普通回合：父子同 id、空 request_reason、resume_turn=False
    assert captured[1]["parent_turn_id"] == "turn-plain"
    assert captured[1]["request_reason"] == ""
    assert captured[1]["resume_turn"] is False