# tests/test_gui_subagent_detail.py — 右面板"子代理"工作台数据链路
# get_subagent_task_detail：目标任务完整阶段历史 + 会话任务清单（全局查目标、按会话过滤清单）
# _subagent_progress_poller：subagent_progress 事件携带完整阶段历史
import threading

import pytest

import core.tools
from core.tools import SubagentManager
from gui_bridge import ApiBridge


@pytest.fixture(autouse=True)
def _turn_undo_storage(tmp_path):
    """后台子代理线程启动时依赖 turn_undo 服务，用临时库配置（参照 test_gui_tool_events）。"""
    from core.memory import MemoryManager
    from core.turn_undo import service as tu_service

    tu_service.configure_default_manager(
        MemoryManager(str(tmp_path / "undo.db")), blob_root=tmp_path / "blobs"
    )


def _make_manager():
    """三个任务的 manager：s1 前台/s1 后台/s2 前台；运行中注入两条工具阶段。"""
    holder = {}

    def runner(record):
        mgr = holder["mgr"]
        mgr._record_stage(record.task_id, "tool", "read_file ✓ 1.2s")
        mgr._record_stage(record.task_id, "tool", "edit_file ✗ 文件不存在")
        return "结果正文"

    manager = SubagentManager(runner=runner)
    holder["mgr"] = manager
    manager.run("前台任务", "s1", "t1", timeout_seconds=30)
    manager.run("后台任务", "s1", "t2", run_in_background=True, timeout_seconds=30)
    manager.run("其他会话任务", "s2", "t3", timeout_seconds=30)
    manager.wait_for_idle(2)
    return manager


def _bare_bridge(monkeypatch, manager):
    bridge = ApiBridge.__new__(ApiBridge)
    monkeypatch.setattr(core.tools, "subagent_manager", manager)
    return bridge


def _tool_details(stages):
    return [s["detail"] for s in stages if s["detail"]]


def test_get_subagent_task_detail_returns_stages_and_session_tasks(monkeypatch):
    manager = _make_manager()
    bridge = _bare_bridge(monkeypatch, manager)
    task = next(s for s in manager.snapshot() if s.description == "前台任务")

    res = bridge.get_subagent_task_detail(task.task_id, "s1")

    assert res["ok"] is True
    detail = res["task"]
    assert detail["id"] == task.task_id
    assert detail["description"] == "前台任务"
    assert detail["status"] == "completed"
    assert _tool_details(detail["stages"]) == [
        "read_file ✓ 1.2s",
        "edit_file ✗ 文件不存在",
    ]
    # 清单只含 s1 的任务（前台+后台），不串 s2
    assert [t["description"] for t in res["tasks"]] == ["前台任务", "后台任务"]
    assert res["tasks"][1]["background"] is True


def test_get_subagent_task_detail_finds_target_globally_filters_list(monkeypatch):
    """目标按 task_id 全局查找：会话切换后仍可回看旧会话任务。"""
    manager = _make_manager()
    bridge = _bare_bridge(monkeypatch, manager)
    s1_task = next(s for s in manager.snapshot() if s.parent_session == "s1")

    res = bridge.get_subagent_task_detail(s1_task.task_id, "s2")

    assert res["ok"] is True
    assert res["task"]["id"] == s1_task.task_id
    assert [t["description"] for t in res["tasks"]] == ["其他会话任务"]


def test_get_subagent_task_detail_unknown_id(monkeypatch):
    manager = _make_manager()
    bridge = _bare_bridge(monkeypatch, manager)

    res = bridge.get_subagent_task_detail("subagent-not-exist", "s1")

    assert res["ok"] is False
    assert res["task"] is None
    assert "未找到" in res["msg"]
    # 清单照常返回，供前端切换到仍存在的任务
    assert len(res["tasks"]) == 2


def test_subagent_progress_poller_emits_stage_history(monkeypatch):
    manager = _make_manager()
    bridge = _bare_bridge(monkeypatch, manager)
    captured = []
    monkeypatch.setattr(bridge, "emit_event", lambda kind, data: captured.append((kind, data)))
    stop = threading.Event()
    stop.set()  # 预置停止：循环体先采样推送一次再退出

    bridge._subagent_progress_poller("s1", stop)

    assert len(captured) == 1
    kind, data = captured[0]
    assert kind == "subagent_progress"
    assert data["session_name"] == "s1"
    assert len(data["tasks"]) == 2
    task = data["tasks"][0]
    assert task["description"] == "前台任务"
    # 阶段历史只断言注入的工具阶段（queued/running 等阶段 detail 为空）
    assert [s for s in task["stages"] if s["detail"]] == [
        {"name": "tool", "at": task["stages"][0]["at"], "detail": "read_file ✓ 1.2s"},
        {"name": "tool", "at": task["stages"][1]["at"], "detail": "edit_file ✗ 文件不存在"},
    ]
    # s2 的任务不进本次推送
    assert all(t["description"] != "其他会话任务" for t in data["tasks"])
