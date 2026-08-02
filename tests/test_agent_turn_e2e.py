import json
import threading
from copy import deepcopy
from types import SimpleNamespace

from core.llm import chat_with_model_events
from core.turn_renderer import TurnRenderer


def _chunk(content="", tool_calls=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content="",
        tool_calls=tool_calls,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _tool_call(index, call_id, name, arguments):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def test_fake_provider_turn_covers_rendering_permissions_and_diff(tmp_path, mocker, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    responses = [
        iter([
            _chunk("A <thi"),
            _chunk("nk>private</think>B", [
                _tool_call(0, "read-1", "glob_files", {"pattern": "*.py", "dir_path": str(tmp_path)}),
                _tool_call(1, "read-2", "glob_files", {"pattern": "*.txt", "dir_path": str(tmp_path)}),
            ]),
        ]),
        iter([_chunk(tool_calls=[
            _tool_call(0, "write-1", "write_file", {
                "file_path": str(tmp_path / "denied.txt"),
                "content": "denied",
            }),
        ])]),
        iter([_chunk(tool_calls=[
            _tool_call(0, "edit-1", "edit_file", {
                "file_path": str(target),
                "target_content": "before",
                "replacement_content": "after",
            }),
        ])]),
        iter([_chunk(" 完成。")]),
    ]
    mocker.patch("core.llm.completion", side_effect=responses)

    def decide(function_name, arguments, event_callback=None, **permission_context):
        approved = function_name == "edit_file"
        event_callback("permission_requested", {"function_name": function_name})
        event_callback("permission_resolved", {"approved": approved})
        return approved

    mocker.patch("core.llm.check_permission_and_ask", side_effect=decide)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "执行"}],
        persist=False,
        turn_id="turn-e2e",
    ))
    renderer = TurnRenderer()
    for event in events:
        renderer.consume(event.kind, dict(event.payload))
    output = renderer.finish()

    assert any(
        event.kind == "thinking_delta" and "private" in event.payload["text"]
        for event in events
    )
    assert "private" not in output
    assert "A B" in renderer.assistant_text()
    assert "target.txt" in output
    assert [event.payload["id"] for event in events if event.kind == "tool_finished"] == [
        "read-1", "read-2", "write-1", "edit-1",
    ]
    denied = next(event for event in events if event.kind == "tool_finished" and event.payload["id"] == "write-1")
    assert denied.payload["ok"] is False
    assert all(event.payload["summary"] for event in events if event.kind == "tool_finished")
    assert any(event.kind == "diff_ready" for event in events)
    assert target.read_text(encoding="utf-8") == "after"
    assert not (tmp_path / "denied.txt").exists()
    assert events[-1].kind == "turn_finished"


def test_cancelled_fake_provider_turn_restores_original_messages(mocker):
    cancel_event = threading.Event()

    def cancelling_stream():
        yield _chunk("partial")
        cancel_event.set()
        yield _chunk("ignored")

    mocker.patch("core.llm.completion", return_value=cancelling_stream())
    messages = [{"role": "user", "content": "keep me"}]
    original = deepcopy(messages)
    events = []

    try:
        events.extend(chat_with_model_events(
            "fake-model",
            "fake-key",
            messages,
            persist=False,
            turn_id="turn-cancel-e2e",
            cancel_event=cancel_event,
        ))
    except KeyboardInterrupt:
        pass

    assert messages == original
    assert [event.kind for event in events][-1] == "turn_cancelled"


def test_empty_tool_followup_requests_one_final_summary(tmp_path, mocker):
    source = tmp_path / "module.py"
    source.write_text("print('source')\n", encoding="utf-8")
    responses = [
        iter([_chunk(tool_calls=[
            _tool_call(0, "read-1", "read_file", {"file_path": str(source)}),
        ])]),
        iter([_chunk(")")]),
        iter([_chunk("检查结论：模块可以正常读取。")]),
    ]
    completion = mocker.patch("core.llm.completion", side_effect=responses)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "检查"}],
        persist=False,
        turn_id="turn-final-summary",
    ))

    assistant_text = "".join(
        event.payload["text"] for event in events if event.kind == "assistant_delta"
    )
    assert completion.call_count == 3
    assert "检查结论" in assistant_text


def test_cancel_after_permission_does_not_write_file(tmp_path, mocker, monkeypatch):
    import core.llm as llm

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "must-not-exist.txt"
    cancel_event = threading.Event()
    tool_call = {
        "id": "write-cancelled",
        "function": {
            "name": "write_file",
            "arguments": '{"file_path":"%s","content":"no"}' % str(target).replace("\\", "\\\\"),
        },
    }

    def approve_then_cancel(*args, **kwargs):
        cancel_event.set()
        return True

    mocker.patch("core.llm.check_permission_and_ask", side_effect=approve_then_cancel)

    try:
        llm._execute_single_tool_call(
            tool_call,
            event_callback=lambda *args, **kwargs: None,
            cancel_event=cancel_event,
        )
    except KeyboardInterrupt:
        pass

    assert target.exists() is False
