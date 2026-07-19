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

    message = plan_update("runtime", status="completed")

    assert "真实调度器" in message
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


def test_corrupt_plan_file_is_backed_up_and_starts_empty(tmp_path):
    storage = tmp_path / "plans"
    storage.mkdir()
    plan_file = storage / "broken.json"
    plan_file.write_text("{ not valid json !!!", encoding="utf-8")

    with pytest.warns(UserWarning, match="已损坏"):
        plan = PlanManager(storage_dir=storage, session_id="broken")

    assert plan.snapshot() == []
    assert not plan_file.exists()
    assert (storage / "broken.json.corrupt").exists()

    # 损坏文件被隔离后可以正常写入新计划
    plan.replace([{"id": "a", "subject": "新任务"}])
    assert plan.snapshot()[0]["id"] == "a"


def test_non_list_plan_json_is_backed_up_and_starts_empty(tmp_path):
    storage = tmp_path / "plans"
    storage.mkdir()
    plan_file = storage / "dict.json"
    # 合法 JSON 但不是计划项列表
    plan_file.write_text('{"id": "a", "subject": "任务"}', encoding="utf-8")

    with pytest.warns(UserWarning, match="已损坏"):
        plan = PlanManager(storage_dir=storage, session_id="dict")

    assert plan.snapshot() == []
    assert not plan_file.exists()
    assert (storage / "dict.json.corrupt").exists()


def test_invalid_utf8_plan_file_is_backed_up_and_starts_empty(tmp_path):
    storage = tmp_path / "plans"
    storage.mkdir()
    plan_file = storage / "garbled.json"
    plan_file.write_bytes(b"\xff\xfe\x00\x01 not json")

    with pytest.warns(UserWarning, match="已损坏"):
        plan = PlanManager(storage_dir=storage, session_id="garbled")

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
