import threading

from core.plan import PlanManager
from core.task_scheduler import TaskScheduler


def test_scheduler_runs_ready_tasks_and_unblocks_dependents(tmp_path):
    plan = PlanManager(storage_dir=tmp_path, session_id="scheduler")
    plan.replace([
        {"id": "a", "subject": "探索", "status": "pending"},
        {"id": "b", "subject": "实现", "status": "pending", "depends_on": ["a"]},
    ])
    events = []
    scheduler = TaskScheduler(plan, event_callback=lambda kind, payload: events.append((kind, payload)))

    assert scheduler.run_ready(lambda item: f"完成:{item['id']}") == ["a"]
    assert plan.snapshot()[0]["status"] == "completed"
    assert [item["id"] for item in plan.ready_items()] == ["b"]
    assert ("plan_item_started", "a") in [
        (kind, payload["item"]["id"]) for kind, payload in events if "item" in payload
    ]

    scheduler.run_ready(lambda item: f"完成:{item['id']}")
    assert plan.snapshot()[1]["status"] == "completed"


def test_scheduler_does_not_run_tasks_with_overlapping_files():
    plan = PlanManager()
    plan.replace([
        {"id": "a", "subject": "改文件 A", "files": ["shared.py"]},
        {"id": "b", "subject": "改文件 B", "files": ["shared.py"]},
        {"id": "c", "subject": "改文件 C", "files": ["other.py"]},
    ])
    scheduler = TaskScheduler(plan, max_workers=2)

    started = scheduler.start_ready()

    assert [item["id"] for item in started] == ["a", "c"]
    assert plan.snapshot()[1]["status"] == "pending"
    scheduler.cancel_all()


def test_scheduler_marks_failure_and_retry_requeues_item():
    plan = PlanManager()
    plan.replace([{"id": "a", "subject": "不稳定任务"}])
    scheduler = TaskScheduler(plan)

    scheduler.run_ready(lambda item: (_ for _ in ()).throw(RuntimeError("boom")))
    failed = plan.snapshot()[0]
    assert failed["status"] == "failed"
    assert failed["attempts"] == 1
    assert "boom" in failed["error"]

    assert scheduler.retry("a") is True
    retried = plan.snapshot()[0]
    assert retried["status"] == "pending"
    # attempts 只在真实执行开始时自增（Task 3 语义）：retry 仅重置状态，
    # 下一次 run_ready/start_item 才会计入第 2 次尝试
    assert retried["attempts"] == 1
    assert [item["id"] for item in scheduler.start_ready()] == ["a"]
    assert plan.snapshot()[0]["attempts"] == 2


def test_scheduler_pause_and_resume():
    plan = PlanManager()
    plan.replace([{"id": "a", "subject": "任务"}])
    scheduler = TaskScheduler(plan)

    scheduler.pause()
    assert scheduler.start_ready() == []
    assert plan.snapshot()[0]["status"] == "pending"

    scheduler.resume()
    assert [item["id"] for item in scheduler.start_ready()] == ["a"]
    scheduler.complete("a", "完成")
    assert plan.snapshot()[0]["status"] == "completed"


def test_scheduler_start_item_enforces_dependencies_and_emits_live_snapshot():
    plan = PlanManager()
    plan.replace([
        {"id": "a", "subject": "前置"},
        {"id": "b", "subject": "后置", "depends_on": ["a"]},
    ])
    events = []
    scheduler = TaskScheduler(plan)

    assert scheduler.start_item("b", event_callback=lambda kind, payload: events.append((kind, payload))) is False
    assert scheduler.start_item("a", event_callback=lambda kind, payload: events.append((kind, payload))) is True
    assert plan.snapshot()[0]["status"] == "in_progress"
    assert events[-1][0] == "plan_item_started"
    scheduler.complete("a", "已完成")
    assert [item["id"] for item in plan.ready_items()] == ["b"]


def test_scheduler_verification_failure_does_not_complete_item():
    plan = PlanManager()
    plan.replace([{
        "id": "a",
        "subject": "验证任务",
        "verification_type": "command",
        "verification": "pytest -q",
    }])
    scheduler = TaskScheduler(plan, verification_runner=lambda command: (False, "测试失败"))

    assert scheduler.start_item("a") is True
    assert scheduler.complete("a", "实现完成") is False
    item = plan.snapshot()[0]
    assert item["status"] == "failed"
    assert "测试失败" in item["error"]


def test_result_verification_never_executes_human_description_as_shell():
    plan = PlanManager()
    plan.replace([{
        "id": "news",
        "subject": "查询新闻",
        "verification_type": "result",
        "verification": "确认返回5条且与AI相关",
    }])
    executed = []
    scheduler = TaskScheduler(
        plan,
        verification_runner=lambda command: executed.append(command) or (False, "不应执行"),
    )

    assert scheduler.start_item("news") is True
    assert scheduler.complete("news", "1. AI 新闻\nhttps://example.com") is True
    assert executed == []
    assert plan.snapshot()[0]["status"] == "completed"


def test_failed_command_verification_preserves_child_result():
    plan = PlanManager()
    plan.replace([{
        "id": "news",
        "subject": "查询新闻",
        "verification_type": "command",
        "verification": "verify-news",
    }])
    scheduler = TaskScheduler(plan, verification_runner=lambda command: (False, "验证失败"))

    assert scheduler.start_item("news") is True
    assert scheduler.complete("news", "真实搜索结果") is False
    item = plan.snapshot()[0]
    assert item["status"] == "failed"
    assert item["result"] == "真实搜索结果"


def test_command_verification_decodes_windows_console_output(monkeypatch):
    class FakeProcess:
        returncode = 1

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            return "验证命令失败".encode("gbk"), b""

    monkeypatch.setattr("core.process_io.platform_console_encoding", lambda: "cp936")
    monkeypatch.setattr("core.task_scheduler.subprocess.Popen", lambda *args, **kwargs: FakeProcess())
    plan = PlanManager()
    plan.replace([{
        "id": "command",
        "subject": "命令验证",
        "verification_type": "command",
        "verification": "verify",
    }])
    scheduler = TaskScheduler(
        plan,
        verification_runner=lambda command: scheduler._run_verification(command),
    )

    assert scheduler.start_item("command") is True
    assert scheduler.complete("command", "结果") is False
    error = plan.snapshot()[0]["error"]
    assert "验证命令失败" in error
    assert "�" not in error


def test_command_verification_without_authorized_runner_fails_closed(monkeypatch):
    """缺少授权 runner 时，计划验证不得自行启动 shell。"""
    launches = []

    class FakeProcess:
        returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            return b"verification passed", b""

    monkeypatch.setattr(
        "core.task_scheduler.subprocess.Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)) or FakeProcess(),
    )
    plan = PlanManager()
    plan.replace([{
        "id": "build",
        "subject": "构建项目",
        "verification_type": "command",
        "verification": "pytest -q",
    }])
    scheduler = TaskScheduler(plan)

    assert scheduler.start_item("build") is True
    assert scheduler.complete("build", "实现完成") is False
    assert launches == []
    item = plan.snapshot()[0]
    assert item["status"] == "failed"
    assert "授权" in item["error"]


def test_command_verification_uses_injected_runner_only():
    plan = PlanManager()
    plan.replace([{
        "id": "build",
        "subject": "构建项目",
        "verification_type": "command",
        "verification": "pytest -q",
    }])
    seen = []
    scheduler = TaskScheduler(plan)

    assert scheduler.start_item("build") is True
    assert scheduler.complete(
        "build",
        "实现完成",
        verification_runner=lambda command: (seen.append(command) or True, "ok"),
    ) is True
    assert seen == ["pytest -q"]
    assert plan.snapshot()[0]["status"] == "completed"


def test_scheduler_cancel_all_cancels_pending_and_in_progress_items():
    plan = PlanManager()
    plan.replace([
        {"id": "running", "subject": "运行中"},
        {"id": "pending", "subject": "待执行"},
    ])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_item("running") is True

    assert scheduler.cancel_all() == 2
    assert {item["status"] for item in plan.snapshot()} == {"cancelled"}


def test_cancel_wins_atomic_race_against_delayed_completion(monkeypatch):
    plan = PlanManager()
    plan.replace([{"id": "question", "subject": "确认城市"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_item("question") is True

    completion_entered = threading.Event()
    allow_completion = threading.Event()
    original_mark_completed = plan.mark_completed

    def delayed_mark_completed(item_id, result=""):
        completion_entered.set()
        assert allow_completion.wait(1)
        return original_mark_completed(item_id, result)

    monkeypatch.setattr(plan, "mark_completed", delayed_mark_completed)
    completion_result = []
    thread = threading.Thread(
        target=lambda: completion_result.append(
            scheduler.complete("question", "北京")
        )
    )
    thread.start()
    assert completion_entered.wait(1)

    assert scheduler.cancel("question", "用户取消提问") is True
    allow_completion.set()
    thread.join(timeout=1)

    assert completion_result == [False]
    assert plan.snapshot()[0]["status"] == "cancelled"


def test_verification_with_large_output_does_not_deadlock():
    import sys

    plan = PlanManager()
    plan.replace([{
        "id": "big",
        "subject": "大输出验证",
        "verification_type": "command",
        # 输出远超 64KB 管道缓冲区，旧的 poll 轮询实现会在此死锁
        "verification": f'"{sys.executable}" -c "print(\'x\' * 300000)"',
    }])
    scheduler = TaskScheduler(
        plan,
        verification_runner=lambda command: scheduler._run_verification(command),
    )

    assert scheduler.start_item("big") is True
    assert scheduler.complete("big", "结果") is True
    assert plan.snapshot()[0]["status"] == "completed"


def test_verification_honors_cancel_event_and_stops_process(monkeypatch):
    import sys
    import subprocess as subprocess_module

    stopped_processes = []

    class FakeProcess:
        pid = 12345
        returncode = None
        stdout = None
        stderr = None

        def poll(self):
            return None

        def communicate(self, timeout=None):
            raise subprocess_module.TimeoutExpired(cmd="verify", timeout=timeout)

    monkeypatch.setattr(
        "core.task_scheduler.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        "core.task_scheduler._stop_process_tree",
        lambda process: stopped_processes.append(process.pid),
    )

    cancel_event = threading.Event()
    cancel_event.set()
    plan = PlanManager()
    plan.replace([{
        "id": "slow",
        "subject": "慢验证",
        "verification_type": "command",
        "verification": f'"{sys.executable}" -c "import time; time.sleep(60)"',
    }])
    scheduler = TaskScheduler(
        plan,
        verification_runner=lambda command: scheduler._run_verification(
            command,
            cancel_event=cancel_event,
        ),
    )

    assert scheduler.start_item("slow") is True
    started = threading.Event()
    result = []

    def complete():
        started.set()
        result.append(scheduler.complete("slow", "结果", cancel_event=cancel_event))

    thread = threading.Thread(target=complete)
    thread.start()
    assert started.wait(1)
    thread.join(timeout=15)

    assert not thread.is_alive()
    assert result == [False]
    item = plan.snapshot()[0]
    assert item["status"] == "failed"
    assert "已取消" in item["error"]
    assert stopped_processes == [12345]


def test_verification_cancel_reaps_output_without_blocking(monkeypatch):
    """detached 孙进程继承管道句柄时 communicate 可能永久阻塞；
    取消/超时分支必须带超时回收并强关管道，避免验证线程悬挂。"""
    import subprocess as subprocess_module

    closed_streams = []

    class FakeStream:
        def close(self):
            closed_streams.append(self)

    class StuckProcess:
        pid = 99999
        returncode = None

        def __init__(self):
            self.stdout = FakeStream()
            self.stderr = FakeStream()

        def poll(self):
            return None

        def communicate(self, timeout=None):
            raise subprocess_module.TimeoutExpired(cmd="stuck", timeout=timeout)

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

        def terminate(self):
            pass

    monkeypatch.setattr(
        "core.task_scheduler.subprocess.Popen",
        lambda *args, **kwargs: StuckProcess(),
    )
    stop_calls = []
    monkeypatch.setattr(
        "core.task_scheduler._stop_process_tree",
        lambda process: stop_calls.append(process.pid),
    )

    scheduler = TaskScheduler(PlanManager())
    cancel_event = threading.Event()
    cancel_event.set()

    passed, detail = scheduler._run_verification("stuck", cancel_event=cancel_event)

    assert passed is False
    assert "已取消" in detail
    assert stop_calls == [99999]
    assert len(closed_streams) == 2


def test_blocked_item_revives_when_dependency_reset_pending_via_plan_update():
    plan = PlanManager()
    plan.replace([
        {"id": "a", "subject": "前置"},
        {"id": "b", "subject": "后置", "depends_on": ["a"]},
    ])
    scheduler = TaskScheduler(plan)

    assert scheduler.start_item("a") is True
    assert scheduler.fail("a", "失败") is True
    assert scheduler.start_ready() == []
    assert plan.snapshot()[1]["status"] == "blocked"

    # 模拟 plan_update 直接把依赖项重置为 pending（不经过 complete）
    assert plan.update("a", status="pending", error="") is True
    scheduler.start_ready()

    assert plan.snapshot()[1]["status"] == "pending"


def test_blocked_item_revives_after_dependency_completes():
    plan = PlanManager()
    plan.replace([
        {"id": "a", "subject": "前置"},
        {"id": "b", "subject": "后置", "depends_on": ["a"]},
    ])
    scheduler = TaskScheduler(plan)

    assert scheduler.start_item("a") is True
    assert scheduler.fail("a", "失败") is True
    assert scheduler.start_ready() == []
    assert plan.snapshot()[1]["status"] == "blocked"

    assert scheduler.retry("a") is True
    assert scheduler.start_item("a") is True
    assert scheduler.complete("a", "完成") is True

    revived = plan.snapshot()[1]
    assert revived["status"] == "pending"
    assert [item["id"] for item in plan.ready_items()] == ["b"]


def test_operations_on_missing_item_return_false():
    plan = PlanManager()
    scheduler = TaskScheduler(plan)

    assert scheduler.complete("missing", "结果") is False
    assert scheduler.fail("missing", "错误") is False
    assert scheduler.cancel("missing") is False
    assert scheduler.retry("missing") is False


def test_session_switch_does_not_cancel_new_session_items():
    from core.plan import bind_default_plan_session, get_default_plan_manager
    from core.task_scheduler import get_default_scheduler
    import core.task_scheduler as scheduler_module

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("switch-old-session")
    plan.replace([{"id": "old", "subject": "旧任务"}])
    old_scheduler = get_default_scheduler()
    assert old_scheduler.start_item("old") is True

    bind_default_plan_session("switch-new-session")
    plan.replace([{"id": "new", "subject": "新任务"}])
    new_scheduler = get_default_scheduler()

    assert new_scheduler is not old_scheduler
    assert plan.snapshot()[0]["status"] == "pending"
    assert new_scheduler.start_item("new") is True
    new_scheduler.cancel_all()
    bind_default_plan_session(previous_session)
    scheduler_module._DEFAULT_SCHEDULER = None
    scheduler_module._DEFAULT_SCHEDULER_SESSION = None


def test_verification_runs_in_process_cwd_not_item_file_dir(monkeypatch):
    """回归：验证命令必须与 execute_command 一样在项目工作目录执行，
    不得根据计划项 files 把 cwd 偷换成文件所在目录（否则模型按项目根
    目录写的 `cd subdir && ...` 会永远报"系统找不到指定的路径"）。"""
    captured = {}

    class FakeProcess:
        returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            return b"ok", b""

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("core.task_scheduler.subprocess.Popen", fake_popen)
    plan = PlanManager()
    plan.replace([{
        "id": "impl",
        "subject": "实现工具",
        "files": ["subdir/tool.py"],
        "verification_type": "command",
        "verification": "cd subdir && python tool.py",
    }])
    scheduler = TaskScheduler(
        plan,
        verification_runner=lambda command: scheduler._run_verification(command),
    )

    assert scheduler.start_item("impl") is True
    assert scheduler.complete("impl", "结果") is True
    assert captured.get("cwd") is None
    assert plan.snapshot()[0]["status"] == "completed"


def test_verification_rejects_catastrophic_commands_before_spawn(monkeypatch):
    monkeypatch.setattr(
        "core.task_scheduler.subprocess.Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    scheduler = TaskScheduler(PlanManager())

    passed, detail = scheduler._run_verification("rm -rf / && echo done")

    assert passed is False
    assert "危险命令" in detail


def test_release_uses_claimed_file_snapshot_not_mutated_plan_files():
    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "work", "files": ["a.py"]}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_item("item") is True

    # 模拟内部调用方绕过模型边界修改计划，调度器仍必须释放真实租约。
    assert plan.update("item", files=["b.py"]) is True
    assert scheduler.cancel("item") is True

    assert scheduler._in_flight_ids == set()
    assert scheduler._in_flight_files == set()
    assert scheduler._in_flight_files_by_item == {}


def test_promote_recovered_takeover_claims_files_before_activation():
    plan = PlanManager()
    plan.replace([{
        "id": "item",
        "subject": "recovered",
        "status": "pending",
        "files": ["shared.py"],
        "execution_mode": "takeover_queued",
        "takeover_required": True,
        "takeover_sequence": 1,
    }])
    scheduler = TaskScheduler(plan)

    promoted = scheduler.promote_next_takeover("turn-2")

    assert promoted is not None
    assert promoted["execution_mode"] == "main_takeover"
    assert scheduler._in_flight_ids == {"item"}
    assert scheduler._in_flight_files == {"shared.py"}
    assert scheduler._in_flight_files_by_item == {
        "item": frozenset({"shared.py"})
    }


def test_promote_takeover_keeps_queue_when_files_are_claimed():
    plan = PlanManager()
    plan.replace([
        {"id": "active", "subject": "active", "files": ["shared.py"]},
        {
            "id": "queued",
            "subject": "queued",
            "status": "pending",
            "files": ["shared.py"],
            "execution_mode": "takeover_queued",
            "takeover_required": True,
            "takeover_sequence": 1,
        },
    ])
    scheduler = TaskScheduler(plan, max_workers=2)
    assert scheduler.start_item("active") is True

    assert scheduler.promote_next_takeover("turn-2") is None
    queued = next(item for item in plan.snapshot() if item["id"] == "queued")
    assert queued["status"] == "pending"
    assert queued["execution_mode"] == "takeover_queued"


def test_promote_refuses_queued_item_without_takeover_flag():
    """CAS 防御：takeover_queued 但 takeover_required=False 的异常项不得晋升，
    与 claim_takeover_for_turn / resume_takeover 的接管防御级别保持一致。

    plan 层 _normalize_item 会把 queued 模式强制补上 takeover_required，
    因此这里用 transition_execution 模拟内部调用方绕过不变量的场景。"""
    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "broken-flag", "status": "pending", "files": ["a.py"]}])
    assert plan.transition_execution(
        "item",
        expected_status="pending",
        expected_mode="",
        status="pending",
        execution_mode="takeover_queued",
        takeover_required=False,
        takeover_sequence=1,
    ) is not None
    scheduler = TaskScheduler(plan)

    assert scheduler.promote_next_takeover("turn-2") is None
    item = next(item for item in plan.snapshot() if item["id"] == "item")
    assert item["status"] == "pending"
    assert item["execution_mode"] == "takeover_queued"
    assert scheduler._in_flight_ids == set()
