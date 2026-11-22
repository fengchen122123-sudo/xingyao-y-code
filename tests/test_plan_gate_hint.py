import core.llm as llm
from core.plan import bind_default_plan_session, get_default_plan_manager


def _call(name, arguments, session, turn):
    return llm._execute_single_tool_call(
        {"id": f"call-{turn}", "function": {"name": name, "arguments": arguments}},
        event_callback=lambda *args, **kwargs: None,
        parent_session=session,
        parent_turn_id=turn,
    )


def test_plan_bound_hint_lists_bindable_items():
    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("plan-bound-hint-test")
    plan.replace(
        [
            {"id": "weather", "subject": "查询天气"},
            {"id": "ask-city", "subject": "确认城市", "kind": "input_preparation"},
        ]
    )
    try:
        subagent = _call(
            "run_subagent", '{"task_description":"查询天气"}', "plan-bound-hint-test", "t1"
        )
        content = subagent["content"]
        assert "必须填写 plan_item_id" in content
        assert "'weather'(查询天气)" in content
        assert "ask-city" not in content

        asked = _call(
            "ask_user",
            '{"question":"城市？","options":["北京"]}',
            "plan-bound-hint-test",
            "t2",
        )
        assert "必须填写 plan_item_id" in asked["content"]
        assert "ask-city" in asked["content"]
    finally:
        bind_default_plan_session(previous_session)


def test_plan_bound_hint_points_to_plan_read_when_nothing_bindable():
    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("plan-bound-hint-empty-test")
    plan.replace([{"id": "weather", "subject": "查询天气"}])
    try:
        blocked = _call(
            "ask_user",
            '{"question":"城市？","options":["北京"]}',
            "plan-bound-hint-empty-test",
            "t3",
        )
        assert "plan_read" in blocked["content"]
    finally:
        bind_default_plan_session(previous_session)
