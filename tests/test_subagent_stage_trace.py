# tests/test_subagent_stage_trace.py — 子代理执行流程阶段历史（右面板"子代理"工作台数据源）
# tool_finished 必须把工具结果（✓ 耗时 / ✗ 摘要）记入阶段历史，
# 否则前端只能看到"调用工具"这一行，无法还原子代理实际执行流程。
import core.llm
from core.tools import SubagentManager, _format_tool_stage_duration
from core.turn_events import TurnEvent


def _event(seq, kind, payload):
    return TurnEvent(turn_id="fake", sequence=seq, kind=kind, payload=payload)


def _fake_chat_events(events):
    def fake_chat(*args, **kwargs):
        yield from events
    return fake_chat


def _run_with_default_runner(monkeypatch, events):
    monkeypatch.setattr(core.llm, "chat_with_model_events", _fake_chat_events(events))
    manager = SubagentManager()
    manager.run("读文件任务", "s1", "t1", timeout_seconds=30)
    return manager.snapshot()[0]


def test_format_tool_stage_duration():
    assert _format_tool_stage_duration(1500) == "1.5s"
    assert _format_tool_stage_duration(350) == "350ms"
    assert _format_tool_stage_duration(0) == ""
    assert _format_tool_stage_duration(None) == ""
    assert _format_tool_stage_duration("bad") == ""


def test_tool_finished_records_ok_stage_with_duration(monkeypatch):
    snapshot = _run_with_default_runner(monkeypatch, [
        _event(1, "tool_started", {"name": "read_file"}),
        _event(2, "tool_finished", {"name": "read_file", "ok": True, "duration_ms": 1500}),
        _event(3, "assistant_delta", {"text": "结论" * 150}),
    ])

    assert snapshot.status == "completed"
    details = [stage.detail for stage in snapshot.stages]
    assert "read_file ✓ 1.5s" in details


def test_tool_finished_records_failure_stage_with_summary(monkeypatch):
    snapshot = _run_with_default_runner(monkeypatch, [
        _event(1, "tool_started", {"name": "edit_file"}),
        _event(2, "tool_finished", {"name": "edit_file", "ok": False, "summary": "权限被拒绝\n第二行"}),
        _event(3, "assistant_delta", {"text": "结论" * 150}),
    ])

    # 唯一工具调用失败：任务按产品语义判 failed，不影响阶段历史断言
    assert snapshot.status == "failed"
    details = [stage.detail for stage in snapshot.stages]
    # 换行折叠为空格，摘要进阶段详情
    assert "edit_file ✗ 权限被拒绝 第二行" in details


def test_tool_stage_history_capped_length_summary(monkeypatch):
    """超长失败摘要截断到 120 字符，不把阶段历史撑爆。"""
    long_summary = "错" * 500
    snapshot = _run_with_default_runner(monkeypatch, [
        _event(1, "tool_started", {"name": "execute_command"}),
        _event(2, "tool_finished", {"name": "execute_command", "ok": False, "summary": long_summary}),
        _event(3, "assistant_delta", {"text": "结论" * 150}),
    ])

    detail = next(s.detail for s in snapshot.stages if "✗" in s.detail)
    assert detail == f"execute_command ✗ {'错' * 120}"
