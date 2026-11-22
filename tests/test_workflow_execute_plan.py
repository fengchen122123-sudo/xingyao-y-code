from core.workflows.executor import execute_plan


def test_execute_plan_is_importable():
    assert callable(execute_plan)


def test_execute_plan_returns_summary_contract():
    plan = {
        "steps": [
            {"op": "phase", "name": "准备"},
            {"op": "report", "value": "产物A"},
        ]
    }

    result = execute_plan(plan, run_id="run-test")

    assert result["ok"] is True
    assert any("产物A" in item for item in result["reports"])
    assert "工作流执行完成" in result["summary"]


def test_execute_plan_accepts_missing_publish():
    result = execute_plan({"steps": [{"op": "phase", "name": "p"}]})

    assert result["ok"] is True


def test_execute_plan_reports_events_through_publish():
    events = []

    execute_plan(
        {"steps": [{"op": "phase", "name": "准备"}]},
        publish=lambda kind, payload: events.append(kind),
    )

    assert "phase_started" in events
    assert "run_completed" in events


def test_execute_plan_failure_returns_ok_false():
    result = execute_plan({"steps": [{"op": "不存在的步骤"}]})

    assert result["ok"] is False
    assert "执行失败" in result["summary"]
