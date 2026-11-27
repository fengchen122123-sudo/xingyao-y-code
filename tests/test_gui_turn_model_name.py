from core.memory import MemoryManager
from gui_bridge import ApiBridge


def test_emit_message_final_includes_display_model_meta():
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = MemoryManager(":memory:")
    bridge._memory.create_session_explicit("s1", workdir="GLOBAL", source="GUI")
    bridge._memory.add_message("s1", "user", "你好")
    bridge._memory.add_message(
        "s1",
        "assistant",
        {
            "role": "assistant",
            "content": "完成回复",
            "_display": {
                "model_id": "deepseek/deepseek-v4.1-flash-1017",
                "model_name": "deepseek-v4.1-flash-1017",
            },
        },
    )
    events = []
    bridge.emit_event = lambda etype, payload: events.append((etype, payload))

    bridge._emit_message_final("s1")

    assert len(events) == 1
    etype, payload = events[0]
    assert etype == "message_final"
    assert payload["model_name"] == "deepseek-v4.1-flash-1017"
    assert payload["model_id"] == "deepseek/deepseek-v4.1-flash-1017"
