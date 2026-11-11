"""回合看门狗判定：用户点了停止之后，不能还按 30 分钟那一档慢慢等。

回归背景：原来只有「30 分钟无进展」一档兜底。用户点停止后如果回合线程卡在
不可取消的同步调用上（系统弹窗、外部锁、原生库），要等满 30 分钟才会被强收，
期间界面一直显示运行中，用户只能反复点停止。新增停止看门狗档位：已请求停止
且 60 秒无任何进展，直接判定卡死并强制收尾。
"""

import gui_bridge


def test_force_finish_reason_stop_requested(monkeypatch):
    monkeypatch.setattr(gui_bridge, "_STOP_WATCHDOG_SECONDS", 60.0)
    monkeypatch.setattr(gui_bridge, "_STALL_TIMEOUT_SECONDS", 1800.0)

    assert gui_bridge._force_finish_reason(59.0, True) is None
    assert gui_bridge._force_finish_reason(60.0, True) == "stop"


def test_force_finish_reason_stall_without_stop(monkeypatch):
    monkeypatch.setattr(gui_bridge, "_STOP_WATCHDOG_SECONDS", 60.0)
    monkeypatch.setattr(gui_bridge, "_STALL_TIMEOUT_SECONDS", 1800.0)

    # 没点停止时，60 秒远远不够判定卡死，仍然只看 30 分钟那一档
    assert gui_bridge._force_finish_reason(60.0, False) is None
    assert gui_bridge._force_finish_reason(1799.0, False) is None
    assert gui_bridge._force_finish_reason(1800.0, False) == "stall"


def test_force_finish_reason_stop_takes_priority(monkeypatch):
    """两档同时满足时按「停止」处理：日志与用户提示文案要能区分。"""
    monkeypatch.setattr(gui_bridge, "_STOP_WATCHDOG_SECONDS", 60.0)
    monkeypatch.setattr(gui_bridge, "_STALL_TIMEOUT_SECONDS", 1800.0)

    assert gui_bridge._force_finish_reason(2000.0, True) == "stop"