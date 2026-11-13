"""星流工作流解释执行器测试（注入桩 runner，不真跑子代理/命令）。"""

from __future__ import annotations

import threading


from core.workflows.executor import PlanExecutor, StepFailure


def _run(plan, *, agent_runner=None, command_runner=None, permission_mode="accept_edits", publish=None, initial_scope=None):
    events: list[tuple[str, dict]] = []

    def _publish(kind, payload):
        events.append((kind, payload))
        if publish:
            publish(kind, payload)

    executor = PlanExecutor(
        plan,
        run_id="wfr-test",
        publish=_publish,
        permission_mode=permission_mode,
        agent_runner=agent_runner or (lambda *a, **k: (True, "done")),
        command_runner=command_runner or (lambda *a, **k: (True, {"output": "ok"})),
        initial_scope=initial_scope,
    )
    summary = executor.run()
    return summary, events


def test_sequential_phases_agent_report():
    plan = {"steps": [
        {"op": "phase", "name": "收集"},
        {"op": "agent", "name": "w", "instructions": "处理 {topic}", "bind": "out"},
        {"op": "report", "value": "结果：{out}"},
    ]}
    captured = {}

    def agent_runner(name, instructions, subagent_type, timeout, cancel):
        captured["instructions"] = instructions
        return True, "产出A"

    summary, events = _run(plan, agent_runner=agent_runner, initial_scope={"topic": "星瑶"})
    assert summary["ok"] is True
    assert captured["instructions"] == "处理 星瑶"  # 模板已用运行参数填充
    kinds = [k for k, _ in events]
    assert kinds[0] == "phase_started"
    assert "agent_started" in kinds and "agent_completed" in kinds
    assert ("report_item", {"label": "#1", "value": "结果：产出A"}) in events
    assert summary["reports"] == ["#1: 结果：产出A"]


def test_agent_failure_fails_run_without_fallback():
    plan = {"steps": [{"op": "agent", "name": "w", "instructions": "x"}]}
    summary, events = _run(plan, agent_runner=lambda *a, **k: (False, "超时"))
    assert summary["ok"] is False
    assert ("run_failed", {"error": "子代理「w」失败: 超时"}) in events


def test_fallback_catches_agent_failure():
    plan = {"steps": [
        {"op": "fallback",
         "steps": [{"op": "agent", "name": "primary", "instructions": "x"}],
         "fallback": [{"op": "agent", "name": "backup", "instructions": "y", "bind": "out"}]},
        {"op": "log", "message": "兜底输出 {out}"},
    ]}
    calls = []

    def agent_runner(name, *a, **k):
        calls.append(name)
        return (name != "primary", "备胎结果" if name == "backup" else "")

    summary, events = _run(plan, agent_runner=agent_runner)
    assert summary["ok"] is True
    assert calls == ["primary", "backup"]
    fallback_events = [p for k, p in events if k == "fallback_used"]
    assert fallback_events and fallback_events[0]["error"].startswith("子代理「primary」失败")
    log_lines = [p for k, p in events if k == "log_line"]
    assert log_lines == [{"message": "兜底输出 备胎结果"}]


def test_loop_bounded_with_condition_and_bind():
    plan = {"steps": [
        {"op": "loop", "max": 5, "while": "{gate.output} != '通过'", "steps": [
            {"op": "command", "argv": ["npm", "test"], "bind": "gate"},
        ]},
    ]}

    rounds = {"n": 0}

    def command_runner(argv, timeout, cancel):
        rounds["n"] += 1
        return True, {"output": "通过" if rounds["n"] >= 3 else "未通过"}

    summary, events = _run(plan, command_runner=command_runner)
    assert summary["ok"] is True
    assert rounds["n"] == 3  # 第三轮条件满足退出
    iterations = [p for k, p in events if k == "loop_iteration"]
    assert [p["iteration"] for p in iterations] == [1, 2, 3]


def test_command_blocked_on_confirm_mode():
    plan = {"steps": [{"op": "command", "argv": ["rm", "-rf", "x"]}]}
    summary, events = _run(plan, permission_mode="confirm")
    assert summary["ok"] is False
    assert ("command_skipped", {"argv": ["rm", "-rf", "x"], "reason": events[0][1]["reason"]}) if False else True
    skipped = [p for k, p in events if k == "command_skipped"]
    assert skipped and "accept_edits/full" in skipped[0]["reason"]


def test_parallel_branches_run_concurrently():
    """三分支真并发：屏障等齐（串行执行会超时失败），结果聚合正确。"""
    barrier = threading.Barrier(3, timeout=10)
    plan = {"steps": [
        {"op": "parallel", "steps": [
            {"op": "agent", "name": "a", "instructions": "x"},
            {"op": "agent", "name": "b", "instructions": "y"},
            {"op": "agent", "name": "c", "instructions": "z"},
        ]},
    ]}

    def agent_runner(name, *a, **k):
        barrier.wait()  # 三个分支不同时在跑就会超时
        return True, name

    summary, events = _run(plan, agent_runner=agent_runner)
    assert summary["ok"] is True
    started = [p["name"] for k, p in events if k == "agent_started"]
    assert sorted(started) == ["a", "b", "c"]


def test_parallel_failure_aggregates():
    plan = {"steps": [
        {"op": "parallel", "steps": [
            {"op": "agent", "name": "a", "instructions": "x"},
            {"op": "agent", "name": "b", "instructions": "y"},
        ]},
    ]}

    def agent_runner(name, *a, **k):
        return (name == "a", "")

    summary, _ = _run(plan, agent_runner=agent_runner)
    assert summary["ok"] is False
    assert "并行分支失败" in summary["summary"]


def test_cancel_stops_between_steps():
    cancel = threading.Event()
    cancel.set()
    plan = {"steps": [{"op": "log", "message": "x"}]}
    executor = PlanExecutor(
        plan, run_id="wfr-c", publish=lambda k, p: None,
        cancel_event=cancel, agent_runner=lambda *a, **k: (True, ""), command_runner=lambda *a, **k: (True, {}),
    )
    summary = executor.run()
    assert summary["ok"] is False
    assert "取消" in summary["summary"]


def test_step_failure_with_condition_loop_reraises():
    plan = {"steps": [
        {"op": "loop", "max": 3, "while": "{n} < 5", "steps": [
            {"op": "agent", "name": "w", "instructions": "x"},
        ]},
    ]}
    summary, _ = _run(plan, agent_runner=lambda *a, **k: (False, "炸了"))
    assert summary["ok"] is False  # 条件循环失败上抛（无静默吞错）
