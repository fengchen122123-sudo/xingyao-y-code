import os
import threading

import pytest

from core.turn_events import TurnEventFactory
from gui_bridge import (
    ApiBridge,
    _ensure_session_model_binding,
    _session_from_turn_id,
)


def make_events(items):
    factory = TurnEventFactory("turn-gui-tools")
    return [factory.emit(kind, payload) for kind, payload in items]


def test_session_from_turn_id_uses_the_prefix_before_the_last_separator():
    assert _session_from_turn_id("session-a:turn-123") == "session-a"
    assert _session_from_turn_id("renamed:session:turn-123") == "renamed:session"
    assert _session_from_turn_id("") == ""


def test_session_is_created_before_model_binding_and_unchanged_binding_skips_update():
    """首回合必须先建 session；第二回合同模型不得重复 UPDATE+commit。"""
    calls = []

    class Memory:
        model_id = None

        def ensure_session(self, session_name, workdir=None, source="GUI"):
            calls.append(("ensure", session_name, workdir, source))

        def get_session_model_id(self, session_name):
            calls.append(("get", session_name))
            return self.model_id

        def set_session_model_id(self, session_name, model_id):
            calls.append(("set", session_name, model_id))
            self.model_id = model_id

    memory = Memory()
    _ensure_session_model_binding(memory, "session-a", "m1", "GLOBAL")
    _ensure_session_model_binding(memory, "session-a", "m1", "GLOBAL")

    assert calls == [
        ("ensure", "session-a", "GLOBAL", "GUI"),
        ("get", "session-a"),
        ("set", "session-a", "m1"),
        ("ensure", "session-a", "GLOBAL", "GUI"),
        ("get", "session-a"),
    ]


def test_worker_model_snapshot_does_not_overwrite_newer_session_binding():
    """worker 的旧模型 A 不得覆盖用户并发切换后已经落库的 B。"""
    calls = []

    class Memory:
        model_id = "model-B"

        def ensure_session(self, session_name, workdir=None, source="GUI"):
            calls.append(("ensure", session_name))

        def get_session_model_id(self, session_name):
            calls.append(("get", session_name))
            return self.model_id

        def set_session_model_id(self, session_name, model_id):
            calls.append(("set", session_name, model_id))
            self.model_id = model_id

    memory = Memory()

    _ensure_session_model_binding(memory, "session-a", "model-A", "GLOBAL")

    assert memory.model_id == "model-B"
    assert calls == [("ensure", "session-a"), ("get", "session-a")]


def start_payload(tool_id):
    return {
        "id": tool_id,
        "name": "execute_command",
        "summary": "运行命令",
        "display": {
            "category": "command",
            "action": "运行命令",
            "target": "pytest -q",
            "command": "pytest -q",
            "metrics": {},
            "error_summary": "",
            "is_skill": False,
        },
    }


def finish_payload(tool_id, ok, duration_ms):
    payload = start_payload(tool_id)
    payload.update({
        "ok": ok,
        "duration_ms": duration_ms,
        "summary": "命令执行失败" if not ok else "命令执行完成",
        "result": "退出码 1" if not ok else "passed",
        "error": "ModuleNotFoundError: pytest" if not ok else "",
        "display": {
            **payload["display"],
            "error_summary": "ModuleNotFoundError: pytest" if not ok else "",
        },
    })
    return payload


def consume_bridge_events(monkeypatch, events):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._cancel_events = {"session": threading.Event()}
    bridge._active_turns = {}
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_lock = threading.Lock()
    bridge._delta_last_flush = 0.0
    bridge._delta_flusher = None
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))
    monkeypatch.setattr(
        "core.llm.chat_with_model_events",
        lambda *args, **kwargs: iter(events),
    )
    bridge._consume_turn_events(
        [], "model", "key", None, "session", "turn-gui-tools"
    )
    return emitted


def test_bridge_deduplicates_start_and_forwards_structured_fields(monkeypatch):
    monkeypatch.setattr("gui_bridge.time.time", lambda: 1234.5)
    events = make_events([
        ("tool_requested", start_payload("cmd-1")),
        ("tool_started", start_payload("cmd-1")),
        ("tool_finished", finish_payload("cmd-1", ok=False, duration_ms=3200)),
        ("turn_finished", {}),
    ])

    emitted = consume_bridge_events(monkeypatch, events)

    starts = [data for kind, data in emitted if kind == "tool_start"]
    finishes = [data for kind, data in emitted if kind == "tool_finish"]
    assert len(starts) == 1
    assert starts[0]["started_at_ms"] == 1234500
    assert starts[0]["display"]["category"] == "command"
    assert finishes[0]["duration_ms"] == 3200
    assert finishes[0]["error"] == "ModuleNotFoundError: pytest"
    assert finishes[0]["display"]["error_summary"] == (
        "ModuleNotFoundError: pytest"
    )


def test_bridge_does_not_infer_finish_error_from_result(monkeypatch):
    payload = finish_payload("cmd-2", ok=False, duration_ms=10)
    del payload["error"]
    payload["result"] = "failure-looking raw result"
    events = make_events([
        ("tool_finished", payload),
        ("turn_finished", {}),
    ])

    emitted = consume_bridge_events(monkeypatch, events)

    finish = next(data for kind, data in emitted if kind == "tool_finish")
    assert finish["error"] == ""


def test_bridge_emits_one_start_for_empty_tool_id(monkeypatch):
    events = make_events([
        ("tool_requested", start_payload("")),
        ("tool_started", start_payload("")),
        ("turn_finished", {}),
    ])

    emitted = consume_bridge_events(monkeypatch, events)

    starts = [data for kind, data in emitted if kind == "tool_start"]
    assert len(starts) == 1
    assert starts[0]["id"] == ""


def test_bridge_forwards_diff_binding_and_full_counts(monkeypatch):
    events = make_events([
        ("diff_ready", {
            "tool_call_id": "edit-1",
            "file_path": "a.py",
            "diff_text": "--- a.py\n+++ a.py\n-old\n+new",
            "added": 120,
            "removed": 45,
        }),
        ("turn_finished", {}),
    ])

    emitted = consume_bridge_events(monkeypatch, events)

    diff = next(data for kind, data in emitted if kind == "diff_ready")
    assert diff["tool_call_id"] == "edit-1"
    assert diff["added"] == 120
    assert diff["removed"] == 45


def test_bridge_error_then_turn_error_has_single_message_owner(monkeypatch):
    events = make_events([
        ("error", {"message": "provider disconnected"}),
        ("turn_error", {"message": "provider disconnected"}),
    ])

    emitted = consume_bridge_events(monkeypatch, events)

    # error/turn_error 现在附带 session_name，只校验 message 字段
    error_msgs = [data.get("message") for kind, data in emitted if kind == "error"]
    turn_error_msgs = [data.get("message") for kind, data in emitted if kind == "turn_error"]
    assert error_msgs == ["provider disconnected"]
    assert turn_error_msgs == ["provider disconnected"]


def test_worker_exception_emits_turn_error_and_error_toast(monkeypatch):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._bg_summary_pending_sessions = set()
    bridge._bg_summary_lock = threading.Lock()
    bridge._active_turn_ids = {}
    bridge._cancel_events = {"session": threading.Event()}
    bridge._active_turns = {}
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_lock = threading.Lock()
    bridge._delta_last_flush = 0.0
    bridge._delta_flusher = None
    bridge._emit_token_usage = lambda: None
    bridge._shutting_down = False
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))
    monkeypatch.setattr(
        "gui_bridge.get_active_model",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("provider disconnected")),
    )

    bridge._run_turn_worker("hello", "session")

    # error/turn_error 现在附带 session_name
    assert any(kind == "turn_error" and data.get("message") == "provider disconnected" for kind, data in emitted)
    assert any(kind == "error" and data.get("message") == "provider disconnected" for kind, data in emitted)


def test_worker_activates_session_workdir_before_context(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.chdir(launch)

    bridge = ApiBridge.__new__(ApiBridge)
    bridge._cancel_events = {"project-session": threading.Event()}
    bridge._active_turns = {}
    bridge._active_turn_ids = {}
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_lock = threading.Lock()
    bridge._delta_last_flush = 0.0
    bridge._delta_flusher = None
    bridge._bg_summary_pending_sessions = set()
    bridge._bg_summary_lock = threading.Lock()
    bridge._current_permission_mode = "confirm"
    bridge._emit_token_usage = lambda: None
    bridge._sync_active_model_context = lambda *_args: None
    bridge._session_workdir = lambda _session_name: str(project)
    bridge._set_current_session = lambda _session_name: None
    bridge._subagent_progress_poller = lambda _session_name, _stop: None
    bridge._consume_turn_events = lambda *args, **kwargs: False
    bridge._maybe_auto_rename = lambda _session_name: None
    bridge._shutting_down = False
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._memory = type("Memory", (), {
        "ensure_session": lambda self, *args, **kwargs: None,
        "get_session_model_id": lambda self, *args: None,
        "set_session_model_id": lambda self, *args: None,
        "add_message": lambda self, *args, **kwargs: None,
        "set_workdir": lambda self, workdir: None,
    })()
    bridge.emit_event = lambda _kind, _data: None
    seen = {}

    monkeypatch.setattr("gui_bridge.get_active_model", lambda *_args: {
        "litellm_model": "model",
        "api_key": "key",
        "base_url": None,
        "context_window": 128000,
    })
    monkeypatch.setattr("core.input_parser.parse_user_input", lambda text: (text, []))

    def capture_context(*args, **kwargs):
        # 多会话并行改造后，workdir 绑定在线程本地上下文而不再是进程 cwd
        from core.workspace_context import get_workdir
        seen["cwd"] = str(get_workdir())
        return []

    monkeypatch.setattr("core.agent_service.prepare_turn_messages", capture_context)
    monkeypatch.setattr("core.memory.begin_turn_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.memory.end_turn_checkpoint", lambda: None)

    bridge._run_turn_worker("inspect", "project-session")

    assert os.path.realpath(seen["cwd"]) == os.path.realpath(project)
    # 回合结束后线程本地 workdir 必须恢复，不能泄漏到本回合之外
    from core.workspace_context import get_workdir
    assert str(get_workdir()) == os.path.realpath(launch)


def test_worker_emits_attachment_notes_inline_and_finishes_with_renamed_session(monkeypatch):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._cancel_events = {"session_abc123": threading.Event()}
    bridge._active_turns = {}
    bridge._active_turn_ids = {}
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_lock = threading.Lock()
    bridge._delta_last_flush = 0.0
    bridge._delta_flusher = None
    bridge._bg_summary_pending_sessions = set()
    bridge._bg_summary_lock = threading.Lock()
    bridge._current_permission_mode = "confirm"
    bridge._emit_token_usage = lambda: None
    bridge._sync_active_model_context = lambda *_args: None
    bridge._session_workdir = lambda _session_name: "GLOBAL"
    bridge._subagent_progress_poller = lambda _session_name, _stop: None
    bridge._consume_turn_events = lambda *args, **kwargs: False
    bridge._maybe_auto_rename = lambda _session_name: "检查项目"
    bridge._shutting_down = False
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._memory = type("Memory", (), {
        "ensure_session": lambda self, *args, **kwargs: None,
        "get_session_model_id": lambda self, *args: None,
        "set_session_model_id": lambda self, *args: None,
        "add_message": lambda self, *args, **kwargs: None,
        "set_workdir": lambda self, workdir: None,
    })()
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))

    monkeypatch.setattr("gui_bridge.get_active_model", lambda *_args: {
        "litellm_model": "model",
        "api_key": "key",
        "base_url": None,
        "context_window": 128000,
    })
    monkeypatch.setattr(
        "core.input_parser.parse_user_input",
        lambda text: (text, ["🖼️ 已附加图片: sample.png（压缩后 320KB）"]),
    )
    monkeypatch.setattr(
        "core.agent_service.prepare_turn_messages", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        "core.memory.begin_turn_checkpoint", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("core.memory.end_turn_checkpoint", lambda: None)

    bridge._run_turn_worker("检查项目", "session_abc123")

    attachment_events = [
        data for kind, data in emitted if kind == "user_attachment_notes"
    ]
    assert attachment_events == [{
        "session_name": "session_abc123",
        "notes": ["🖼️ 已附加图片: sample.png（压缩后 320KB）"],
    }]
    assert not any(
        kind == "notice" and "sample.png" in data.get("message", "")
        for kind, data in emitted
    )
    finishes = [data for kind, data in emitted if kind == "turn_finish"]
    assert finishes == [{"session_name": "检查项目"}]


@pytest.mark.parametrize(
    ("yielded_message", "raised_message", "expected_toast_messages"),
    [
        (
            "provider disconnected",
            "provider disconnected",
            ["provider disconnected"],
        ),
        (
            "provider disconnected",
            "transport cleanup failed",
            ["provider disconnected", "transport cleanup failed"],
        ),
    ],
)
def test_worker_owns_terminal_state_without_hiding_distinct_errors(
    monkeypatch,
    yielded_message,
    raised_message,
    expected_toast_messages,
):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._cancel_events = {"session": threading.Event()}
    bridge._active_turns = {}
    bridge._active_turn_ids = {}
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_lock = threading.Lock()
    bridge._delta_last_flush = 0.0
    bridge._delta_flusher = None
    bridge._bg_summary_pending_sessions = set()
    bridge._bg_summary_lock = threading.Lock()
    bridge._current_permission_mode = "confirm"
    bridge._emit_token_usage = lambda: None
    bridge._sync_active_model_context = lambda *_args: None
    bridge._session_workdir = lambda session_name: "."
    bridge._set_current_session = lambda session_name: None
    bridge._subagent_progress_poller = lambda session_name, stop: None
    bridge._maybe_auto_rename = lambda session_name: None
    bridge._shutting_down = False
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._memory = type("Memory", (), {
        "set_workdir": lambda self, workdir: None,
        "ensure_session": lambda self, *args, **kwargs: None,
        "get_session_model_id": lambda self, *args: None,
        "set_session_model_id": lambda self, *args: None,
        "add_message": lambda self, *args, **kwargs: None,
    })()
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))

    def failing_events(*args, **kwargs):
        factory = TurnEventFactory("turn-gui-tools")
        yield factory.emit("turn_error", {"message": yielded_message})
        raise RuntimeError(raised_message)

    monkeypatch.setattr("gui_bridge.get_active_model", lambda *_args: {
        "litellm_model": "model",
        "api_key": "key",
        "base_url": None,
        "context_window": 128000,
    })
    monkeypatch.setattr("core.input_parser.parse_user_input", lambda text: (text, []))
    monkeypatch.setattr(
        "core.agent_service.prepare_turn_messages", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        "core.memory.begin_turn_checkpoint", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("core.memory.end_turn_checkpoint", lambda: None)
    monkeypatch.setattr("core.llm.chat_with_model_events", failing_events)

    bridge._run_turn_worker("hello", "session")

    assert [
        data["message"] for kind, data in emitted if kind == "error"
    ] == expected_toast_messages
    assert [
        data["message"] for kind, data in emitted if kind == "turn_error"
    ] == [yielded_message]
    assert [kind for kind, _ in emitted].count("turn_finish") == 1


def test_bridge_set_reasoning_choice_returns_model_snapshot(monkeypatch):
    """设置成功必须返回当前模型的精确选项，而不是通用档位。"""
    bridge = ApiBridge.__new__(ApiBridge)
    monkeypatch.setattr(
        "gui_bridge.set_model_reasoning_choice",
        lambda model_id, choice: (True, "模型 m1 思考设置已设为 medium"),
    )
    monkeypatch.setattr("gui_bridge.get_model_reasoning_choice", lambda model_id: "medium")
    monkeypatch.setattr(
        "gui_bridge.get_active_model",
        lambda: {"id": "m1", "name": "qwen3.8-max", "litellm_model": "openai/qwen3.8-max"},
    )
    monkeypatch.setattr(
        "gui_bridge.load_config",
        lambda: {
            "active_model_id": "m1",
            "providers": [{
                "id": "p1",
                "name": "测试厂商",
                "base_url": "https://example.test/v1",
                "api_key": "encrypted:test",
                "protocol": "openai",
                "models": [{
                    "id": "m1",
                    "name": "qwen3.8-max",
                    "litellm_model": "openai/qwen3.8-max",
                    "context_window": 1000000,
                }],
            }],
        },
    )

    res = bridge.set_reasoning_choice("m1", "medium")

    assert res["ok"] is True
    assert res["reasoning"]["choice"] == "medium"
    assert [item["value"] for item in res["reasoning"]["options"]] == [
        "default", "off", "low", "medium", "xhigh",
    ]


def test_bridge_set_reasoning_choice_requires_model_id(monkeypatch):
    """前端没有给出按钮对应模型时不能猜测全局模型。"""
    bridge = ApiBridge.__new__(ApiBridge)

    res = bridge.set_reasoning_choice("", "low")

    assert res["ok"] is False
    assert "激活模型" in res["msg"]


def test_bridge_set_reasoning_choice_exposes_fixed_model_as_disabled(monkeypatch):
    """固定思考模型的桥接快照必须锁定为开启。"""
    bridge = ApiBridge.__new__(ApiBridge)
    monkeypatch.setattr(
        "gui_bridge.set_model_reasoning_choice",
        lambda model_id, choice: (False, "该模型始终开启思考"),
    )
    monkeypatch.setattr(
        "gui_bridge.get_active_model",
        lambda: {"id": "m1", "name": "kimi-k2.7-code"},
    )

    res = bridge.set_reasoning_choice("m1", "off")

    assert res["ok"] is False
    assert "始终开启" in res["msg"]


def test_bridge_forwards_round_started_event(monkeypatch):
    """round_started 轮次边界事件必须透传给前端（流式分段思考依赖它）"""
    events = make_events([("round_started", {"round": 2})])

    emitted = consume_bridge_events(monkeypatch, events)

    assert any(kind == "round_started" and data.get("round") == 2 for kind, data in emitted)


class _AliveThread:
    """模拟存活的回合线程（is_alive 恒真）。"""

    def is_alive(self):
        return True


def test_stop_current_turn_cancels_permission_wait_and_foreground_subagents(monkeypatch):
    """H6：停止回合必须同步取消权限等待（owner=turn_id）与前台子代理，
    否则回合线程悬置在 broker 等待或 run_subagent 上，一直持有 _turn_workdir_lock。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "session"
    bridge._cancel_events = {"session": threading.Event()}
    bridge._active_turn_ids = {"session": "session:turn-1"}

    cancelled_owners = []
    monkeypatch.setattr(
        "gui_bridge.permission_broker.cancel_owner",
        lambda owner: cancelled_owners.append(owner),
    )
    import core.tools as core_tools

    cancelled_foreground = []
    monkeypatch.setattr(
        core_tools.subagent_manager,
        "cancel_foreground",
        lambda session, turn_id: cancelled_foreground.append((session, turn_id)),
    )

    res = bridge.stop_current_turn()

    assert res["ok"] is True
    assert bridge._cancel_events["session"].is_set()
    assert cancelled_owners == ["session:turn-1"]
    assert cancelled_foreground == [("session", "session:turn-1")]


def test_run_turn_worker_tracks_and_clears_turn_id(monkeypatch):
    """回合运行期间 _active_turn_ids 记录 session → turn_id，结束后清理；
    回合结束按 turn_id（不是会话名）清理权限 broker 的残留等待。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._cancel_events = {"session": threading.Event()}
    bridge._active_turns = {}
    bridge._active_turn_ids = {}
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_lock = threading.Lock()
    bridge._delta_last_flush = 0.0
    bridge._delta_flusher = None
    bridge._bg_summary_pending_sessions = set()
    bridge._bg_summary_lock = threading.Lock()
    bridge._current_permission_mode = "confirm"
    bridge._emit_token_usage = lambda: None
    bridge._sync_active_model_context = lambda *_args: None
    bridge._session_workdir = lambda _session_name: "GLOBAL"
    bridge._subagent_progress_poller = lambda _s, _stop: None
    bridge._maybe_auto_rename = lambda _s: None
    bridge._shutting_down = False
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._memory = type("Memory", (), {
        "ensure_session": lambda self, *a, **k: None,
        "get_session_model_id": lambda self, *a: None,
        "set_session_model_id": lambda self, *a: None,
        "add_message": lambda self, *a, **k: None,
        "set_workdir": lambda self, workdir: None,
    })()
    bridge.emit_event = lambda _kind, _data: None

    seen_turn_ids = []

    def capture_turn_id(*args, **kwargs):
        seen_turn_ids.append(bridge._active_turn_ids.get("session"))
        return False

    bridge._consume_turn_events = capture_turn_id

    cancelled_owners = []
    monkeypatch.setattr(
        "gui_bridge.permission_broker.cancel_owner",
        lambda owner: cancelled_owners.append(owner),
    )
    monkeypatch.setattr("gui_bridge.get_active_model", lambda *_args: {
        "litellm_model": "model",
        "api_key": "key",
        "base_url": None,
        "context_window": 128000,
    })
    monkeypatch.setattr("core.input_parser.parse_user_input", lambda text: (text, []))
    monkeypatch.setattr("core.agent_service.prepare_turn_messages", lambda *a, **k: [])
    monkeypatch.setattr("core.memory.begin_turn_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr("core.memory.end_turn_checkpoint", lambda: None)

    bridge._run_turn_worker("hello", "session")

    assert seen_turn_ids and seen_turn_ids[0].startswith("session:")
    assert "session" not in bridge._active_turn_ids
    # 清理必须按 turn_id，历史实现传会话名永远不命中
    assert cancelled_owners == seen_turn_ids


def test_bg_summary_pending_is_bucketed_per_session(monkeypatch):
    """M22：pending 按会话分桶，多会话并行时互不覆盖；回合结束只消费自己的桶。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._bg_summary_pending_sessions = set()
    bridge._bg_summary_lock = threading.Lock()
    bridge._active_turns = {"s1": _AliveThread(), "s2": _AliveThread()}
    bridge._turn_start_lock = threading.Lock()

    monkeypatch.setattr(
        "core.tools.subagent_manager.has_running_background_tasks",
        lambda session: False,
    )

    bridge._maybe_start_bg_summary_turn("s1")
    bridge._maybe_start_bg_summary_turn("s2")

    assert bridge._bg_summary_pending_sessions == {"s1", "s2"}


def test_stop_current_turn_marks_user_cancelled_and_skips_bg_summary(monkeypatch):
    """用户点终止后：记录取消标记、清掉挂起的自动汇总、抑制计划续推；
    _maybe_start_bg_summary_turn 对该会话不再自动发起汇总回合。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "session"
    bridge._cancel_events = {"session": threading.Event()}
    bridge._active_turn_ids = {}
    bridge._bg_summary_lock = threading.Lock()
    bridge._bg_summary_pending_sessions = {"session"}

    res = bridge.stop_current_turn()

    assert res["ok"] is True
    assert "session" in bridge._user_cancelled_sessions
    assert "session" not in bridge._bg_summary_pending_sessions

    # 计划续推抑制已生效（core.plan 模块级状态，用后清理）
    from core.plan import (
        clear_plan_auto_continue_suppression,
        plan_auto_continue_suppressed,
    )
    assert plan_auto_continue_suppressed("session")

    # 被用户取消的会话不自动发起汇总回合：提前返回，不入 triggered、不起线程
    bridge._shutting_down = False
    bridge._turn_start_lock = threading.Lock()
    bridge._active_turns = {}
    bridge._bg_summary_triggered = set()
    import core.tools as core_tools
    monkeypatch.setattr(
        core_tools.subagent_manager, "has_running_background_tasks", lambda s: False
    )

    bridge._maybe_start_bg_summary_turn("session")

    assert "session" not in bridge._bg_summary_triggered
    assert "session" not in bridge._active_turns

    clear_plan_auto_continue_suppression("session")


def test_turn_finish_emits_empty_plan_updated_when_plan_all_terminal(tmp_path, monkeypatch):
    import core.plan as plan

    manager = plan.PlanManager(storage_dir=tmp_path, session_id="sess-plan-cleared")
    manager.replace([
        {"id": "a", "subject": "任务A", "status": "completed"},
    ])
    monkeypatch.setattr(plan, "_PLAN_MANAGERS", {"sess-plan-cleared": manager})

    bridge = ApiBridge.__new__(ApiBridge)
    bridge._cancel_events = {"sess-plan-cleared": threading.Event()}
    bridge._active_turns = {}
    bridge._active_turn_ids = {}
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_lock = threading.Lock()
    bridge._delta_last_flush = 0.0
    bridge._delta_flusher = None
    bridge._bg_summary_pending_sessions = set()
    bridge._bg_summary_lock = threading.Lock()
    bridge._current_permission_mode = "confirm"
    bridge._emit_token_usage = lambda: None
    bridge._sync_active_model_context = lambda *_args: None
    bridge._session_workdir = lambda _session_name: "GLOBAL"
    bridge._subagent_progress_poller = lambda _session_name, _stop: None
    bridge._consume_turn_events = lambda *args, **kwargs: False
    bridge._maybe_auto_rename = lambda _session_name: "检查项目"
    bridge._shutting_down = False
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._memory = type("Memory", (), {
        "ensure_session": lambda self, *args, **kwargs: None,
        "get_session_model_id": lambda self, *args: None,
        "set_session_model_id": lambda self, *args: None,
        "add_message": lambda self, *args, **kwargs: None,
        "set_workdir": lambda self, workdir: None,
    })()
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))

    monkeypatch.setattr("gui_bridge.get_active_model", lambda *_args: {
        "litellm_model": "model",
        "api_key": "key",
        "base_url": None,
        "context_window": 128000,
    })
    monkeypatch.setattr("core.input_parser.parse_user_input", lambda text: (text, []))
    monkeypatch.setattr("core.agent_service.prepare_turn_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr("core.memory.begin_turn_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.memory.end_turn_checkpoint", lambda: None)

    bridge._run_turn_worker("检查项目", "sess-plan-cleared")

    # 空 plan_updated 在 turn_finish 之前发出（前端先归零面板再解锁输入框）
    kinds = [kind for kind, _data in emitted]
    assert "plan_updated" in kinds
    assert kinds.index("plan_updated") < kinds.index("turn_finish")
    plan_events = [data for kind, data in emitted if kind == "plan_updated"]
    assert plan_events == [{
        "title": "当前任务计划",
        "items": [],
        "session_name": "检查项目",
    }]
    finishes = [data for kind, data in emitted if kind == "turn_finish"]
    assert finishes == [{"session_name": "检查项目"}]
    assert manager.snapshot() == []


def test_turn_finish_survives_plan_clear_failure(tmp_path, monkeypatch):
    import core.plan as plan

    manager = plan.PlanManager(storage_dir=tmp_path, session_id="sess-plan-clear-fail")
    manager.replace([
        {"id": "a", "subject": "任务A", "status": "completed"},
    ])

    def _disk_full_clear():
        raise OSError("disk full")

    manager.clear = _disk_full_clear
    monkeypatch.setattr(plan, "_PLAN_MANAGERS", {"sess-plan-clear-fail": manager})

    bridge = ApiBridge.__new__(ApiBridge)
    bridge._cancel_events = {"sess-plan-clear-fail": threading.Event()}
    bridge._active_turns = {}
    bridge._active_turn_ids = {}
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_lock = threading.Lock()
    bridge._delta_last_flush = 0.0
    bridge._delta_flusher = None
    bridge._bg_summary_pending_sessions = set()
    bridge._bg_summary_lock = threading.Lock()
    bridge._current_permission_mode = "confirm"
    bridge._emit_token_usage = lambda: None
    bridge._sync_active_model_context = lambda *_args: None
    bridge._session_workdir = lambda _session_name: "GLOBAL"
    bridge._subagent_progress_poller = lambda _session_name, _stop: None
    bridge._consume_turn_events = lambda *args, **kwargs: False
    bridge._maybe_auto_rename = lambda _session_name: "检查项目"
    bridge._shutting_down = False
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._memory = type("Memory", (), {
        "ensure_session": lambda self, *args, **kwargs: None,
        "get_session_model_id": lambda self, *args: None,
        "set_session_model_id": lambda self, *args: None,
        "add_message": lambda self, *args, **kwargs: None,
        "set_workdir": lambda self, workdir: None,
    })()
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))

    monkeypatch.setattr("gui_bridge.get_active_model", lambda *_args: {
        "litellm_model": "model",
        "api_key": "key",
        "base_url": None,
        "context_window": 128000,
    })
    monkeypatch.setattr("core.input_parser.parse_user_input", lambda text: (text, []))
    monkeypatch.setattr("core.agent_service.prepare_turn_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr("core.memory.begin_turn_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.memory.end_turn_checkpoint", lambda: None)

    bridge._run_turn_worker("检查项目", "sess-plan-clear-fail")

    # 计划清空落盘失败（磁盘满/权限）不能吞掉 turn_finish：前端输入框靠它解锁
    kinds = [kind for kind, _data in emitted]
    assert "turn_finish" in kinds
    finishes = [data for kind, data in emitted if kind == "turn_finish"]
    assert finishes == [{"session_name": "检查项目"}]
