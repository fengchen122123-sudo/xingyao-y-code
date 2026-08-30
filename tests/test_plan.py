from core.plan import PlanManager
import pytest


def test_plan_update_rejects_unknown_status():
    plan = PlanManager()
    plan.replace([{"id": "1", "subject": "test", "status": "pending"}])

    assert plan.update("1", status="invalid") is False
    assert plan.snapshot()[0]["status"] == "pending"


def test_plan_tracks_in_progress_item_and_returns_copies():
    plan = PlanManager()
    plan.replace([
        {"id": "1", "subject": "first", "status": "completed"},
        {"id": "2", "subject": "second", "status": "in_progress"},
    ])

    items = plan.snapshot()
    items[0]["subject"] = "changed outside manager"

    assert plan.in_progress()["id"] == "2"
    assert plan.snapshot()[0]["subject"] == "first"


def test_plan_tracks_dependencies_and_persists_across_manager_restart(tmp_path):
    plan = PlanManager(storage_dir=tmp_path, session_id="session-1")
    plan.replace([
        {"id": "explore", "subject": "探索代码", "status": "pending"},
        {
            "id": "implement",
            "subject": "修改代码",
            "status": "pending",
            "depends_on": ["explore"],
            "owner": "implement",
            "files": ["core/plan.py"],
            "verification": "pytest tests/test_plan.py",
        },
    ])

    assert [item["id"] for item in plan.ready_items()] == ["explore"]
    assert plan.update("explore", status="completed", result="找到相关模块") is True
    assert [item["id"] for item in plan.ready_items()] == ["implement"]

    restored = PlanManager(storage_dir=tmp_path, session_id="session-1")
    assert restored.snapshot()[0]["status"] == "completed"
    assert restored.snapshot()[1]["depends_on"] == ["explore"]
    assert restored.snapshot()[1]["verification"] == "pytest tests/test_plan.py"


def test_crashed_delegated_item_reload_clears_dead_lease(tmp_path):
    """普通委派在进程崩溃后只能恢复为可重新调度的 pending，
    不能凭非空 handoff_task_id 伪造“已超时接管”事实。"""
    from core.task_scheduler import TaskScheduler

    plan = PlanManager(storage_dir=tmp_path, session_id="delegated-crash")
    plan.replace([{"id": "item", "subject": "普通委派"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item(
        "item", "subagent-1234abcd", "subagent:implement", None
    )

    restored = PlanManager(storage_dir=tmp_path, session_id="delegated-crash")
    item = restored.snapshot()[0]

    assert item["status"] == "pending"
    assert item["execution_mode"] == ""
    assert item["handoff_task_id"] == ""
    assert item["takeover_required"] is False
    assert [candidate["id"] for candidate in restored.ready_items()] == ["item"]


def test_failed_delegated_item_remains_retryable_after_reload(tmp_path):
    """普通子 Agent 失败不是超时接管，重启后仍必须能 plan_retry。"""
    from core.task_scheduler import TaskScheduler

    plan = PlanManager(storage_dir=tmp_path, session_id="delegated-failed")
    plan.replace([{"id": "item", "subject": "可重试委派"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item(
        "item", "subagent-1234abcd", "subagent:implement", None
    )
    assert scheduler.fail("item", "provider failed")

    restored = PlanManager(storage_dir=tmp_path, session_id="delegated-failed")
    restored_scheduler = TaskScheduler(restored)

    assert restored.snapshot()[0]["takeover_required"] is False
    assert restored_scheduler.retry("item") is True
    assert restored.snapshot()[0]["status"] == "pending"


def test_plan_blocks_items_with_failed_dependencies():
    plan = PlanManager()
    plan.replace([
        {"id": "a", "subject": "前置任务", "status": "pending"},
        {"id": "b", "subject": "后置任务", "status": "pending", "depends_on": ["a"]},
    ])

    plan.update("a", status="failed", error="失败")

    assert plan.ready_items() == []
    assert plan.blocked_items()[0]["id"] == "b"


def test_plan_rejects_duplicate_unknown_and_cyclic_dependencies():
    plan = PlanManager()

    with pytest.raises(ValueError, match="duplicate"):
        plan.replace([
            {"id": "a", "subject": "一"},
            {"id": "a", "subject": "二"},
        ])

    with pytest.raises(ValueError, match="unknown dependency"):
        plan.replace([
            {"id": "a", "subject": "一", "depends_on": ["missing"]},
        ])

    with pytest.raises(ValueError, match="cycle"):
        plan.replace([
            {"id": "a", "subject": "一", "depends_on": ["b"]},
            {"id": "b", "subject": "二", "depends_on": ["a"]},
        ])


def test_plan_emits_snapshot_after_every_state_change():
    events = []
    plan = PlanManager(event_callback=lambda kind, payload: events.append((kind, payload)))
    plan.replace([{"id": "a", "subject": "任务"}])
    plan.update("a", status="in_progress")

    assert [kind for kind, _ in events] == ["plan_snapshot", "plan_snapshot"]
    assert events[-1][1]["items"][0]["status"] == "in_progress"


def test_public_plan_update_cannot_forge_runtime_completion():
    from core.plan import get_default_plan_manager, plan_update

    manager = get_default_plan_manager()
    manager.clear()
    manager.replace([{"id": "runtime", "subject": "真实执行"}])

    # plan_update 对受管状态（completed/in_progress 等）静默忽略，
    # 不再返回错误，但状态字段不会被伪造
    message = plan_update("runtime", status="completed")

    # 状态仍保持 pending，不会被 plan_update 伪造
    assert manager.snapshot()[0]["status"] == "pending"
    manager.clear()


def test_plan_update_rejects_invalid_stage_without_partial_mutation():
    plan = PlanManager()
    plan.replace([{"id": "a", "subject": "原始", "stage": "build"}])

    assert plan.update("a", subject="不应保存", stage="unknown") is False
    assert plan.snapshot()[0]["subject"] == "原始"
    assert plan.snapshot()[0]["stage"] == "build"


def test_plan_defaults_to_result_verification_and_validates_explicit_type():
    plan = PlanManager()
    plan.replace([{"id": "info", "subject": "查询信息", "verification": "返回真实结果"}])

    assert plan.snapshot()[0]["verification_type"] == "result"
    assert plan.update("info", verification_type="command") is True
    assert plan.snapshot()[0]["verification_type"] == "command"
    assert plan.update("info", verification_type="unknown") is False


def test_plan_write_rejects_human_description_as_command_verification():
    from core.plan import bind_default_plan_session, get_default_plan_manager, plan_write

    manager = get_default_plan_manager()
    previous_session = manager.session_id
    bind_default_plan_session("invalid-command-verification-test")
    manager.clear()
    try:
        message = plan_write([{
            "id": "time",
            "subject": "查询时间",
            "status": "pending",
            "verification_type": "command",
            "verification": "成功返回当前日期时间",
        }])

        assert "verification_type=command" in message
        assert manager.snapshot() == []
    finally:
        bind_default_plan_session(previous_session)


def test_corrupt_plan_file_is_backed_up_and_starts_empty(tmp_path, caplog):
    storage = tmp_path / "plans"
    storage.mkdir()
    plan_file = storage / "broken.json"
    plan_file.write_text("{ not valid json !!!", encoding="utf-8")

    with caplog.at_level("WARNING", logger="core.plan"):
        plan = PlanManager(storage_dir=storage, session_id="broken")

    assert "已损坏" in caplog.text
    assert plan.snapshot() == []
    assert not plan_file.exists()
    assert (storage / "broken.json.corrupt").exists()

    # 损坏文件被隔离后可以正常写入新计划
    plan.replace([{"id": "a", "subject": "新任务"}])
    assert plan.snapshot()[0]["id"] == "a"


def test_non_list_plan_json_is_backed_up_and_starts_empty(tmp_path, caplog):
    storage = tmp_path / "plans"
    storage.mkdir()
    plan_file = storage / "dict.json"
    # 合法 JSON 但不是计划项列表
    plan_file.write_text('{"id": "a", "subject": "任务"}', encoding="utf-8")

    with caplog.at_level("WARNING", logger="core.plan"):
        plan = PlanManager(storage_dir=storage, session_id="dict")

    assert "已损坏" in caplog.text
    assert plan.snapshot() == []
    assert not plan_file.exists()
    assert (storage / "dict.json.corrupt").exists()


def test_invalid_utf8_plan_file_is_backed_up_and_starts_empty(tmp_path, caplog):
    storage = tmp_path / "plans"
    storage.mkdir()
    plan_file = storage / "garbled.json"
    plan_file.write_bytes(b"\xff\xfe\x00\x01 not json")

    with caplog.at_level("WARNING", logger="core.plan"):
        plan = PlanManager(storage_dir=storage, session_id="garbled")

    assert "已损坏" in caplog.text
    assert plan.snapshot() == []
    assert not plan_file.exists()
    assert (storage / "garbled.json.corrupt").exists()


def test_plan_update_rejects_negative_attempts_without_partial_mutation():
    plan = PlanManager()
    plan.replace([{"id": "a", "subject": "原始", "attempts": 1}])

    assert plan.update("a", subject="不应保存", attempts=-1) is False
    item = plan.snapshot()[0]
    assert item["subject"] == "原始"
    assert item["attempts"] == 1


def test_retry_does_not_increment_attempt_until_next_start():
    """重置 retry 不得伪造新的真实执行次数。"""
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "retry counter"}])
    scheduler = TaskScheduler(plan)

    assert scheduler.start_item("item") is True
    assert plan.snapshot()[0]["attempts"] == 1
    assert scheduler.fail("item", "boom") is True
    assert scheduler.retry("item") is True
    assert plan.snapshot()[0]["attempts"] == 1
    assert scheduler.start_item("item") is True
    assert plan.snapshot()[0]["attempts"] == 2


def test_plan_retry_after_two_attempts_requires_explicit_force(monkeypatch):
    """失败两次后的重试必须由调用方显式确认。"""
    from core.plan import plan_retry
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{
        "id": "item",
        "subject": "force retry",
        "status": "failed",
        "attempts": 2,
        "error": "same failure",
    }])
    scheduler = TaskScheduler(plan)
    monkeypatch.setattr("core.task_scheduler.get_default_scheduler", lambda: scheduler)
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)

    assert "拒绝盲目重试" in plan_retry("item", force=False)
    assert plan.snapshot()[0]["status"] == "failed"
    assert "已重置为 pending" in plan_retry("item", force=True)
    assert plan.snapshot()[0]["status"] == "pending"


def test_handoff_keeps_item_in_progress_and_same_attempt():
    """子代理超时移交主 Agent：状态保持 in_progress，attempts 不变。"""
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "delegated work"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item(
        "item", "subagent-abcd", "subagent:implement", None
    ) is True
    item = scheduler.handoff_to_main(
        "item", "subagent-abcd", "turn-1", "foreground timeout"
    )
    assert item["status"] == "in_progress"
    assert item["attempts"] == 1
    assert item["execution_mode"] == "main_takeover"
    assert item["owner"] == "main:turn-1"
    assert item["handoff_task_id"] == "subagent-abcd"
    assert item["takeover_required"] is True


def test_takeover_required_survives_failure_retry_and_reload(tmp_path):
    """超时是不可逆业务事实：失败、普通 retry 和重载都不能重新开放子代理。"""
    from core.task_scheduler import TaskScheduler

    plan = PlanManager(storage_dir=tmp_path, session_id="takeover-history")
    plan.replace([{"id": "item", "subject": "delegated work"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item(
        "item", "subagent-abcd", "subagent:implement", None
    ) is True
    assert scheduler.handoff_to_main(
        "item", "subagent-abcd", "turn-1", "foreground timeout"
    ) is not None
    assert scheduler.fail("item", "verification failed") is True

    assert scheduler.retry("item") is False
    failed = plan.snapshot()[0]
    assert failed["status"] == "failed"
    assert failed["takeover_required"] is True

    restored = PlanManager(storage_dir=tmp_path, session_id="takeover-history")
    assert restored.snapshot()[0]["takeover_required"] is True


def test_claim_takeover_for_new_turn_rebinds_orphaned_active_item():
    """新主回合必须能认领绑定已死亡 turn 的 active takeover。"""
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "delegated work"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item(
        "item", "subagent-abcd", "subagent:implement", None
    ) is True
    assert scheduler.handoff_to_main(
        "item", "subagent-abcd", "old-turn", "foreground timeout"
    ) is not None

    claimed = scheduler.claim_takeover_for_turn("new-turn")

    assert claimed is not None
    assert claimed["id"] == "item"
    assert claimed["status"] == "in_progress"
    assert claimed["execution_mode"] == "main_takeover"
    assert claimed["takeover_turn_id"] == "new-turn"
    assert claimed["owner"] == "main:new-turn"
    assert claimed["takeover_required"] is True


def test_release_takeover_requeues_or_cancels_by_turn_outcome():
    """未收尾的系统退出要排队续接；用户取消必须保持 cancelled。"""
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "delegated work"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item(
        "item", "subagent-abcd", "subagent:implement", None
    ) is True
    assert scheduler.handoff_to_main(
        "item", "subagent-abcd", "turn-1", "foreground timeout"
    ) is not None

    released = scheduler.release_takeover_for_turn(
        "turn-1", cancelled=False, reason="turn ended before plan_finish"
    )
    assert released is not None
    assert released["status"] == "pending"
    assert released["execution_mode"] == "takeover_queued"
    assert released["takeover_turn_id"] == ""
    assert released["takeover_required"] is True

    assert scheduler.claim_takeover_for_turn("turn-2") is not None
    cancelled = scheduler.release_takeover_for_turn(
        "turn-2", cancelled=True, reason="user cancelled"
    )
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert cancelled["execution_mode"] == ""
    assert cancelled["takeover_required"] is True


def test_resume_takeover_rebinds_failed_item_without_new_attempt():
    """验证失败后的恢复仍由主 Agent 执行，且不增加原子代理尝试次数。"""
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "delegated work"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item(
        "item", "subagent-abcd", "subagent:implement", None
    ) is True
    assert scheduler.handoff_to_main(
        "item", "subagent-abcd", "turn-1", "foreground timeout"
    ) is not None
    assert scheduler.fail("item", "verification failed") is True

    resumed = scheduler.resume_takeover("item", "turn-2")

    assert resumed is not None
    assert resumed["status"] == "in_progress"
    assert resumed["execution_mode"] == "main_takeover"
    assert resumed["takeover_turn_id"] == "turn-2"
    assert resumed["attempts"] == 1
    assert resumed["delegation_count"] == 1
    assert resumed["takeover_required"] is True


def test_handoff_does_not_jump_a_preexisting_takeover_queue():
    """无 active 但已有 queued 时，新超时项不得越过更早的 FIFO 项。"""
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([
        {
            "id": "old",
            "subject": "old queued",
            "status": "pending",
            "execution_mode": "takeover_queued",
            "takeover_required": True,
            "takeover_sequence": 1,
            "handoff_task_id": "subagent-old",
        },
        {"id": "new", "subject": "new delegated"},
    ])
    scheduler = TaskScheduler(plan, max_workers=2)
    assert scheduler.start_delegated_item(
        "new", "subagent-new", "subagent:implement", None
    ) is True

    handed_off = scheduler.handoff_to_main(
        "new", "subagent-new", "turn-1", "foreground timeout"
    )

    assert handed_off is not None
    assert handed_off["execution_mode"] == "takeover_queued"
    claimed = scheduler.claim_takeover_for_turn("turn-1")
    assert claimed is not None
    assert claimed["id"] == "old"


def test_multiple_handoffs_queue_and_promote_fifo():
    """同会话多个同时超时项：第一个 active，其余 FIFO 排队，完成后按序提升。"""
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": item_id, "subject": item_id} for item_id in ("a", "b", "c")])
    scheduler = TaskScheduler(plan, max_workers=3)
    for item_id in ("a", "b", "c"):
        assert scheduler.start_delegated_item(
            item_id, f"s{item_id}", "subagent:implement", None
        ) is True
    active = scheduler.handoff_to_main("a", "sa", "turn-1", "timeout")
    queued_b = scheduler.handoff_to_main("b", "sb", "turn-1", "timeout")
    queued_c = scheduler.handoff_to_main("c", "sc", "turn-1", "timeout")
    assert active["queue_position"] == 0
    assert queued_b["queue_position"] == 1
    assert queued_c["queue_position"] == 2
    assert scheduler.active_takeover("turn-1")["id"] == "a"
    items = {item["id"]: item for item in plan.snapshot()}
    assert items["b"]["execution_mode"] == "takeover_queued"
    assert items["c"]["execution_mode"] == "takeover_queued"
    scheduler.complete("a", "done")
    assert scheduler.promote_next_takeover("turn-1")["id"] == "b"


def test_plan_finish_emits_handoff_for_promoted_fifo_item(monkeypatch):
    """plan_finish 提升下一项时必须发送可展示的统一接管事件。"""
    from core.plan import plan_finish
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([
        {"id": "a", "subject": "first"},
        {"id": "b", "subject": "second"},
    ])
    scheduler = TaskScheduler(plan, max_workers=2)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler", lambda: scheduler
    )
    for item_id in ("a", "b"):
        assert scheduler.start_delegated_item(
            item_id, f"subagent-{item_id * 8}", "subagent:implement", None
        )
    assert scheduler.handoff_to_main(
        "a", "subagent-aaaaaaaa", "turn-1", "timeout"
    ) is not None
    assert scheduler.handoff_to_main(
        "b", "subagent-bbbbbbbb", "turn-1", "timeout"
    ) is not None
    events = []

    message = plan_finish(
        "a",
        "verified",
        parent_turn_id="turn-1",
        event_callback=lambda kind, payload: events.append((kind, payload)),
    )

    assert "已提升" in message
    handoffs = [payload for kind, payload in events if kind == "plan_item_handoff"]
    assert len(handoffs) == 1
    assert handoffs[0]["item"]["id"] == "b"
    assert handoffs[0]["mode"] == "main_takeover"
    assert handoffs[0]["queue_position"] == 0
    assert handoffs[0]["source_task_id"] == "subagent-bbbbbbbb"


def test_handoff_rejects_mismatched_source_task_id():
    """handoff 必须校验真实执行租约 id，防止伪造接管。"""
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "delegated work"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item(
        "item", "subagent-abcd", "subagent:implement", None
    ) is True
    assert scheduler.handoff_to_main(
        "item", "subagent-other", "turn-1", "timeout"
    ) is None
    items = {item["id"]: item for item in plan.snapshot()}
    assert items["item"]["execution_mode"] == "delegated"
    assert items["item"]["status"] == "in_progress"


def test_loaded_main_takeover_downgrades_to_queued_for_next_turn(tmp_path):
    """崩溃恢复：遗留 main_takeover 降级为 queued 并清空旧 turn id，可被重新认领。"""
    import json

    storage = tmp_path / "plans"
    storage.mkdir()
    plan_file = storage / "crash.json"
    plan_file.write_text(
        json.dumps([{
            "id": "item",
            "subject": "crash takeover",
            "status": "in_progress",
            "execution_mode": "main_takeover",
            "takeover_turn_id": "old-turn",
            "takeover_sequence": 5,
            "handoff_task_id": "subagent-old",
            "handoff_reason": "foreground timeout",
        }], ensure_ascii=False),
        encoding="utf-8",
    )
    plan = PlanManager(storage_dir=storage, session_id="crash")
    item = plan.snapshot()[0]
    assert item["status"] == "pending"
    assert item["execution_mode"] == "takeover_queued"
    assert item["takeover_turn_id"] == ""
    assert item["takeover_required"] is True

    from core.task_scheduler import TaskScheduler

    scheduler = TaskScheduler(plan)
    promoted = scheduler.promote_next_takeover("new-turn")
    assert promoted is not None
    assert promoted["id"] == "item"
    assert promoted["status"] == "in_progress"
    assert promoted["execution_mode"] == "main_takeover"
    assert promoted["takeover_turn_id"] == "new-turn"
    assert promoted["owner"] == "main:new-turn"


def test_plan_write_rejects_missing_id_and_subject_with_clear_errors():
    from core.plan import bind_default_plan_session, get_default_plan_manager, plan_write

    manager = get_default_plan_manager()
    previous_session = manager.session_id
    bind_default_plan_session("missing-fields-test")
    manager.clear()
    try:
        # 缺少 id
        message = plan_write([{"subject": "没有id", "status": "pending"}])
        assert "missing required field 'id'" in message
        assert manager.snapshot() == []

        # 缺少 subject
        message = plan_write([{"id": "no-subject", "status": "pending"}])
        assert "missing required field 'subject'" in message
        assert "no-subject" in message
        assert manager.snapshot() == []
    finally:
        bind_default_plan_session(previous_session)


def test_plan_write_rejects_non_list_items():
    from core.plan import bind_default_plan_session, get_default_plan_manager, plan_write

    manager = get_default_plan_manager()
    previous_session = manager.session_id
    bind_default_plan_session("non-list-items-test")
    manager.clear()
    try:
        message = plan_write(None)
        assert "必须传入 items 数组" in message
        assert manager.snapshot() == []

        message = plan_write({"id": "a", "subject": "b"})
        assert "items 必须是数组" in message
        assert manager.snapshot() == []
    finally:
        bind_default_plan_session(previous_session)


def test_plan_auto_continue_suppression_lifecycle():
    """用户取消 → suppress；恢复执行/计划清空 → clear；空会话名是 no-op。"""
    from core.plan import (
        clear_plan_auto_continue_suppression,
        plan_auto_continue_suppressed,
        suppress_plan_auto_continue,
    )

    session = "s-nudge-suppress"
    try:
        assert not plan_auto_continue_suppressed(session)
        suppress_plan_auto_continue(session)
        assert plan_auto_continue_suppressed(session)
        suppress_plan_auto_continue("")  # no-op，不抛异常
        clear_plan_auto_continue_suppression(session)
        assert not plan_auto_continue_suppressed(session)
    finally:
        clear_plan_auto_continue_suppression(session)


def test_maybe_clear_completed_plan_clears_when_all_terminal(tmp_path, monkeypatch):
    """显式 session_id 路由：全部终态时清空并返回 True。"""
    import core.plan as plan
    manager = plan.PlanManager(storage_dir=tmp_path, session_id="clear-all-terminal")
    manager.replace([
        {"id": "a", "subject": "任务A", "status": "completed"},
        {"id": "b", "subject": "任务B", "status": "failed"},
    ])
    monkeypatch.setattr(plan, "_PLAN_MANAGERS", {"clear-all-terminal": manager})

    assert plan.maybe_clear_completed_plan("clear-all-terminal") is True
    assert manager.snapshot() == []


def test_maybe_clear_completed_plan_keeps_pending(tmp_path, monkeypatch):
    """仍有 pending 项时不清空（跨回合任务不能被提前抹掉）。"""
    import core.plan as plan
    manager = plan.PlanManager(storage_dir=tmp_path, session_id="clear-has-pending")
    manager.replace([
        {"id": "a", "subject": "任务A", "status": "completed"},
        {"id": "b", "subject": "任务B", "status": "pending"},
    ])
    monkeypatch.setattr(plan, "_PLAN_MANAGERS", {"clear-has-pending": manager})

    assert plan.maybe_clear_completed_plan("clear-has-pending") is False
    assert len(manager.snapshot()) == 2


def test_maybe_clear_completed_plan_noop_when_empty(tmp_path, monkeypatch):
    """无计划项时返回 False，不误报清空。"""
    import core.plan as plan
    manager = plan.PlanManager(storage_dir=tmp_path, session_id="clear-empty")
    monkeypatch.setattr(plan, "_PLAN_MANAGERS", {"clear-empty": manager})

    assert plan.maybe_clear_completed_plan("clear-empty") is False
    assert manager.snapshot() == []


def test_maybe_clear_completed_plan_noarg_routes_default_manager(tmp_path, monkeypatch):
    """不传 session_id（CLI 路径）：走 get_default_plan_manager 默认路由。"""
    import core.plan as plan
    manager = plan.PlanManager(storage_dir=tmp_path, session_id="clear-noarg")
    manager.replace([
        {"id": "a", "subject": "任务A", "status": "cancelled"},
    ])
    monkeypatch.setattr(plan, "get_default_plan_manager", lambda: manager)

    assert plan.maybe_clear_completed_plan() is True
    assert manager.snapshot() == []


def test_maybe_clear_completed_plan_does_not_create_registry_entry(monkeypatch):
    """显式 session_id 且注册表无条目（早退回合场景）：no-op 返回 False，
    且绝不惰性创建条目——get_plan_manager_for 会以当前线程 workdir 推导
    storage_dir，早退路径 workdir 未绑定会创建指向进程 cwd 的污染条目。"""
    import core.plan as plan
    monkeypatch.setattr(plan, "_PLAN_MANAGERS", {})
    assert plan.maybe_clear_completed_plan("never-seen-session") is False
    assert "never-seen-session" not in plan._PLAN_MANAGERS


def _fingerprint_fixture(tmp_path, monkeypatch, session="fp-session"):
    import core.plan as plan

    manager = plan.PlanManager(storage_dir=tmp_path, session_id=session)
    manager.replace([
        {"id": "a", "subject": "探索", "status": "pending"},
        {"id": "b", "subject": "实施", "status": "pending", "depends_on": ["a"]},
    ])
    monkeypatch.setattr(plan, "_PLAN_MANAGERS", {session: manager})
    return plan, manager, session


def test_plan_state_fingerprint_stable_for_same_state(tmp_path, monkeypatch):
    plan, _manager, session = _fingerprint_fixture(tmp_path, monkeypatch)
    assert plan.plan_state_fingerprint(session) == plan.plan_state_fingerprint(session)


def test_plan_state_fingerprint_changes_on_status_change(tmp_path, monkeypatch):
    plan, manager, session = _fingerprint_fixture(tmp_path, monkeypatch)
    before = plan.plan_state_fingerprint(session)
    assert manager.update("a", status="completed") is True
    assert plan.plan_state_fingerprint(session) != before


def test_plan_state_fingerprint_changes_on_retry_reset(tmp_path, monkeypatch):
    # retry 重置 failed→pending 时指纹必须变化，attempts 仅在下一次真实 start 时增加：
    # TurnProgressGuard 据此清零停滞计数。
    plan, manager, session = _fingerprint_fixture(tmp_path, monkeypatch)
    assert manager.update("b", status="failed", attempts=1, error="boom") is True
    before = plan.plan_state_fingerprint(session)
    assert manager.update("b", status="pending", attempts=2) is True
    assert plan.plan_state_fingerprint(session) != before


def test_plan_state_fingerprint_ignores_attempt_only_changes(tmp_path, monkeypatch):
    """attempts 是诊断计数，不得伪装成可调度状态进展。"""
    plan, manager, session = _fingerprint_fixture(tmp_path, monkeypatch)
    before = plan.plan_state_fingerprint(session)

    assert manager.update("a", attempts=1) is True

    assert plan.plan_state_fingerprint(session) == before


def test_plan_state_fingerprint_ignores_display_text(tmp_path, monkeypatch):
    # subject/result/error 等展示文案变化不算业务状态变化，指纹保持稳定
    plan, manager, session = _fingerprint_fixture(tmp_path, monkeypatch)
    before = plan.plan_state_fingerprint(session)
    assert manager.update("a", subject="改写后的任务标题") is True
    assert manager.update("a", result="新的结果文案", error="新的错误文案") is True
    assert plan.plan_state_fingerprint(session) == before


def test_plan_state_fingerprint_tracks_auto_continue_suppression(tmp_path, monkeypatch):
    plan, _manager, session = _fingerprint_fixture(tmp_path, monkeypatch)
    before = plan.plan_state_fingerprint(session)
    plan.suppress_plan_auto_continue(session)
    try:
        assert plan.plan_state_fingerprint(session) != before
    finally:
        plan.clear_plan_auto_continue_suppression(session)


def test_plan_state_fingerprint_tracks_in_progress_item(tmp_path, monkeypatch):
    plan, manager, session = _fingerprint_fixture(tmp_path, monkeypatch)
    before = plan.plan_state_fingerprint(session)
    assert manager.update("a", status="in_progress") is True
    after = plan.plan_state_fingerprint(session)
    assert after != before
    assert manager.update("a", status="completed") is True
    # in_progress 从 a 变回 None：与 before 不同（a 的 status 已变），
    # 与"in_progress 为 a"的状态也不同
    assert plan.plan_state_fingerprint(session) != after


def test_plan_state_fingerprint_unknown_session_is_stable_and_clean(tmp_path, monkeypatch):
    # 未知会话：不惰性创建注册表条目（早退回合场景），返回稳定空态指纹
    import core.plan as plan
    monkeypatch.setattr(plan, "_PLAN_MANAGERS", {})
    first = plan.plan_state_fingerprint("never-planned")
    assert first == plan.plan_state_fingerprint("never-planned")
    assert "never-planned" not in plan._PLAN_MANAGERS


def test_plan_finish_completes_takeover_via_shared_verification(monkeypatch):
    """接管项经 plan_finish 完成：验证共用、状态回写、不重新委派。"""
    from core.plan import plan_finish
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "takeover finish"}])
    scheduler = TaskScheduler(plan)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler", lambda: scheduler
    )
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)

    assert scheduler.start_delegated_item(
        "item", "subagent-f1n1", "subagent:implement", None
    ) is True
    assert scheduler.handoff_to_main(
        "item", "subagent-f1n1", "turn-1", "timeout"
    ) is not None

    message = plan_finish(
        "item", "现场核查完成，结果有效", parent_turn_id="turn-1"
    )
    assert "已完成" in message
    item = plan.snapshot()[0]
    assert item["status"] == "completed"
    assert item["result"] == "现场核查完成，结果有效"
    # 完成后无排队项，接管流程结束
    assert scheduler.active_takeover("turn-1") is None


def test_public_plan_update_cannot_reset_active_takeover_to_pending():
    from core.plan import get_default_plan_manager, plan_update
    from core.task_scheduler import TaskScheduler

    manager = get_default_plan_manager()
    manager.clear()
    manager.replace([{"id": "item", "subject": "work"}])
    scheduler = TaskScheduler(manager)
    assert scheduler.start_delegated_item(
        "item", "subagent-abcd", "subagent:implement", None
    )
    assert scheduler.handoff_to_main(
        "item", "subagent-abcd", "turn-1", "timeout"
    ) is not None
    before = manager.snapshot()[0]

    message = plan_update("item", status="pending")

    assert "状态变更由调度器自动管理" in message
    assert manager.snapshot()[0] == before
    manager.clear()


def test_public_plan_update_cannot_edit_execution_locked_item():
    from core.plan import get_default_plan_manager, plan_update

    manager = get_default_plan_manager()
    manager.clear()
    manager.replace([{
        "id": "item",
        "subject": "work",
        "status": "pending",
        "files": ["a.py"],
        "execution_mode": "takeover_queued",
        "takeover_required": True,
    }])
    before = manager.snapshot()[0]

    message = plan_update("item", subject="changed", files=["b.py"])

    assert "执行租约" in message
    assert manager.snapshot()[0] == before
    manager.clear()


def test_public_plan_update_rejects_metadata_edit_when_lease_starts_after_snapshot(
    monkeypatch,
):
    """计划项在读取快照后被认领时，旧快照不得绕过执行租约保护。"""
    from core.plan import get_default_plan_manager, plan_update

    manager = get_default_plan_manager()
    manager.clear()
    manager.replace([{
        "id": "item",
        "subject": "work",
        "files": ["a.py"],
    }])
    original_snapshot = manager.snapshot
    lease_started = False

    def snapshot_then_start_lease():
        nonlocal lease_started
        snapshot = original_snapshot()
        if not lease_started:
            lease_started = True
            claimed = manager.transition_execution(
                "item",
                expected_status="pending",
                expected_mode="",
                status="in_progress",
                execution_mode="delegated",
                owner="subagent:implement",
                attempts=1,
                delegation_count=1,
                handoff_task_id="subagent-abcd1234",
            )
            assert claimed is not None
        return snapshot

    monkeypatch.setattr(manager, "snapshot", snapshot_then_start_lease)

    message = plan_update("item", subject="changed", files=["b.py"])

    item = original_snapshot()[0]
    assert "无法更新" in message
    assert item["status"] == "in_progress"
    assert item["execution_mode"] == "delegated"
    assert item["subject"] == "work"
    assert item["files"] == ["a.py"]
    manager.clear()


def test_public_plan_update_allows_metadata_edit_outside_execution_lease():
    from core.plan import get_default_plan_manager, plan_update

    manager = get_default_plan_manager()
    manager.clear()
    manager.replace([
        {"id": "plain", "subject": "普通项", "files": ["a.py"]},
        {
            "id": "takeover",
            "subject": "失败接管项",
            "status": "failed",
            "files": ["a.py"],
            "takeover_required": True,
            "error": "验证失败",
        },
    ])

    # 普通 pending 项允许更新元数据
    message = plan_update("plain", subject="改名", files=["b.py"])
    assert "已更新" in message
    plain = next(i for i in manager.snapshot() if i["id"] == "plain")
    assert plain["subject"] == "改名"
    assert plain["files"] == ["b.py"]

    # failed 接管项已释放运行租约：允许先修改元数据，再走 plan_resume_takeover
    message = plan_update("takeover", subject="修正描述", files=["c.py"])
    assert "已更新" in message
    takeover = next(i for i in manager.snapshot() if i["id"] == "takeover")
    assert takeover["subject"] == "修正描述"
    assert takeover["files"] == ["c.py"]
    assert takeover["status"] == "failed"
    assert takeover["takeover_required"] is True

    # 但仍拒绝任何运行字段写入；接管项只能走 plan_resume_takeover
    before = manager.snapshot()
    message = plan_update("takeover", status="pending")
    assert "状态变更由调度器自动管理" in message
    assert "plan_resume_takeover" in message
    assert manager.snapshot() == before
    manager.clear()
