from core.turn_events import TurnEventFactory


def test_event_factory_assigns_monotonic_sequence_per_turn():
    factory = TurnEventFactory("turn-1")

    first = factory.emit("assistant_delta", {"text": "a"})
    second = factory.emit("tool_started", {"name": "read_file"})

    assert (first.turn_id, first.sequence) == ("turn-1", 1)
    assert second.sequence == 2


def test_event_payload_is_a_snapshot():
    payload = {"text": "before"}
    event = TurnEventFactory("turn-1").emit("assistant_delta", payload)

    payload["text"] = "after"

    assert event.payload["text"] == "before"
