import threading
import time

from gui_bridge import ApiBridge


def _bare_bridge() -> ApiBridge:
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._turn_activity_lock = threading.Lock()
    bridge._turn_activity = {}
    return bridge


def test_touch_then_idle_is_small():
    bridge = _bare_bridge()
    bridge._touch_turn_activity("s")
    assert bridge._turn_idle_seconds("s") < 1.0


def test_unknown_session_is_not_treated_as_stalled():
    bridge = _bare_bridge()
    assert bridge._turn_idle_seconds("never-started") == 0.0


def test_idle_grows_with_elapsed_time():
    bridge = _bare_bridge()
    bridge._turn_activity["s"] = time.monotonic() - 120.0
    assert bridge._turn_idle_seconds("s") >= 120.0


def test_bare_instance_without_fields_is_noop():
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._touch_turn_activity("s")
    assert bridge._turn_idle_seconds("s") == 0.0