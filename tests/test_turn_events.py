import threading

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


def test_event_factory_sequences_are_unique_across_threads():
    """并行工具批次跨线程 emit 时序号不得重复或丢失。"""
    factory = TurnEventFactory("turn-parallel")
    sequences = []
    sequences_lock = threading.Lock()
    per_thread = 100

    def emit_batch():
        local = [
            factory.emit("tool_finished", {"id": i}).sequence
            for i in range(per_thread)
        ]
        with sequences_lock:
            sequences.extend(local)

    threads = [threading.Thread(target=emit_batch) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert thread.is_alive() is False

    assert sorted(sequences) == list(range(1, 8 * per_thread + 1))
