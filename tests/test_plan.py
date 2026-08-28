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
    # plan_retry 真正重置 failed→pending（attempts+1）时指纹必须变化：
    # TurnProgressGuard 据此清零停滞计数，两段式重试不被误杀
    plan, manager, session = _fingerprint_fixture(tmp_path, monkeypatch)
    assert manager.update("b", status="failed", attempts=1, error="boom") is True
    before = plan.plan_state_fingerprint(session)
    assert manager.update("b", status="pending", attempts=2) is True
    assert plan.plan_state_fingerprint(session) != before


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
