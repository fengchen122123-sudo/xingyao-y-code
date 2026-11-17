# -*- coding: utf-8 -*-
"""任务执行期 JEV 自动决策钩子测试（不干预模型路由）。

覆盖：
1. 默认关闭（enabled=False）：零请求、零审计、行为完全不变；
2. 开启全局决策（enabled=True）：不打开 router 选模型，但 4 个执行期决策点自动调用 JEV；
   - shadow_mode=True 时写影子审计（shadow=True, adopted=False）但不改业务；
   - shadow_mode=False 时正式采纳（shadow=False, adopted=True）；
   - 显式在 scenes 里设 enabled=False 可单项关闭；
3. 4 个执行期接入点真实联动：
   - loop_guard：连续报错/停滞时触发纠偏（pivot / ask_user / continue）；
   - subagent_review：SubagentManager._run_task 验收子代理结果（accepted / incomplete / failed）；
   - plan_recovery：plan_retry 对失败项给出恢复策略（retry_same / split_smaller / escalate_user）；
   - memory_filter：后台学习落盘前过滤噪音记忆（keep_all / keep_facts_only / skip_all）；
4. 隐私脱敏：sk- / Bearer / api_key 等密钥不进入发给 JEV 的 state。
"""

import json
import os
import sys
import threading

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import decision_gateway as gateway
from core import decision_service as svc
from core import task_decision_hooks as hooks
from core.turn_model_override import router_scene_live

PROVIDER = {
    "id": "tr-1",
    "name": "基元律动",
    "base_url": "https://tokenrhythm.studio/v1",
    "api_key": "sk_key_one",
}


def _write_user_config(data_dir, providers):
    (data_dir / "config.json").write_text(
        json.dumps({"providers": providers}, ensure_ascii=False), encoding="utf-8"
    )


def _resp(code, body=None):
    return httpx.Response(code, json=body if body is not None else {"answers": {}})


def _mock_post(monkeypatch, handler):
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append(
            {"url": url, "headers": headers, "payload": payload, "timeout": timeout_seconds}
        )
        return handler(len(calls) - 1, calls[-1])

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _mock_no_network(monkeypatch):
    calls = []

    def fake(url, headers, payload, timeout_seconds):  # pragma: no cover - 不应触发
        calls.append(payload)
        raise AssertionError("未启用决策时不允许发起网络请求")

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _audit_records(data_dir):
    records = []
    directory = data_dir / "decision_audit"
    if not directory.is_dir():
        return records
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    import config

    directory = tmp_path / "ycode"
    directory.mkdir()
    monkeypatch.setattr(config, "YCODE_DIR", str(directory), raising=False)
    svc._reset_runtime_state()
    yield directory
    svc._reset_runtime_state()


# ---------------------------------------------------------------------------
# 1. 自动开启与路由隔离
# ---------------------------------------------------------------------------

def test_disabled_by_default_zero_requests(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    calls = _mock_no_network(monkeypatch)

    assert hooks.decide_loop_intervention(
        turn_index=2, max_turns=20, consecutive_error_steps=2,
        stalled_steps=1, repeat_blocked=False, error_samples=["err"],
    ) is None
    assert hooks.decide_subagent_review(
        description="修复 Bug", subagent_type="implement", result_text="已完成修改",
    ) is None
    assert hooks.decide_plan_recovery(
        item_id="t1", subject="运行测试", attempts=1, last_error="exit 1",
    ) is None
    assert hooks.filter_extracted_learning({
        "user_facts": [{"kind": "preference", "text": "用中文"}],
        "lessons": [],
        "self_notes": [],
    })["user_facts"][0]["text"] == "用中文"
    assert calls == []
    assert _audit_records(data_dir) == []


def test_global_enabled_auto_activates_task_scenes_without_enabling_router(data_dir, monkeypatch):
    """仅开全局开关 enabled=True（不改 scenes）：router 仍关闭，4 个任务决策点自动调用 JEV。"""
    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True, "shadow_mode": False})
    assert router_scene_live() is False

    answers = iter(["pivot", "accepted", "split_smaller", "keep_facts_only"])
    calls = _mock_post(
        monkeypatch,
        lambda i, call: _resp(200, {"answers": {"q": {"choice": next(answers)}}}),
    )

    assert hooks.decide_loop_intervention(
        turn_index=3, max_turns=20, consecutive_error_steps=2,
        stalled_steps=2, repeat_blocked=True, error_samples=["bash: fail"],
    ) == "pivot"
    assert hooks.decide_subagent_review(
        description="核查日志", subagent_type="explore", result_text="已完成核查，无异常",
    ) == "accepted"
    assert hooks.decide_plan_recovery(
        item_id="t1", subject="全量重构", attempts=1, last_error="timeout",
    ) == "split_smaller"
    filtered = hooks.filter_extracted_learning({
        "user_facts": [{"kind": "preference", "text": "用中文"}],
        "lessons": [{"slug": "x", "scene": "s", "how": "h"}],
        "self_notes": ["note"],
    })
    assert len(filtered["user_facts"]) == 1
    assert filtered["lessons"] == []
    assert filtered["self_notes"] == []

    assert len(calls) == 4
    # 任务决策点默认至少 1500ms 超时，避免 250ms 提前超时回退
    assert all(call["timeout"] >= 1.5 for call in calls)
    records = _audit_records(data_dir)
    assert [r["scene"] for r in records] == [
        "loop_guard", "subagent_review", "plan_recovery", "memory_filter"
    ]
    assert all(r["shadow"] is False and r["adopted"] is True for r in records)


def test_global_shadow_mode_records_audit_without_affecting_behavior(data_dir, monkeypatch):
    """全局 shadow_mode=True 时自动发请求并写影子审计，但返回值全为 None / 保留原样。"""
    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True, "shadow_mode": True})
    calls = _mock_post(
        monkeypatch,
        lambda i, call: _resp(200, {"answers": {"q": {"choice": "skip_all"}}}),
    )

    raw = {
        "user_facts": [{"kind": "preference", "text": "保留"}],
        "lessons": [],
        "self_notes": [],
    }
    assert hooks.filter_extracted_learning(raw) == raw
    assert len(calls) == 1
    records = _audit_records(data_dir)
    assert len(records) == 1
    assert records[0]["scene"] == "memory_filter"
    assert records[0]["shadow"] is True
    assert records[0]["adopted"] is False


def test_explicit_scene_opt_out_stops_requests(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({
        "enabled": True,
        "shadow_mode": False,
        "scenes": {"loop_guard": {"enabled": False}},
    })
    calls = _mock_no_network(monkeypatch)

    assert hooks.decide_loop_intervention(
        turn_index=2, max_turns=20, consecutive_error_steps=3,
        stalled_steps=3, repeat_blocked=True, error_samples=["err"],
    ) is None
    assert calls == []


def test_state_redacts_sensitive_tokens(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True, "shadow_mode": False})
    calls = _mock_post(
        monkeypatch,
        lambda i, call: _resp(200, {"answers": {"q": {"choice": "pivot"}}}),
    )

    hooks.decide_loop_intervention(
        turn_index=2,
        max_turns=10,
        consecutive_error_steps=2,
        stalled_steps=1,
        repeat_blocked=False,
        error_samples=["Authorization: Bearer secret_abc_123456 failed with sk-secret999999"],
    )
    sent_state = calls[0]["payload"]["state"]
    assert "secret_abc_123456" not in sent_state
    assert "sk-secret999999" not in sent_state


# ---------------------------------------------------------------------------
# 2. loop_guard：构建纠偏消息 + chat_with_model_events 异常步触发联动
# ---------------------------------------------------------------------------

def test_loop_intervention_message_variants():
    assert "纠偏" in hooks.build_loop_intervention_message("pivot")
    assert "向用户" in hooks.build_loop_intervention_message("ask_user")
    assert hooks.build_loop_intervention_message("continue") is None
    assert hooks.build_loop_intervention_message(None) is None


def test_loop_guard_injects_pivot_guidance_in_chat_turn(data_dir, monkeypatch):
    """chat_with_model_events 连续 2 步工具报错时，自动触发 loop_guard 并注入纠偏消息。"""
    from types import SimpleNamespace
    from core import llm as llm_mod

    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True, "shadow_mode": False})
    _mock_post(
        monkeypatch,
        lambda i, call: _resp(200, {"answers": {"q": {"choice": "pivot"}}}),
    )

    def _chunk(content="", tool_calls=None):
        delta = SimpleNamespace(content=content, reasoning_content="", tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)

    def _tc(call_id, name, args):
        return SimpleNamespace(
            index=0,
            id=call_id,
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )

    captured_requests = []
    responses = [
        iter([_chunk(tool_calls=[_tc("c1", "read_file", {"file_path": "missing_1.py"})])]),
        iter([_chunk(tool_calls=[_tc("c2", "read_file", {"file_path": "missing_2.py"})])]),
        iter([_chunk("已根据纠偏建议改为只读排查并停止重试。")]),
    ]

    def fake_completion(**kwargs):
        captured_requests.append(list(kwargs.get("messages") or []))
        return responses.pop(0)

    monkeypatch.setattr(llm_mod, "completion", fake_completion)
    monkeypatch.setattr(llm_mod, "check_permission_and_ask", lambda *a, **kw: True)

    messages = [{"role": "user", "content": "帮我排查"}]
    events = list(llm_mod.chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-loop-guard",
    ))

    assert any(e.kind == "turn_finished" for e in events)
    # 第 3 次模型请求应能看到 loop_guard 注入的纠偏提示
    assert len(captured_requests) == 3
    assert any(
        msg.get("role") == "user" and "JEV 执行纠偏建议" in str(msg.get("content", ""))
        for msg in captured_requests[2]
    )


# ---------------------------------------------------------------------------
# 3. subagent_review：SubagentManager._run_task 联动验收
# ---------------------------------------------------------------------------

def test_subagent_review_marks_fake_completion_as_failed(data_dir, monkeypatch):
    from core.tools import SubagentManager, SubagentTask

    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True, "shadow_mode": False})
    _mock_post(
        monkeypatch,
        lambda i, call: _resp(200, {"answers": {"q": {"choice": "failed"}}}),
    )

    mgr = SubagentManager(runner=lambda task: "工具不可用，未能完成文件修改。")
    task = SubagentTask(
        task_id="sub-1",
        parent_session="s1",
        parent_turn_id="t1",
        description="修改 core/a.py",
        subagent_type="implement",
    )
    mgr._tasks[task.task_id] = task
    mgr._run_task(task)

    assert task.status == "failed"
    assert "验收未通过" in task.error


def test_subagent_review_appends_warning_on_incomplete(data_dir, monkeypatch):
    from core.tools import SubagentManager, SubagentTask

    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True, "shadow_mode": False})
    _mock_post(
        monkeypatch,
        lambda i, call: _resp(200, {"answers": {"q": {"choice": "incomplete"}}}),
    )

    mgr = SubagentManager(runner=lambda task: "我看了一部分代码，还没跑测试。")
    task = SubagentTask(
        task_id="sub-2",
        parent_session="s1",
        parent_turn_id="t1",
        description="修改并跑通测试",
        subagent_type="implement",
    )
    mgr._tasks[task.task_id] = task
    mgr._run_task(task)

    assert task.status == "completed"
    assert "[JEV 验收提醒]" in task.result


# ---------------------------------------------------------------------------
# 4. plan_recovery：plan_retry 联动恢复策略
# ---------------------------------------------------------------------------

def test_plan_recovery_intercepts_blind_retry_on_split_smaller(data_dir, monkeypatch):
    from core import plan as plan_mod
    from core.task_scheduler import TaskScheduler

    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True, "shadow_mode": False})
    _mock_post(
        monkeypatch,
        lambda i, call: _resp(200, {"answers": {"q": {"choice": "split_smaller"}}}),
    )

    mgr = plan_mod.PlanManager()
    mgr.replace([{
        "id": "p1",
        "subject": "一次性重构整个后端并迁移全部测试",
        "status": "failed",
        "attempts": 1,
        "error": "step limit exceeded",
    }])
    scheduler = TaskScheduler(mgr)
    monkeypatch.setattr(plan_mod, "get_default_plan_manager", lambda: mgr)
    monkeypatch.setattr("core.task_scheduler.get_default_scheduler", lambda: scheduler)

    out = plan_mod.plan_retry("p1", force=False)
    assert "JEV 恢复建议：拆小或调整方案" in out
    assert mgr.snapshot()[0]["status"] == "failed"

    # force=True 时尊重显式强制重试
    forced = plan_mod.plan_retry("p1", force=True)
    assert "已重置为 pending" in forced
    assert mgr.snapshot()[0]["status"] == "pending"


def test_plan_recovery_allows_retry_same(data_dir, monkeypatch):
    from core import plan as plan_mod
    from core.task_scheduler import TaskScheduler

    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True, "shadow_mode": False})
    _mock_post(
        monkeypatch,
        lambda i, call: _resp(200, {"answers": {"q": {"choice": "retry_same"}}}),
    )

    mgr = plan_mod.PlanManager()
    mgr.replace([{
        "id": "p1",
        "subject": "跑单元测试",
        "status": "failed",
        "attempts": 1,
        "error": "temporary network glitch",
    }])
    scheduler = TaskScheduler(mgr)
    monkeypatch.setattr(plan_mod, "get_default_plan_manager", lambda: mgr)
    monkeypatch.setattr("core.task_scheduler.get_default_scheduler", lambda: scheduler)

    out = plan_mod.plan_retry("p1", force=False)
    assert "已重置为 pending" in out
    assert mgr.snapshot()[0]["status"] == "pending"


# ---------------------------------------------------------------------------
# 5. memory_filter：run_turn_learning 联动过滤噪音记忆
# ---------------------------------------------------------------------------

def test_memory_filter_blocks_noisy_writes_in_run_turn_learning(data_dir, monkeypatch):
    from core import learning as learning_mod

    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True, "shadow_mode": False})
    _mock_post(
        monkeypatch,
        lambda i, call: _resp(200, {"answers": {"q": {"choice": "skip_all"}}}),
    )

    class _FakeManager:
        def get_history_for_llm(self, session):
            return [{"role": "user", "content": f"msg {i}"} for i in range(8)]

    applied = []
    monkeypatch.setattr(
        learning_mod,
        "extract_turn_lessons",
        lambda s, h, r: {
            "user_facts": [{"kind": "preference", "text": "临时测试"}],
            "lessons": [{"slug": "tmp", "scene": "s", "how": "h"}],
            "self_notes": ["琐碎笔记"],
        },
    )
    monkeypatch.setattr(
        learning_mod,
        "apply_learning",
        lambda res: applied.append(res) or {"user_facts": 0, "lessons": 0, "self_notes": 0},
    )

    result = learning_mod.run_turn_learning("s1", _FakeManager(), ("m", "k", "u"))
    assert result is False
    assert applied == [{"user_facts": [], "lessons": [], "self_notes": []}]
