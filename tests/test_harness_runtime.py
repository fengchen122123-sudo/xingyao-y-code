import json
import threading
from types import SimpleNamespace

import pytest

from core.llm import chat_with_model_events
from core.memory import MemoryManager


def _chunk(content="", tool_calls=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content="",
        tool_calls=tool_calls,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(
        index=0,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _tool_stream(call_id, name, arguments):
    return iter([_chunk(tool_calls=[_tool_call(call_id, name, arguments)])])


def _text_stream(text):
    return iter([_chunk(character) for character in text])


def _cancelling_stream(cancel_event):
    def generate():
        yield _chunk("partial")
        cancel_event.set()
        yield _chunk("ignored")

    return generate()


def _persistent_runtime_fixture(mocker):
    manager = MemoryManager(":memory:")
    manager.create_session_explicit("session")
    cancel_event = threading.Event()
    mocker.patch("core.app_context.get_manager", return_value=manager)
    return manager, cancel_event


def _persistent_events(*, cancel_event=None):
    return chat_with_model_events(
        "fake-model",
        "key",
        [{"role": "user", "content": "q"}],
        persist=True,
        parent_session="session",
        turn_id="turn-runtime",
        cancel_event=cancel_event,
    )


def _steps(manager, turn_id):
    rows = manager.conn.execute(
        "SELECT ordinal, status FROM harness_steps "
        "WHERE turn_id = ? ORDER BY ordinal",
        (turn_id,),
    ).fetchall()
    return [{"ordinal": ordinal, "status": status} for ordinal, status in rows]


def test_persistent_turn_records_two_model_steps_for_one_tool_round(tmp_path, mocker):
    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit("session")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    mocker.patch(
        "core.llm.completion",
        side_effect=[
            _tool_stream(
                "call-1",
                "read_file",
                {"file_path": str(tmp_path / "a.py")},
            ),
            _text_stream("done"),
        ],
    )
    (tmp_path / "a.py").write_text("body", encoding="utf-8")

    list(
        chat_with_model_events(
            "fake-model",
            "key",
            [{"role": "user", "content": "read"}],
            persist=True,
            parent_session="session",
            turn_id="turn-runtime",
        )
    )

    turn = manager.get_harness_turn("turn-runtime")
    steps = _steps(manager, "turn-runtime")
    assert turn["status"] == "completed"
    assert [(item["ordinal"], item["status"]) for item in steps] == [
        (1, "completed"),
        (2, "completed"),
    ]
    committed = manager.conn.execute(
        "SELECT step_id, kind FROM harness_events "
        "WHERE turn_id = ? AND message_id IS NOT NULL ORDER BY sequence",
        ("turn-runtime",),
    ).fetchall()
    assert committed == [
        ("turn-runtime:step:1", "assistant_committed"),
        ("turn-runtime:step:1", "tool_result_committed"),
        ("turn-runtime:step:2", "assistant_committed"),
    ]


def test_nonpersistent_turn_uses_no_database_journal(mocker):
    manager = MemoryManager(":memory:")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    mocker.patch("core.llm.completion", return_value=_text_stream("answer"))

    events = list(
        chat_with_model_events(
            "fake-model",
            "key",
            [{"role": "user", "content": "q"}],
            persist=False,
            turn_id="turn-ephemeral",
        )
    )

    assert [event.kind for event in events] == ["assistant_delta"] * 6 + [
        "turn_finished"
    ]
    assert manager.get_harness_turn("turn-ephemeral") is None


def test_user_cancel_sets_cancelled_not_interrupted(mocker):
    manager, cancel_event = _persistent_runtime_fixture(mocker)
    mocker.patch(
        "core.llm.completion",
        return_value=_cancelling_stream(cancel_event),
    )

    with pytest.raises(KeyboardInterrupt):
        list(_persistent_events(cancel_event=cancel_event))

    assert manager.get_harness_turn("turn-runtime")["status"] == "cancelled"
    assert _steps(manager, "turn-runtime")[-1]["status"] == "cancelled"


def test_cancel_during_preparation_marks_the_turn_cancelled(mocker):
    manager, cancel_event = _persistent_runtime_fixture(mocker)
    mocker.patch(
        "core.llm._describe_images_with_secondary",
        side_effect=KeyboardInterrupt("cancel during preparation"),
    )

    with pytest.raises(KeyboardInterrupt):
        list(_persistent_events(cancel_event=cancel_event))

    assert manager.get_harness_turn("turn-runtime")["status"] == "cancelled"


def test_takeover_claim_failure_marks_preparing_turn_failed(mocker):
    """接管认领属于 Harness 准备阶段，异常也必须收口为 failed。"""
    manager, _cancel_event = _persistent_runtime_fixture(mocker)
    mocker.patch(
        "core.llm._claim_takeover_at_turn_start",
        side_effect=RuntimeError("takeover storage unavailable"),
    )

    with pytest.raises(RuntimeError, match="takeover storage unavailable"):
        list(_persistent_events())

    assert manager.get_harness_turn("turn-runtime")["status"] == "failed"
    assert _steps(manager, "turn-runtime") == []


def test_cancel_salvage_write_failure_marks_the_turn_failed(mocker):
    manager, cancel_event = _persistent_runtime_fixture(mocker)
    mocker.patch(
        "core.llm.completion",
        return_value=_tool_stream("call-1", "read_file", {"file_path": "a.py"}),
    )
    mocker.patch(
        "core.llm._execute_single_tool_call",
        side_effect=KeyboardInterrupt("cancel during tool"),
    )
    mocker.patch.object(
        manager,
        "add_messages_with_harness_events",
        side_effect=RuntimeError("salvage write failed"),
    )

    with pytest.raises(RuntimeError, match="salvage write failed"):
        list(_persistent_events(cancel_event=cancel_event))

    assert manager.get_harness_turn("turn-runtime")["status"] == "failed"
    assert _steps(manager, "turn-runtime")[-1]["status"] == "failed"


def test_provider_error_sets_failed_and_keeps_turn_error_event(mocker):
    manager, _cancel_event = _persistent_runtime_fixture(mocker)
    mocker.patch("core.llm.completion", side_effect=RuntimeError("provider failed"))
    mocker.patch("core.llm.time.sleep", return_value=None)

    events = list(_persistent_events())

    assert [event.kind for event in events].count("turn_error") == 1
    assert manager.get_harness_turn("turn-runtime")["status"] == "failed"


def test_round_limit_fails_turn_after_committing_the_last_step(mocker):
    # 用 write_file（修改类工具）让每步产生真实进展：只有真实进展才能
    # 越过 TurnProgressGuard 熔断抵达硬上限——纯观察类循环会先被熔断
    manager, _cancel_event = _persistent_runtime_fixture(mocker)
    load_limit = mocker.patch(
        "core.harness.turn_limits.load_turn_step_limit",
        return_value=3,
    )
    call_number = 0

    def next_tool_stream(**_kwargs):
        nonlocal call_number
        call_number += 1
        return _tool_stream(
            f"call-{call_number}",
            "write_file",
            {"file_path": f"file-{call_number}.py", "content": "x"},
        )

    mocker.patch("core.llm.completion", side_effect=next_tool_stream)
    mocker.patch("core.llm._execute_single_tool_call", side_effect=_fixed_tool_result)

    events = list(_persistent_events())

    turn = manager.get_harness_turn("turn-runtime")
    assert call_number == 3
    load_limit.assert_called_once_with()
    assert turn["status"] == "failed"
    assert turn["error_code"] == "StepLimitExceeded"
    steps = _steps(manager, "turn-runtime")
    assert len(steps) == 3
    assert all(step["status"] == "completed" for step in steps)
    kinds = [event.kind for event in events]
    assert kinds.count("turn_error") == 1
    assert "turn_finished" not in kinds
    history = manager.get_history("session")
    assert "3 轮" in str(history[-1]["content"])
    assert "任务未完成" in str(history[-1]["content"])
    # 事件顺序：上限 assistant 消息必须先于最后 Step 的终态 step_status
    final_step_events = manager.conn.execute(
        "SELECT sequence, kind FROM harness_events "
        "WHERE step_id = ? ORDER BY sequence",
        ("turn-runtime:step:3",),
    ).fetchall()
    final_assistant_sequence = max(
        sequence
        for sequence, kind in final_step_events
        if kind == "assistant_committed"
    )
    final_status_sequence = max(
        sequence for sequence, kind in final_step_events if kind == "step_status"
    )
    assert final_status_sequence > final_assistant_sequence


def test_message_persistence_failure_propagates_and_fails_the_turn(mocker):
    manager, _cancel_event = _persistent_runtime_fixture(mocker)
    mocker.patch("core.llm.completion", return_value=_text_stream("answer"))
    mocker.patch.object(
        manager,
        "add_messages_with_harness_events",
        side_effect=RuntimeError("message write failed"),
    )

    with pytest.raises(RuntimeError, match="message write failed"):
        list(_persistent_events())

    assert manager.get_harness_turn("turn-runtime")["status"] == "failed"


def _fixed_tool_result(tool_call, **_kwargs):
    return {
        "role": "tool",
        "tool_call_id": tool_call["id"],
        "name": tool_call["function"]["name"],
        "content": "ok",
    }


def _status_tool_result(tool_call, **_kwargs):
    return {
        "role": "tool",
        "tool_call_id": tool_call["id"],
        "name": tool_call["function"]["name"],
        "content": (
            '{"task_id":"t1","status":"running","revision":0,'
            '"terminal":false,"exit_code":null,"output":""}'
        ),
    }


def test_no_progress_guard_aborts_turn_before_seventh_request(tmp_path, mocker):
    # 模型连续返回同一 manage_task(status) 观察类调用：任务指纹/计划占位/结果哈希
    # 全部不变、无 mutation，第 6 个无进展 Step 后熔断，第 7 次 Provider 请求不得发送。
    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit("session")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    request_count = 0

    def next_tool_stream(**_kwargs):
        nonlocal request_count
        request_count += 1
        return _tool_stream(
            f"call-{request_count}",
            "manage_task",
            {"action": "status", "task_id": "t1"},
        )

    mocker.patch("core.llm.completion", side_effect=next_tool_stream)
    mocker.patch("core.llm._execute_single_tool_call", side_effect=_status_tool_result)

    events = list(_persistent_events())

    turn = manager.get_harness_turn("turn-runtime")
    assert turn["status"] == "failed"
    assert turn["error_code"] == "NoProgressError"
    # 有界读取进展预算：首次 manage_task 新读取签名算一次进展，此后同一签名
    # 重复调用不再算，熔断由第 6 次请求推迟到第 7 次（6 步停滞 + 1 步读取进展）
    assert request_count == 7
    # 用户可见错误：固定文案经 error 事件推送（GUI 据此展示 NoProgressError）
    error_events = [event for event in events if event.kind == "error"]
    assert any(
        "连续 6 轮没有检测到后台任务、计划或工具结果状态变化"
        in str(event.payload.get("message", ""))
        for event in error_events
    )
    # Harness 已落库 failed 时，公开事件也必须是失败终态；
    # 否则 GUI 会被随后的 turn_finished 覆盖成“已完成”。
    kinds = [event.kind for event in events]
    assert kinds.count("turn_error") == 1
    assert "turn_finished" not in kinds
    steps = _steps(manager, "turn-runtime")
    assert len(steps) == 7
    assert all(step["status"] == "completed" for step in steps)
    history = manager.get_history("session")
    assert history[-1]["role"] == "assistant"
    assert "连续 6 轮没有检测到" in str(history[-1]["content"])


def test_no_progress_guard_resets_when_mutation_committed(tmp_path, mocker):
    # 第 5 步之后出现一次真实修改（write_file 成功提交），停滞计数清零、Turn 继续。
    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit("session")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    request_count = 0

    def next_stream(**_kwargs):
        nonlocal request_count
        request_count += 1
        if request_count <= 5:
            return _tool_stream(
                f"call-{request_count}",
                "manage_task",
                {"action": "status", "task_id": "t1"},
            )
        if request_count == 6:
            return _tool_stream(
                "call-6",
                "write_file",
                {"file_path": "progress.txt", "content": "hello"},
            )
        return _text_stream("已完成。")

    mocker.patch("core.llm.completion", side_effect=next_stream)
    mocker.patch("core.llm._execute_single_tool_call", side_effect=_fixed_tool_result)

    list(_persistent_events())

    turn = manager.get_harness_turn("turn-runtime")
    assert turn["status"] == "completed"
    assert request_count == 7


def test_distinct_unplanned_subagents_are_progress_before_eighth_request(
    tmp_path, mocker
):
    """连续 7 个不同的无计划子任务均有成果时，不得在第 7 次前误熔断。"""
    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit("session")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    mocker.patch.dict("core.plan._PLAN_MANAGERS", {}, clear=True)
    request_count = 0

    def next_stream(**_kwargs):
        nonlocal request_count
        request_count += 1
        if request_count <= 7:
            return _tool_stream(
                f"call-{request_count}",
                "run_subagent",
                {
                    "task_description": f"核查独立模块 {request_count}",
                    "subagent_type": "explore",
                },
            )
        return _text_stream("七项核查均已完成。")

    def completed_subagent_result(tool_call, **_kwargs):
        ordinal = int(tool_call["id"].split("-")[-1])
        return {
            "role": "tool",
            "tool_call_id": tool_call["id"],
            "name": "run_subagent",
            "content": json.dumps({
                "status": "completed",
                "task_id": f"subagent-{ordinal:08x}",
                "result": "核查完成",
                "execution_elapsed": float(ordinal),
            }, ensure_ascii=False),
        }

    mocker.patch("core.llm.completion", side_effect=next_stream)
    mocker.patch(
        "core.llm._execute_single_tool_call",
        side_effect=completed_subagent_result,
    )

    list(_persistent_events())

    turn = manager.get_harness_turn("turn-runtime")
    assert turn["status"] == "completed"
    assert request_count == 8


def _plan_read_tool_result(tool_call, **_kwargs):
    return {
        "role": "tool",
        "tool_call_id": tool_call["id"],
        "name": tool_call["function"]["name"],
        "content": "  [failed    ] task-x            | 某计划项",
    }


def test_plan_read_loop_without_state_change_aborts(tmp_path, mocker):
    # 计划快照不变时反复 plan_read：结果文本相同、计划指纹不变、无 mutation，
    # 第 6 个无进展 Step 后熔断（spec 测试矩阵：计划 read/retry/subagent 循环）。
    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit("session")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    # 确保计划注册表无 "session" 条目：plan_state_fingerprint 返回稳定空态指纹
    mocker.patch.dict("core.plan._PLAN_MANAGERS", {}, clear=True)
    request_count = 0

    def next_tool_stream(**_kwargs):
        nonlocal request_count
        request_count += 1
        return _tool_stream(f"call-{request_count}", "plan_read", {})

    mocker.patch("core.llm.completion", side_effect=next_tool_stream)
    mocker.patch("core.llm._execute_single_tool_call", side_effect=_plan_read_tool_result)

    list(_persistent_events())

    turn = manager.get_harness_turn("turn-runtime")
    assert turn["status"] == "failed"
    assert turn["error_code"] == "NoProgressError"
    assert request_count == 6



def test_timeout_handoff_takeover_completes_without_redelegation(
    tmp_path, monkeypatch, mocker
):
    """真实超时重派 E2E：同项只建一个 subagent task；重派 blocked；
    主 Agent direct write/plan_finish 完成后 Turn 正常结束。"""
    import core.llm as llm
    import core.tools as tools_module
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    monkeypatch.chdir(tmp_path)
    manager = MemoryManager(":memory:")
    manager.create_session_explicit("session")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    mocker.patch("core.llm.app_context.get_permission_mode", lambda: "accept_edits")

    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "接管任务"}])
    scheduler = TaskScheduler(plan)
    mocker.patch("core.task_scheduler.get_default_scheduler", lambda: scheduler)
    mocker.patch("core.plan.get_default_plan_manager", lambda: plan)
    llm._mark_plan_turn("turn-e2e-takeover")

    class FakeMonotonic:
        def __init__(self, values):
            self._values = iter(values)

        def __call__(self):
            return next(self._values)

    slow_manager = tools_module.SubagentManager(
        runner=lambda record: record.cancel_event.wait(1),
        # 模型伪造的 timeout_seconds=30 已由 adapter 丢弃；general 角色使用
        # 系统 600 秒预算，因此假时钟必须跨过 600 才能触发真实 timeout。
        monotonic=FakeMonotonic([0.0, 601.0]),
        wait_interval=0.001,
    )
    mocker.patch.object(tools_module, "subagent_manager", slow_manager)

    out_file = tmp_path / "out.txt"
    out_file.write_text("old", encoding="utf-8")
    mocker.patch(
        "core.llm.completion",
        side_effect=[
            # 第 1 轮：模型伪造 30 秒被忽略，系统 600 秒预算到期后 handoff
            _tool_stream("call-1", "run_subagent", {
                "task_description": "做点活",
                "plan_item_id": "item",
                "timeout_seconds": 30,
            }),
            # 第 2 轮：同项重新派发 → blocked（main_takeover_required）
            _tool_stream("call-2", "run_subagent", {
                "task_description": "重新做一遍",
                "plan_item_id": "item",
                "timeout_seconds": 30,
            }),
            # 第 3 轮：主 Agent 直接写文件（scoped 接管放行）
            _tool_stream("call-3", "write_file", {
                "file_path": str(out_file),
                "content": "taken over",
            }),
            # 第 4 轮：显式收尾
            _tool_stream("call-4", "plan_finish", {
                "item_id": "item",
                "result": "现场核查完成，接管工作已落地",
            }),
            # 第 5 轮：正常文本收尾
            _text_stream("done"),
        ],
    )

    events = list(chat_with_model_events(
        "fake-model",
        "key",
        [{"role": "user", "content": "执行并接管"}],
        persist=True,
        parent_session="session",
        turn_id="turn-e2e-takeover",
    ))

    turn = manager.get_harness_turn("turn-e2e-takeover")
    assert turn["status"] == "completed"
    # 同一计划项只创建一个 subagent task id（重派被 blocked）
    assert len(slow_manager.snapshot()) == 1
    item = plan.snapshot()[0]
    assert item["status"] == "completed"
    assert item["execution_mode"] == "main_takeover"
    assert out_file.read_text(encoding="utf-8") == "taken over"
    assert not [event for event in events if event.kind == "error"]
