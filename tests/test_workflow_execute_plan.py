"""execute_plan 模块级入口的回归测试。

背景：core/tool_registry.py 是 `from core.workflows.executor import execute_plan`
直接按名导入的，而本模块一度只有 PlanExecutor 类、没有这个函数——带 plan 边车
的工作流一执行就在第一步 ImportError。这里既锁住「名字存在」，也真正跑一遍
最小 plan，防止以后重构又把入口删掉。
"""

from core.workflows.executor import execute_plan


def test_execute_plan_is_importable():
    """tool_registry 是直接按名导入的，名字必须真实可调用。"""
    assert callable(execute_plan)


def test_execute_plan_returns_summary_contract():
    """返回值必须是 {ok, summary, reports}：调用方按这三个键取结果。"""
    plan = {
        "steps": [
            {"op": "phase", "name": "准备"},
            {"op": "report", "value": "产物A"},
        ]
    }

    result = execute_plan(plan, run_id="run-test")

    assert result["ok"] is True
    # 产物条目带序号标签（#1: 产物A），这里只锁内容，不锁标签格式
    assert any("产物A" in item for item in result["reports"])
    assert "工作流执行完成" in result["summary"]


def test_execute_plan_accepts_missing_publish():
    """缺省 publish 不能炸：调用方未必关心事件流。"""
    result = execute_plan({"steps": [{"op": "phase", "name": "p"}]})

    assert result["ok"] is True


def test_execute_plan_reports_events_through_publish():
    """事件要真的透传给调用方传入的 publish。"""
    events = []

    execute_plan(
        {"steps": [{"op": "phase", "name": "准备"}]},
        publish=lambda kind, payload: events.append(kind),
    )

    assert "phase_started" in events
    assert "run_completed" in events


def test_execute_plan_failure_returns_ok_false():
    """失败走返回值语义，不向调用方抛异常（工具层只处理 ToolResult）。"""
    result = execute_plan({"steps": [{"op": "不存在的步骤"}]})

    assert result["ok"] is False
    assert "执行失败" in result["summary"]
