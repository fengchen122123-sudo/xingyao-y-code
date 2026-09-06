"""RepeatCallGuard 对电脑操控无副作用动作的豁免（提速计划 Task 5）。"""

from __future__ import annotations

from core.harness.tool_pipeline import repeat_guard_exempt


def test_observe_scroll_wait_move_exempt():
    for kind in ("observe", "scroll", "wait", "move", "list_surfaces"):
        assert repeat_guard_exempt("computer_action", {"kind": kind, "target": "desktop"}), kind


def test_side_effect_kinds_not_exempt():
    for kind in ("click", "double_click", "type_text", "keypress", "drag", "sequence"):
        assert not repeat_guard_exempt("computer_action", {"kind": kind, "target": "desktop"}), kind


def test_other_tools_unaffected():
    assert not repeat_guard_exempt("read_file", {"path": "x"})
    assert repeat_guard_exempt("plan_read", {})
