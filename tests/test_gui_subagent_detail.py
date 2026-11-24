import threading

import pytest

import core.tools
from core.tools import SubagentManager
from gui_bridge import ApiBridge


@pytest.fixture(autouse=True)
def _turn_undo_storage(tmp_path):
    from core.memory import MemoryManager
    from core.turn_undo import service as tu_service

    tu_service.configure_default_manager(
        MemoryManager(str(tmp_path / "undo.db")), blob_root=tmp_path / "blobs"
    )


def _make_manager():
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
    assert [t["description"] for t in res["tasks"]] == ["前台任务", "后台任务"]
    assert res["tasks"][1]["background"] is True


def test_get_subagent_task_detail_finds_target_globally_filters_list(monkeypatch):
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
    assert len(res["tasks"]) == 2


def test_subagent_progress_poller_emits_stage_history(monkeypatch):
    manager = _make_manager()
    bridge = _bare_bridge(monkeypatch, manager)
    captured = []
    monkeypatch.setattr(bridge, "emit_event", lambda kind, data: captured.append((kind, data)))
    stop = threading.Event()
    stop.set()

    bridge._subagent_progress_poller("s1", stop)

    assert len(captured) == 1
    kind, data = captured[0]
    assert kind == "subagent_progress"
    assert data["session_name"] == "s1"
    assert len(data["tasks"]) == 2
    task = data["tasks"][0]
    assert task["description"] == "前台任务"
    tool_stages = [s for s in task["stages"] if s["detail"]]
    assert [s["detail"] for s in tool_stages] == [
        "read_file ✓ 1.2s",
        "edit_file ✗ 文件不存在",
    ]
    assert all(s["name"] == "tool" for s in tool_stages)
    assert all(s["at"] > 0 for s in tool_stages)
    assert all(t["description"] != "其他会话任务" for t in data["tasks"])


def test_subagent_progress_poller_carries_tool_call_id(monkeypatch):
    manager = SubagentManager(runner=lambda record: "结果正文")
    manager.run(
        "归属任务的子代理",
        "s1",
        "t1",
        timeout_seconds=30,
        tool_call_id="call-abc123",
    )
    manager.wait_for_idle(2)
    bridge = _bare_bridge(monkeypatch, manager)
    captured = []
    monkeypatch.setattr(bridge, "emit_event", lambda kind, data: captured.append((kind, data)))
    stop = threading.Event()
    stop.set()

    bridge._subagent_progress_poller("s1", stop)

    kind, data = captured[0]
    assert kind == "subagent_progress"
    assert [t["tool_call_id"] for t in data["tasks"]] == ["call-abc123"]


def test_tool_call_scope_stamps_and_restores_current_call_id():
    from core import app_context

    assert app_context.get_thread_tool_call_id() is None
    with app_context.tool_call_scope("call-outer"):
        assert app_context.get_thread_tool_call_id() == "call-outer"
        with app_context.tool_call_scope("call-inner"):
            assert app_context.get_thread_tool_call_id() == "call-inner"
        assert app_context.get_thread_tool_call_id() == "call-outer"
    assert app_context.get_thread_tool_call_id() is None


def test_get_subagent_task_detail_empty_id_returns_session_tasks(monkeypatch):
    manager = _make_manager()
    bridge = _bare_bridge(monkeypatch, manager)

    res = bridge.get_subagent_task_detail("", "s1")

    assert res["ok"] is True
    assert res["task"] is None
    assert [t["description"] for t in res["tasks"]] == ["前台任务", "后台任务"]
    assert all("timeout_seconds" in t and "tool_ok" in t for t in res["tasks"])


def test_cancel_subagent_task_stops_running_task_and_guards_session(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def runner(record):
        started.set()
        release.wait(timeout=3.0)
        return "done"

    manager = SubagentManager(runner=runner)
    bridge = _bare_bridge(monkeypatch, manager)
    manager.run(
        "长耗时后台任务",
        "s1",
        "t1",
        run_in_background=True,
        timeout_seconds=30,
    )
    assert started.wait(timeout=2.0)
    task = manager.snapshot()[0]
    manager._record_tool_started(
        task.task_id,
        "call-running",
        1,
        {"name": "execute_command", "arguments": {"command": "pytest"}},
    )

    cross = bridge.cancel_subagent_task(task.task_id, "other-session")
    assert cross["ok"] is False

    res = bridge.cancel_subagent_task(task.task_id, "s1")
    release.set()
    manager.wait_for_idle(2)

    assert res["ok"] is True
    assert res["status"] == "cancelled"
    detail = bridge.get_subagent_task_detail(task.task_id, "s1")["task"]
    assert detail["status"] == "cancelled"
    assert detail["tool_records"][0]["status"] == "cancelled"

    again = bridge.cancel_subagent_task(task.task_id, "s1")
    assert again["ok"] is True
    assert again["already_terminal"] is True


def test_subagent_records_structured_tools_rounds_files_approval_and_fast_partial(monkeypatch):
    import core.llm as llm_mod
    from core.turn_events import TurnEvent

    def fake_events(*args, **kwargs):
        yield TurnEvent("t1", 1, "thinking_started", {})
        yield TurnEvent("t1", 2, "tool_started", {
            "id": "call-1",
            "name": "execute_command",
            "preview": "curl -H Authorization: Bearer secret123",
            "arguments": {"command": "curl -H 'Authorization: Bearer secret123' https://example.com"},
            "display": {
                "category": "command",
                "action": "运行命令",
                "target": "curl",
                "command": "curl -H 'Authorization: Bearer ***' https://example.com",
            },
        })
        yield TurnEvent("t1", 3, "permission_requested", {
            "function_name": "execute_command",
            "arguments": {"command": "curl https://example.com"},
        })
        yield TurnEvent("t1", 4, "permission_resolved", {})
        yield TurnEvent("t1", 5, "tool_finished", {
            "id": "call-1",
            "name": "execute_command",
            "ok": False,
            "duration_ms": 420,
            "summary": "401 Unauthorized",
            "result": "Bearer secret123 rejected: 401 Unauthorized",
            "display": {
                "category": "command",
                "action": "运行命令",
                "target": "curl",
                "command": "curl",
                "error_summary": "401 Unauthorized",
            },
        })
        yield TurnEvent("t1", 6, "thinking_started", {})
        yield TurnEvent("t1", 7, "tool_started", {
            "id": "call-2",
            "name": "edit_file",
            "arguments": {"file_path": "dist/manifest.json"},
            "display": {"category": "mutation", "action": "修改文件", "target": "dist/manifest.json"},
        })
        yield TurnEvent("t1", 8, "diff_ready", {"file_path": "dist/manifest.json"})
        yield TurnEvent("t1", 9, "tool_finished", {
            "id": "call-2",
            "name": "edit_file",
            "ok": True,
            "duration_ms": 18,
            "result": "updated",
            "display": {"category": "mutation", "action": "修改文件", "target": "dist/manifest.json"},
        })
        yield TurnEvent("t1", 10, "assistant_delta", {"text": "已完成修复并验证。" * 25})

    monkeypatch.setattr(llm_mod, "chat_with_model_events", fake_events)
    monkeypatch.setattr(
        "core.task_decision_hooks.decide_subagent_review",
        lambda **kwargs: None,
    )

    manager = SubagentManager()
    bridge = _bare_bridge(monkeypatch, manager)
    manager.run("多轮修复任务", "s1", "t1", timeout_seconds=120, model_config=("openai/gpt-4.1-mini", "k", "u"))
    snap = manager.snapshot()[0]
    detail = bridge.get_subagent_task_detail(snap.task_id, "s1")["task"]

    assert detail["status"] == "completed"
    assert detail["model_name"] == "gpt-4.1-mini"
    assert detail["rounds"] == 3
    assert detail["tool_ok"] == 1
    assert detail["tool_err"] == 1
    assert detail["changed_files"] == ["dist/manifest.json"]
    assert detail["approval_summary"] == ""
    assert len(detail["partial"]) >= 200
    assert len(detail["tool_records"]) == 2
    first, second = detail["tool_records"]
    assert first["round"] == 1 and first["status"] == "err"
    assert "secret123" not in first["args_preview"]
    assert "secret123" not in first["result_preview"]
    assert first["error_summary"] == "401 Unauthorized"
    assert second["round"] == 2 and second["status"] == "ok" and second["duration_text"] == "18ms"


def test_subagent_records_round_thinking_intro_text_and_file_diffs(monkeypatch):
    import core.llm as llm_mod
    from core.turn_events import TurnEvent

    def fake_events(*args, **kwargs):
        yield TurnEvent("t1", 1, "thinking_started", {})
        yield TurnEvent("t1", 2, "thinking_delta", {"text": "先查旧提交差异，"})
        yield TurnEvent("t1", 3, "thinking_delta", {"text": "再修改对应文件。"})
        yield TurnEvent("t1", 4, "assistant_delta", {"text": "开始检查 AgentCore 差异。"})
        yield TurnEvent("t1", 5, "tool_started", {
            "id": "call-edit",
            "name": "edit_file",
            "arguments": {"file_path": "src/AgentCore.java"},
            "display": {"category": "mutation", "action": "修改文件", "target": "src/AgentCore.java"},
        })
        yield TurnEvent("t1", 6, "diff_ready", {
            "tool_call_id": "call-edit",
            "file_path": "src/AgentCore.java",
            "diff_text": "--- a/src/AgentCore.java\n+++ b/src/AgentCore.java\n-old\n+new\n+more",
            "added": 2,
            "removed": 1,
        })
        yield TurnEvent("t1", 7, "tool_finished", {
            "id": "call-edit",
            "name": "edit_file",
            "ok": True,
            "duration_ms": 24,
            "result": "updated",
            "display": {"category": "mutation", "action": "修改文件", "target": "src/AgentCore.java"},
        })
        yield TurnEvent("t1", 8, "thinking_delta", {"text": "已确认修改完成，可以输出结论。"})
        yield TurnEvent("t1", 9, "assistant_delta", {"text": "已完成 AgentCore 恢复并验证所有用例通过。" * 8})

    monkeypatch.setattr(llm_mod, "chat_with_model_events", fake_events)
    monkeypatch.setattr(
        "core.task_decision_hooks.decide_subagent_review",
        lambda **kwargs: None,
    )

    manager = SubagentManager()
    bridge = _bare_bridge(monkeypatch, manager)
    manager.run("记录思考与Diff", "s1", "t1", timeout_seconds=120)
    snap = manager.snapshot()[0]
    detail = bridge.get_subagent_task_detail(snap.task_id, "s1")["task"]

    assert len(detail["round_notes"]) == 2
    assert detail["round_notes"][0]["round"] == 1
    assert "先查旧提交差异" in detail["round_notes"][0]["thinking"]
    assert detail["round_notes"][0]["text"] == "开始检查 AgentCore 差异。"
    assert detail["round_notes"][1]["round"] == 2
    assert "输出结论" in detail["round_notes"][1]["thinking"]
    # 最终轮结论只进 result，不重复塞入 round_notes["text"]
    assert detail["round_notes"][1]["text"] == ""
    assert detail["file_diffs"] == [{
        "file_path": "src/AgentCore.java",
        "diff_text": "--- a/src/AgentCore.java\n+++ b/src/AgentCore.java\n-old\n+new\n+more",
        "added": 2,
        "removed": 1,
    }]
    assert detail["tool_records"][0]["diff"]["added"] == 2
    assert detail["tool_records"][0]["diff"]["removed"] == 1

