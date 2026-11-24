from core.plan import PlanManager, plan_retry
from core.task_scheduler import TaskScheduler
from core.tools import (
    PLAN_STAGE_SUBAGENT_TIMEOUT,
    default_subagent_allowed_tools,
    normalize_builtin_subagent_type,
    resolve_plan_subagent_type,
)


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

    operate = default_subagent_allowed_tools("operate")
    assert "execute_command" in operate
    assert "read_file" in operate
    assert "fetch_url" in operate
    assert "write_file" not in operate
    assert "edit_file" not in operate
    # 只读集合里的插件工具必须随 operate 一起可见，否则「查工时/跑体检」
    # 这类取证计划项会退回历史死锁（子代理没工具、主 Agent 被门禁拦住）。
    assert {"office_read", "novel_audit", "novel_status", "novel_wordcount"} <= operate
    assert "multi_edit" not in operate


def test_operate_tools_equal_read_only_set_plus_command():
    from core.tools import _SUBAGENT_READ_ONLY_TOOLS

    assert default_subagent_allowed_tools("operate") == (
        _SUBAGENT_READ_ONLY_TOOLS | frozenset({"execute_command"})
    )
    for role in ("explore", "review"):
        assert default_subagent_allowed_tools(role) == _SUBAGENT_READ_ONLY_TOOLS


def test_unspecified_plan_stage_uses_operate():
    assert resolve_plan_subagent_type("", "plan") == "operate"
    assert resolve_plan_subagent_type("general", "plan") == "operate"
    assert resolve_plan_subagent_type("explore", "plan") == "explore"
    assert resolve_plan_subagent_type("operate", "plan") == "operate"
    assert resolve_plan_subagent_type("general", "build") == "implement"
    assert resolve_plan_subagent_type("general", "review") == "review"
    assert resolve_plan_subagent_type("neo-implement", "plan") == "neo-implement"
    assert resolve_plan_subagent_type("general", "unknown") == "general"


def test_explicit_role_is_never_rewritten_by_stage():
    # 显式角色在任何 stage 都必须原样保留：只读阶段想派只读角色是合法选择。
    for role in ("explore", "review", "operate", "implement", "test"):
        for stage in ("plan", "build", "review"):
            assert resolve_plan_subagent_type(role, stage) == role
    assert resolve_plan_subagent_type(" neo-implement ", "build") == "neo-implement"


def test_role_name_case_is_normalized_before_stage_mapping():
    # "General" 若不归一，会被当成自定义名字、查不到后静默落 general，
    # 既拿不到 operate 也让计划阶段退回「无命令」。
    assert normalize_builtin_subagent_type("Operate") == "operate"
    assert normalize_builtin_subagent_type(" Explore ") == "explore"
    assert normalize_builtin_subagent_type("Neo-Implement") == "Neo-Implement"
    assert resolve_plan_subagent_type("General", "plan") == "operate"
    assert resolve_plan_subagent_type("OPERATE", "plan") == "operate"


def test_force_general_is_the_explicit_escape_hatch():
    # 计划项合法 stage 只有 plan/build/review，没有这个出口时 general（唯一
    # 含 MCP 工具的角色）在计划内彻底不可达。
    assert resolve_plan_subagent_type("", "plan", force_general=True) == "general"
    assert resolve_plan_subagent_type("general", "build", force_general=True) == "general"
    assert resolve_plan_subagent_type("explore", "plan", force_general=True) == "general"


def _run_delegated(
    monkeypatch,
    *,
    stage,
    subagent_type="general",
    force_general=False,
    permission_mode="full",
    timeout_seconds=None,
):
    """跑一次真实的 run_subagent 计划项派发，捕获传给子代理管理器的参数。"""
    from core import tools as tools_module

    plan = PlanManager()
    plan.replace([{"id": "P1", "subject": "查工时", "stage": stage}])
    scheduler = TaskScheduler(plan)
    monkeypatch.setattr("core.task_scheduler.get_default_scheduler", lambda: scheduler)
    monkeypatch.setattr(
        "core.subagent_settings.orchestration_allowed", lambda *args, **kwargs: True
    )
    monkeypatch.setattr("core.app_context.get_permission_mode", lambda: permission_mode)

    captured = {}

    class _FakeManager:
        def run(self, task_description, parent_session, parent_turn_id, **kwargs):
            captured.update(kwargs)
            return "{}"

    monkeypatch.setattr(tools_module, "subagent_manager", _FakeManager())
    tools_module.run_subagent(
        "查工时数据",
        subagent_type=subagent_type,
        plan_item_id="P1",
        parent_session="s-plan-wire",
        parent_turn_id="t-plan-wire",
        force_general=force_general,
        timeout_seconds=timeout_seconds,
    )
    return captured


def test_run_subagent_wires_unspecified_plan_role_to_operate(monkeypatch):
    captured = _run_delegated(monkeypatch, stage="plan")
    assert captured["subagent_type"] == "operate"
    assert captured["timeout_seconds"] == PLAN_STAGE_SUBAGENT_TIMEOUT


def test_run_subagent_keeps_explicit_role_in_plan_stage(monkeypatch):
    captured = _run_delegated(monkeypatch, stage="plan", subagent_type="explore")
    assert captured["subagent_type"] == "explore"


def test_run_subagent_force_general_reaches_general(monkeypatch):
    captured = _run_delegated(monkeypatch, stage="plan", force_general=True)
    assert captured["subagent_type"] == "general"


def test_run_subagent_build_stage_maps_to_implement(monkeypatch):
    captured = _run_delegated(monkeypatch, stage="build")
    assert captured["subagent_type"] == "implement"
    assert captured["timeout_seconds"] == 900


def test_plan_stage_permission_is_capped_to_confirm(monkeypatch):
    # accept_edits/full 下 execute_command 的普通写命令只判 NORMAL_MUTATION，
    # 不降档就会让 plan 阶段子代理绕过「build 才能改文件」的计划门禁。
    for wide_mode in ("accept_edits", "full"):
        captured = _run_delegated(monkeypatch, stage="plan", permission_mode=wide_mode)
        assert captured["permission_mode"] == "confirm"


def test_plan_stage_permission_is_never_relaxed(monkeypatch):
    for strict_mode in ("plan", "confirm"):
        captured = _run_delegated(monkeypatch, stage="plan", permission_mode=strict_mode)
        assert captured["permission_mode"] is None
    captured = _run_delegated(monkeypatch, stage="build", permission_mode="full")
    assert captured["permission_mode"] is None


def test_plan_stage_explicit_timeout_is_not_capped(monkeypatch):
    captured = _run_delegated(monkeypatch, stage="plan", timeout_seconds=900)
    assert captured["timeout_seconds"] == 900


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
