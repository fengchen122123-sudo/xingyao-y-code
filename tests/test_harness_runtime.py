import json
import threading
from types import SimpleNamespace

import pytest

from core.llm import chat_with_model_events
from core.memory import MemoryManager


def _chunk(content="", tool_calls=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content="",
        tool_calls=tool_calls,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(
        index=0,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _tool_stream(call_id, name, arguments):
    return iter([_chunk(tool_calls=[_tool_call(call_id, name, arguments)])])


def _text_stream(text):
    return iter([_chunk(character) for character in text])


def _cancelling_stream(cancel_event):
    def generate():
        yield _chunk("partial")
        cancel_event.set()
        yield _chunk("ignored")

    return generate()


def _persistent_runtime_fixture(mocker):
    manager = MemoryManager(":memory:")
    manager.create_session_explicit("session")
    cancel_event = threading.Event()
    mocker.patch("core.app_context.get_manager", return_value=manager)
    return manager, cancel_event


def _persistent_events(*, cancel_event=None):
    return chat_with_model_events(
        "fake-model",
        "key",
        [{"role": "user", "content": "q"}],
        persist=True,
        parent_session="session",
        turn_id="turn-runtime",
        cancel_event=cancel_event,
    )


def _steps(manager, turn_id):
    rows = manager.conn.execute(
        "SELECT ordinal, status FROM harness_steps "
        "WHERE turn_id = ? ORDER BY ordinal",
        (turn_id,),
    ).fetchall()
    return [{"ordinal": ordinal, "status": status} for ordinal, status in rows]


def test_persistent_turn_records_two_model_steps_for_one_tool_round(tmp_path, mocker):
    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit("session")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    mocker.patch(
        "core.llm.completion",
        side_effect=[
            _tool_stream(
                "call-1",
                "read_file",
                {"file_path": str(tmp_path / "a.py")},
            ),
            _text_stream("done"),
        ],
    )
    (tmp_path / "a.py").write_text("body", encoding="utf-8")

    list(
        chat_with_model_events(
            "fake-model",
            "key",
            [{"role": "user", "content": "read"}],
            persist=True,
            parent_session="session",
            turn_id="turn-runtime",
        )
    )

    turn = manager.get_harness_turn("turn-runtime")
    steps = _steps(manager, "turn-runtime")
    assert turn["status"] == "completed"
    assert [(item["ordinal"], item["status"]) for item in steps] == [
        (1, "completed"),
        (2, "completed"),
    ]
    committed = manager.conn.execute(
        "SELECT step_id, kind FROM harness_events "
        "WHERE turn_id = ? AND message_id IS NOT NULL ORDER BY sequence",
        ("turn-runtime",),
    ).fetchall()
    assert committed == [
        ("turn-runtime:step:1", "assistant_committed"),
        ("turn-runtime:step:1", "tool_result_committed"),
        ("turn-runtime:step:2", "assistant_committed"),
    ]


def test_nonpersistent_turn_uses_no_database_journal(mocker):
    manager = MemoryManager(":memory:")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    mocker.patch("core.llm.completion", return_value=_text_stream("answer"))

    events = list(
        chat_with_model_events(
            "fake-model",
            "key",
            [{"role": "user", "content": "q"}],
            persist=False,
            turn_id="turn-ephemeral",
        )
    )

    assert [event.kind for event in events] == ["assistant_delta"] * 6 + [
        "turn_finished"
    ]
    assert manager.get_harness_turn("turn-ephemeral") is None


def test_user_cancel_sets_cancelled_not_interrupted(mocker):
    manager, cancel_event = _persistent_runtime_fixture(mocker)
    mocker.patch(
        "core.llm.completion",
        return_value=_cancelling_stream(cancel_event),
    )

    with pytest.raises(KeyboardInterrupt):
        list(_persistent_events(cancel_event=cancel_event))

    assert manager.get_harness_turn("turn-runtime")["status"] == "cancelled"
    assert _steps(manager, "turn-runtime")[-1]["status"] == "cancelled"


def test_provider_error_sets_failed_and_keeps_turn_error_event(mocker):
    manager, _cancel_event = _persistent_runtime_fixture(mocker)
    mocker.patch("core.llm.completion", side_effect=RuntimeError("provider failed"))
    mocker.patch("core.llm.time.sleep", return_value=None)

    events = list(_persistent_events())

    assert [event.kind for event in events].count("turn_error") == 1
    assert manager.get_harness_turn("turn-runtime")["status"] == "failed"


def test_message_persistence_failure_propagates_and_fails_the_turn(mocker):
    manager, _cancel_event = _persistent_runtime_fixture(mocker)
    mocker.patch("core.llm.completion", return_value=_text_stream("answer"))
    mocker.patch.object(
        manager,
        "add_messages_with_harness_events",
        side_effect=RuntimeError("message write failed"),
    )

    with pytest.raises(RuntimeError, match="message write failed"):
        list(_persistent_events())

    assert manager.get_harness_turn("turn-runtime")["status"] == "failed"
