import threading

import pytest

from core.turn_events import TurnEventFactory
from gui_bridge import ApiBridge


def make_events(items):
    factory = TurnEventFactory("turn-gui-tools")
    return [factory.emit(kind, payload) for kind, payload in items]


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
    bridge._cancel_event = threading.Event()
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


def test_bridge_forwards_diff_tool_call_id(monkeypatch):
    events = make_events([
        ("diff_ready", {
            "tool_call_id": "edit-1",
            "file_path": "a.py",
            "diff_text": "--- a.py\n+++ a.py\n-old\n+new",
        }),
        ("turn_finished", {}),
    ])

    emitted = consume_bridge_events(monkeypatch, events)

    diff = next(data for kind, data in emitted if kind == "diff_ready")
    assert diff["tool_call_id"] == "edit-1"


def test_bridge_error_then_turn_error_has_single_message_owner(monkeypatch):
    events = make_events([
        ("error", {"message": "provider disconnected"}),
        ("turn_error", {"message": "provider disconnected"}),
    ])

    emitted = consume_bridge_events(monkeypatch, events)

    expected = {"message": "provider disconnected"}
    assert [data for kind, data in emitted if kind == "error"] == [expected]
    assert [data for kind, data in emitted if kind == "turn_error"] == [expected]


def test_worker_exception_emits_turn_error_and_error_toast(monkeypatch):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._bg_summary_pending_session = ""
    bridge._emit_token_usage = lambda: None
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))
    monkeypatch.setattr(
        "gui_bridge.get_active_model",
        lambda: (_ for _ in ()).throw(RuntimeError("provider disconnected")),
    )

    bridge._run_turn_worker("hello", "session")

    expected = {"message": "provider disconnected"}
    assert ("turn_error", expected) in emitted
    assert ("error", expected) in emitted


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
    bridge._cancel_event = threading.Event()
    bridge._bg_summary_pending_session = ""
    bridge._current_permission_mode = "confirm"
    bridge._emit_token_usage = lambda: None
    bridge._sync_active_model_context = lambda: None
    bridge._session_workdir = lambda session_name: "."
    bridge._set_current_session = lambda session_name: None
    bridge._subagent_progress_poller = lambda session_name, stop: None
    bridge._maybe_auto_rename = lambda session_name: None
    bridge._memory = type("Memory", (), {
        "ensure_session": lambda self, *args, **kwargs: None,
        "add_message": lambda self, *args, **kwargs: None,
    })()
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))

    def failing_events(*args, **kwargs):
        factory = TurnEventFactory("turn-gui-tools")
        yield factory.emit("turn_error", {"message": yielded_message})
        raise RuntimeError(raised_message)

    monkeypatch.setattr("gui_bridge.get_active_model", lambda: {
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
