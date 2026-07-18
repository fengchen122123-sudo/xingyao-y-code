from core.turn_controller import TurnController
from core.llm import chat_with_model_events
import threading


def test_cancel_turn_restores_start_length():
    controller = TurnController([{"role": "user", "content": "old"}])

    controller.begin_turn()
    controller.append({"role": "assistant", "content": "partial"})
    controller.cancel()

    assert controller.messages == [{"role": "user", "content": "old"}]
    assert controller.cancel_event.is_set()


def test_new_turn_clears_previous_cancel_event():
    controller = TurnController([])
    controller.begin_turn()
    controller.cancel()

    controller.begin_turn()

    assert controller.cancel_event.is_set() is False


def test_event_adapter_emits_cancelled_event_before_propagating_interrupt():
    cancel_event = threading.Event()
    cancel_event.set()
    events = []

    try:
        events.extend(chat_with_model_events(
            "fake-model",
            "fake-key",
            [{"role": "user", "content": "cancel"}],
            persist=False,
            turn_id="turn-cancel",
            cancel_event=cancel_event,
        ))
    except KeyboardInterrupt:
        pass

    assert [event.kind for event in events] == ["turn_cancelled"]
