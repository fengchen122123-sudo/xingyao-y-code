"""看门狗强制收尾文案：等待审批与真卡死必须给出不同提示。

用户实测反馈：权限卡片挂在屏幕上没点，30 分钟后收到"判定为卡死"，
很容易被误会成程序故障。等待审批属于用户侧未响应，文案要直说。
"""

import gui_bridge


def test_stall_message_reports_pending_permission(monkeypatch):
    monkeypatch.setattr(gui_bridge, "_STALL_TIMEOUT_SECONDS", 1800.0)

    msg = gui_bridge._stall_turn_error_message("stall", 1806.0, "执行命令：pytest tests/test_x.py")

    assert "等你审批" in msg
    assert "pytest tests/test_x.py" in msg
    assert "30 分钟" in msg
    # 等待审批不是程序故障，不能再出现"判定为卡死"
    assert "判定为卡死" not in msg
    assert "这不是程序故障" in msg


def test_stall_message_without_permission_keeps_stall_wording(monkeypatch):
    monkeypatch.setattr(gui_bridge, "_STALL_TIMEOUT_SECONDS", 1800.0)

    msg = gui_bridge._stall_turn_error_message("stall", 1806.0, "")

    assert "判定为卡死" in msg
    assert "30 分钟" in msg
    assert "审批" not in msg


def test_stop_message_ignores_pending_permission(monkeypatch):
    monkeypatch.setattr(gui_bridge, "_STOP_WATCHDOG_SECONDS", 60.0)
    monkeypatch.setattr(gui_bridge, "_STALL_TIMEOUT_SECONDS", 1800.0)

    msg = gui_bridge._stall_turn_error_message("stop", 61.0, "执行命令：pytest")

    assert "已请求停止" in msg
    assert "60 秒" in msg
    # 用户主动停止这条路径不涉及审批提示
    assert "审批" not in msg


def test_pending_permission_summary_empty_without_turn():
    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)

    assert bridge._pending_permission_summary("") == ""


def test_pending_permission_summary_matches_turn(monkeypatch):
    from core import permission_broker as pb

    class _Request:
        function_name = "execute_command"
        arguments = {"command": "venv\\Scripts\\python -m pytest tests/test_harness_runtime.py -q"}
        owner_id = "turn-abc"
        parent_turn_id = "turn-abc"

    monkeypatch.setattr(pb.permission_broker, "active_request", lambda: _Request())
    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)

    summary = bridge._pending_permission_summary("turn-abc")
    assert "pytest" in summary
    # 多行命令压成单行，提示不能拖成一大段
    assert "\n" not in summary


def test_pending_permission_summary_ignores_other_turn(monkeypatch):
    from core import permission_broker as pb

    class _Request:
        function_name = "execute_command"
        arguments = {"command": "rm -rf something"}
        owner_id = "turn-other"
        parent_turn_id = "turn-other"

    monkeypatch.setattr(pb.permission_broker, "active_request", lambda: _Request())
    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)

    # 别的回合在等审批，不能把本回合的收尾文案说成等待审批
    assert bridge._pending_permission_summary("turn-abc") == ""