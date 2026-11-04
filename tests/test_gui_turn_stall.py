"""回合无进展看门狗的活动时钟单测。

看门狗本体嵌在 _run_turn_worker 的闭包里（需要真实回合才能触发），
这里覆盖它唯一的外部依赖：活动时钟的刷新与读取语义。
"""

import threading
import time

from gui_bridge import ApiBridge


def _bare_bridge() -> ApiBridge:
    """绕过 __init__ 构造最小实例：只装活动时钟需要的两个字段。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._turn_activity_lock = threading.Lock()
    bridge._turn_activity = {}
    return bridge


def test_touch_then_idle_is_small():
    bridge = _bare_bridge()
    bridge._touch_turn_activity("s")
    assert bridge._turn_idle_seconds("s") < 1.0


def test_unknown_session_is_not_treated_as_stalled():
    """没有活动记录（回合刚起步）不能被当成已经卡死。"""
    bridge = _bare_bridge()
    assert bridge._turn_idle_seconds("never-started") == 0.0


def test_idle_grows_with_elapsed_time():
    bridge = _bare_bridge()
    bridge._turn_activity["s"] = time.monotonic() - 120.0
    assert bridge._turn_idle_seconds("s") >= 120.0


def test_bare_instance_without_fields_is_noop():
    """测试里的裸实例没有这两个字段：必须静默跳过而不是抛异常。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._touch_turn_activity("s")
    assert bridge._turn_idle_seconds("s") == 0.0