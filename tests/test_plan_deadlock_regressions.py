from core.plan import PlanManager, plan_retry
from core.task_scheduler import TaskScheduler
from core.tools import default_subagent_allowed_tools


def test_scheduler_retry_accepts_completed_item():
    plan = PlanManager()
    plan.replace([{"id": "audit", "subject": "重跑一致性审查"}])
    scheduler = TaskScheduler(plan)

    assert scheduler.start_item("audit") is True
    assert scheduler.complete("audit", "子代理声称完成") is True
    assert plan.snapshot()[0]["status"] == "completed"

    assert scheduler.retry("audit") is True
    item = plan.snapshot()[0]
    assert item["status"] == "pending"
    assert item["result"] == ""
    assert item["error"] == ""


def test_plan_retry_resets_completed_item(monkeypatch):
    plan = PlanManager()
    plan.replace([{"id": "audit", "subject": "重跑一致性审查"}])
    scheduler = TaskScheduler(plan)
    monkeypatch.setattr("core.task_scheduler.get_default_scheduler", lambda: scheduler)
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)

    assert scheduler.start_item("audit") is True
    assert scheduler.complete("audit", "done") is True

    message = plan_retry("audit")
    assert "已重置为 pending" in message
    assert plan.snapshot()[0]["status"] == "pending"


def test_in_progress_item_is_still_not_retryable():
    plan = PlanManager()
    plan.replace([{"id": "audit", "subject": "执行中"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_item("audit") is True
    assert plan.snapshot()[0]["status"] == "in_progress"
    assert scheduler.retry("audit") is False


def test_subagent_whitelist_covers_read_only_plugin_tools():
    for role in ("explore", "review"):
        allowed = default_subagent_allowed_tools(role)
        assert "novel_audit" in allowed, f"{role} 缺少 novel_audit"
        assert "novel_status" in allowed
        assert "novel_wordcount" in allowed
        assert "office_read" in allowed
        assert "write_file" not in allowed
        assert "edit_file" not in allowed
        assert "execute_command" not in allowed

    implement = default_subagent_allowed_tools("implement")
    assert {"novel_audit", "novel_track", "write_file", "edit_file", "execute_command"} <= implement

    test_role = default_subagent_allowed_tools("test")
    assert {"execute_command", "novel_audit"} <= test_role
    assert "edit_file" not in test_role

    assert default_subagent_allowed_tools("general") is None
    assert default_subagent_allowed_tools("no-such-role") is None


def test_gate_hint_points_to_plan_retry_when_nothing_ready(monkeypatch, tmp_path):
    import core.llm as llm
    from core.plan import bind_default_plan_session, get_default_plan_manager

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "accept_edits")
    monkeypatch.setattr("core.llm.check_permission_and_ask", lambda *a, **k: True)

    plan = get_default_plan_manager()
    bind_default_plan_session("plan-gate-hint-test")
    plan.replace([
        {"id": "audit", "subject": "跑审查", "status": "completed"},
        {"id": "prep", "subject": "请作者拍板", "kind": "input_preparation", "status": "cancelled"},
        {"id": "write", "subject": "写正文", "depends_on": ["prep"]},
    ])
    llm._mark_plan_turn("turn-gate-hint")
    try:
        result = llm._execute_single_tool_call(
            {
                "id": "call-1",
                "function": {
                    "name": "write_file",
                    "arguments": '{"file_path": "a.txt", "content": "x"}',
                },
            },
            event_callback=lambda *args, **kwargs: None,
            parent_session="plan-gate-hint-test",
            parent_turn_id="turn-gate-hint",
        )
        content = result["content"]
        assert "必须通过 run_subagent" in content
        assert "audit" in content
        assert "prep" in content
        assert "plan_retry" in content
        assert not (tmp_path / "a.txt").exists()
    finally:
        plan.clear()
        bind_default_plan_session(None)
