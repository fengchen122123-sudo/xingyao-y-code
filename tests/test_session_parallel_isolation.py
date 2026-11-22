import threading
from types import SimpleNamespace

import pytest

import core.app_context as app_context
from core import plan as plan_module
from core.workspace_context import clear_workdir, set_workdir


class SessionRoute:

    def __init__(self, session_id, workdir):
        self.session_id = str(session_id)
        self.workdir = str(workdir)

    def __enter__(self):
        app_context.set_thread_session(self.session_id)
        plan_module.set_current_plan_session(self.session_id)
        set_workdir(self.workdir)
        return self

    def __exit__(self, *exc):
        plan_module.set_current_plan_session(None)
        app_context.set_thread_session(None)
        clear_workdir()
        return False


def start_session_worker(session_id, workdir, fn, results, key, barrier=None):
    outcome = {}

    def _worker():
        with SessionRoute(session_id, workdir):
            if barrier is not None:
                try:
                    barrier.wait(timeout=30)
                except threading.BrokenBarrierError:
                    pass
            try:
                outcome["value"] = ("ok", fn())
            except Exception as error:  # noqa: BLE001 - 诊断用途
                outcome["value"] = ("error", repr(error))

    thread = threading.Thread(
        target=_worker, name=f"session-{session_id}", daemon=True
    )
    thread.start()

    def _finalize():
        thread.join(timeout=60)
        results[key] = outcome.get("value", ("error", "worker timed out"))

    return _finalize


def join_workers(*finalizers):
    for finalize in finalizers:
        finalize()


def run_in_session(session_id, workdir, fn, results, key, barrier=None):
    finalize = start_session_worker(session_id, workdir, fn, results, key, barrier)
    finalize()


@pytest.fixture(autouse=True)
def clean_session_state():
    import core.llm as llm
    from core import permission_grants, tools as tools_module

    def _reset():
        with plan_module._PLAN_MANAGERS_LOCK:
            plan_module._PLAN_MANAGERS.clear()
        permission_grants._clear_all_for_tests()
        llm._last_tracked_session = None
        with tools_module._session_sandbox_lock:
            tools_module._session_sandbox_bypass_by_session.clear()
            tools_module._session_sandbox_bypass = False
        app_context.set_thread_session(None)
        plan_module.set_current_plan_session(None)
        app_context._current_session_fallback = ""
        clear_workdir()

    _reset()
    yield
    _reset()


def test_plan_managers_isolated_between_parallel_sessions(tmp_path):
    dir_a, dir_b = tmp_path / "projA", tmp_path / "projB"
    dir_a.mkdir()
    dir_b.mkdir()
    barrier = threading.Barrier(2)
    results = {}

    def _write_plan(marker):
        def _fn():
            manager = plan_module.get_default_plan_manager()
            manager.replace([
                {"id": f"{marker}-1", "subject": f"任务-{marker}", "status": "pending"},
            ])
            for _ in range(20):
                snapshot = manager.snapshot()
                assert len(snapshot) == 1
                assert snapshot[0]["id"] == f"{marker}-1"
            return [item["id"] for item in snapshot]
        return _fn

    run_a = start_session_worker("sess-A", dir_a, _write_plan("A"), results, "A", barrier)
    run_b = start_session_worker("sess-B", dir_b, _write_plan("B"), results, "B", barrier)
    join_workers(run_a, run_b)

    assert results["A"][0] == "ok", results["A"]
    assert results["B"][0] == "ok", results["B"]
    assert results["A"][1] == ["A-1"]
    assert results["B"][1] == ["B-1"]

    plan_file_a = dir_a / ".ycode_tasks" / "plans" / "sess-A.json"
    plan_file_b = dir_b / ".ycode_tasks" / "plans" / "sess-B.json"
    assert plan_file_a.exists()
    assert plan_file_b.exists()


def test_plan_isolated_between_sessions_in_same_directory(tmp_path):
    shared = tmp_path / "shared-proj"
    shared.mkdir()
    barrier = threading.Barrier(2)
    results = {}

    def _write_plan(session_marker):
        def _fn():
            manager = plan_module.get_default_plan_manager()
            manager.replace([
                {"id": f"{session_marker}-only", "subject": session_marker, "status": "pending"},
            ])
            barrier.wait(timeout=30)
            ids = {item["id"] for item in manager.snapshot()}
            assert ids == {f"{session_marker}-only"}, f"串会话: {ids}"
            return sorted(ids)
        return _fn

    run_a = start_session_worker("same-dir-A", shared, _write_plan("A"), results, "A", barrier)
    run_b = start_session_worker("same-dir-B", shared, _write_plan("B"), results, "B", barrier)
    join_workers(run_a, run_b)

    assert results["A"][0] == "ok", results["A"]
    assert results["B"][0] == "ok", results["B"]
    plans_dir = shared / ".ycode_tasks" / "plans"
    assert (plans_dir / "same-dir-A.json").exists()
    assert (plans_dir / "same-dir-B.json").exists()


def test_plan_manager_registry_reuses_instance_per_session(tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    results = {}

    def _first():
        manager = plan_module.get_default_plan_manager()
        manager.replace([
            {"id": "p1", "subject": "先探索", "status": "completed"},
        ])
        return manager

    def _second():
        manager = plan_module.get_default_plan_manager()
        snapshot = manager.snapshot()
        return manager, [item["id"] for item in snapshot]

    run_in_session("reuse-sess", workdir, _first, results, "first")
    run_in_session("reuse-sess", workdir, _second, results, "second")

    assert results["first"][0] == "ok", results["first"]
    assert results["second"][0] == "ok", results["second"]
    assert results["first"][1] is results["second"][1][0]
    assert results["second"][1][1] == ["p1"]

    plan_module.release_plan_manager("reuse-sess")
    run_in_session("reuse-sess", workdir, _second, results, "restored")
    assert results["restored"][0] == "ok", results["restored"]
    assert results["restored"][1][1] == ["p1"]
    assert results["restored"][1][0] is not results["first"][1]


def test_task_grant_survives_visible_session_switch(monkeypatch):
    from core import app_context
    from core.permission_grants import grant_command, matches_command

    visible = {"name": "task-a"}
    monkeypatch.setattr(app_context, "get_current_session", lambda: visible["name"])
    grant_command("git status")
    visible["name"] = "task-b"
    assert matches_command("git status --short") is False
    visible["name"] = "task-a"
    assert matches_command("git status --short") is True


def test_bound_background_task_keeps_its_own_grant(monkeypatch):
    from core import app_context
    from core.permission_grants import grant_file, matches_file

    app_context.set_thread_session("background-a")
    try:
        grant_file("write", "C:/project/a.py")
        monkeypatch.setattr(app_context, "get_current_session", lambda: "visible-b")
        assert matches_file("write", "C:/project/a.py") is True
    finally:
        app_context.set_thread_session(None)

def test_command_grants_isolated_between_sessions(tmp_path):
    import core.llm as llm

    dir_a, dir_b = tmp_path / "projA", tmp_path / "projB"
    dir_a.mkdir()
    dir_b.mkdir()
    results = {}

    def _grant():
        llm._grant_command_add(("git", "status"))
        return llm._grant_command_hit("git status --short")

    def _check_other():
        return llm._grant_command_hit("git status --short")

    run_in_session("grant-A", dir_a, _grant, results, "A")
    run_in_session("grant-B", dir_b, _check_other, results, "B")

    assert results["A"] == ("ok", True)
    assert results["B"] == ("ok", False)

    run_in_session("grant-A", dir_a, _check_other, results, "A2")
    assert results["A2"] == ("ok", True)


def test_write_edit_grants_isolated_between_sessions(tmp_path):
    import core.llm as llm

    dir_a, dir_b = tmp_path / "projA", tmp_path / "projB"
    dir_a.mkdir()
    dir_b.mkdir()
    target = str(dir_a / "owned.txt")
    canonical = llm._canonical_permission_path(target)
    results = {}

    def _grant():
        llm._grant_write_add(canonical)
        llm._grant_edit_add(canonical)
        return (
            llm._grant_write_hit(canonical),
            llm._grant_edit_hit(canonical),
        )

    def _check_other():
        return (
            llm._grant_write_hit(canonical),
            llm._grant_edit_hit(canonical),
        )

    run_in_session("file-A", dir_a, _grant, results, "A")
    run_in_session("file-B", dir_b, _check_other, results, "B")

    assert results["A"] == ("ok", (True, True))
    assert results["B"] == ("ok", (False, False))


def test_check_session_reset_keeps_task_grants_across_visible_switches(tmp_path):
    import core.llm as llm

    workdir = tmp_path / "proj"
    workdir.mkdir()
    results = {}

    def _grant_and_park():
        llm._grant_command_add(("npm", "run"))
        app_context._current_session_fallback = "sess-other"
        llm.check_session_reset()
        return llm._grant_command_hit("npm run build")

    run_in_session("sess-running", workdir, _grant_and_park, results, "bound")
    assert results["bound"] == ("ok", True)

    app_context.set_current_session("cli-sess-1")
    llm._grant_command_add(("ls",))
    assert llm._grant_command_hit("ls") is True
    app_context.set_current_session("cli-sess-2")
    llm.check_session_reset()
    assert llm._grant_command_hit("ls") is False
    app_context.set_current_session("cli-sess-1")
    assert llm._grant_command_hit("ls") is True


def test_sandbox_bypass_isolated_between_sessions(tmp_path):
    from core import tools as tools_module

    dir_a, dir_b = tmp_path / "projA", tmp_path / "projB"
    dir_a.mkdir()
    dir_b.mkdir()
    results = {}

    def _enable():
        tools_module.set_session_sandbox_bypass(True)
        return tools_module.is_session_sandbox_bypass_enabled()

    def _check_other():
        return tools_module.is_session_sandbox_bypass_enabled()

    run_in_session("sandbox-A", dir_a, _enable, results, "A")
    run_in_session("sandbox-B", dir_b, _check_other, results, "B")

    assert results["A"] == ("ok", True)
    assert results["B"] == ("ok", False)

    def _disable_b():
        tools_module.set_session_sandbox_bypass(False)
        return tools_module.is_session_sandbox_bypass_enabled()

    run_in_session("sandbox-B", dir_b, _disable_b, results, "B2")
    run_in_session("sandbox-A", dir_a, _check_other, results, "A2")
    assert results["B2"] == ("ok", False)
    assert results["A2"] == ("ok", True)


def test_many_sessions_parallel_zero_crosstalk(tmp_path):
    import core.llm as llm
    from core import tools as tools_module

    session_count = 8
    dirs = []
    for i in range(session_count):
        workdir = tmp_path / f"proj{i // 2}"
        workdir.mkdir(exist_ok=True)
        dirs.append(workdir)

    barrier = threading.Barrier(session_count)
    results = {}

    def _make_worker(index):
        sid = f"stress-{index}"

        def _fn():
            manager = plan_module.get_default_plan_manager()
            manager.replace([
                {"id": f"item-{index}", "subject": f"任务{index}", "status": "pending"},
            ])
            llm._grant_command_add((f"tool{index}", "run"))
            tools_module.set_session_sandbox_bypass(index % 2 == 0)
            barrier.wait(timeout=30)
            ids = {item["id"] for item in manager.snapshot()}
            return (
                ids == {f"item-{index}"},
                llm._grant_command_hit(f"tool{index} run build"),
                llm._grant_command_hit(f"tool{(index + 1) % session_count} run build"),
                tools_module.is_session_sandbox_bypass_enabled() == (index % 2 == 0),
            )

        return sid, dirs[index], _fn

    finalizers = []
    for i in range(session_count):
        sid, workdir, fn = _make_worker(i)
        finalizers.append(
            start_session_worker(sid, workdir, fn, results, sid, barrier)
        )
    join_workers(*finalizers)

    for i in range(session_count):
        status, value = results[f"stress-{i}"]
        assert status == "ok", f"stress-{i}: {value}"
        plan_ok, own_grant, other_grant, sandbox_ok = value
        assert plan_ok, f"会话 {i} 计划串扰"
        assert own_grant, f"会话 {i} 自己的授权丢失"
        assert not other_grant, f"会话 {i} 命中了别的会话的授权"
        assert sandbox_ok, f"会话 {i} 沙盒放行状态串扰"


def test_subagent_dirs_follow_thread_workdir(tmp_path):
    from core import subagent_registry

    proj_a, proj_b = tmp_path / "projA", tmp_path / "projB"
    agents_a = proj_a / ".ycode" / "agents"
    agents_b = proj_b / ".ycode" / "agents"
    agents_a.mkdir(parents=True)
    agents_b.mkdir(parents=True)

    results = {}
    run_in_session(
        "dirs-A", proj_a,
        lambda: [str(d) for d in subagent_registry.get_agent_search_dirs()],
        results, "A",
    )
    run_in_session(
        "dirs-B", proj_b,
        lambda: [str(d) for d in subagent_registry.get_agent_search_dirs()],
        results, "B",
    )

    assert results["A"][0] == "ok", results["A"]
    assert results["B"][0] == "ok", results["B"]
    assert str(agents_a) in results["A"][1]
    assert str(agents_b) not in results["A"][1]
    assert str(agents_b) in results["B"][1]
    assert str(agents_a) not in results["B"][1]


def test_skills_relative_paths_follow_thread_workdir(tmp_path):
    from core import skills

    proj_a, proj_b = tmp_path / "projA", tmp_path / "projB"
    proj_a.mkdir()
    proj_b.mkdir()

    results = {}
    run_in_session(
        "skills-A", proj_a, lambda: skills._expand_path("./skills-local"), results, "A",
    )
    run_in_session(
        "skills-B", proj_b, lambda: skills._expand_path("./skills-local"), results, "B",
    )

    import os

    assert results["A"][1] == os.path.normpath(str(proj_a / "skills-local"))
    assert results["B"][1] == os.path.normpath(str(proj_b / "skills-local"))


def test_subagent_model_snapshot_uses_parent_session_when_global_was_overwritten(
    monkeypatch,
):
    from core import tools as tools_module

    app_context.set_active_model_tuple("openai/model-B", "key-B", "url-B")
    monkeypatch.setattr(
        "core.model_manager.get_secondary_roles", lambda: {"subagent": False}
    )
    models = {
        "session-A": {
            "id": "model-id-A",
            "litellm_model": "openai/model-A",
            "api_key": "key-A",
            "base_url": "url-A",
        },
        "session-B": {
            "id": "model-id-B",
            "litellm_model": "openai/model-B",
            "api_key": "key-B",
            "base_url": "url-B",
        },
    }
    monkeypatch.setattr(
        "core.model_manager.get_active_model", lambda session=None: models.get(session)
    )

    assert tools_module._snapshot_model_config(session_name="session-A") == (
        "openai/model-A", "key-A", "url-A", "model-id-A",
    )


def test_explicit_session_model_lookup_never_reuses_process_tuple(monkeypatch):
    from core import tools as tools_module

    app_context.set_active_model_tuple("openai/model-B", "key-B", "url-B")
    monkeypatch.setattr(
        "core.model_manager.get_secondary_roles", lambda: {"subagent": False}
    )
    monkeypatch.setattr("core.model_manager.get_active_model", lambda _session=None: None)

    assert tools_module._snapshot_model_config(session_name="session-A") is None


def test_subagent_manager_run_snapshots_parent_session_model(monkeypatch):
    from core import tools as tools_module

    monkeypatch.setattr(
        "core.model_manager.get_secondary_roles", lambda: {"subagent": False}
    )
    monkeypatch.setattr(
        "core.model_manager.get_active_model",
        lambda session=None: {
            "id": f"id-{session}",
            "litellm_model": f"openai/{session}",
            "api_key": f"key-{session}",
            "base_url": f"url-{session}",
        },
    )
    captured = []
    manager = tools_module.SubagentManager()
    manager._runner = lambda task: captured.append(task.model_config) or "完成"

    manager.run("检查模型快照", "session-A", "turn-A", timeout_seconds=30)

    assert captured == [(
        "openai/session-A", "key-session-A", "url-session-A", "id-session-A"
    )]


def test_custom_inherit_uses_parent_session_model(monkeypatch):
    from core import tools as tools_module

    monkeypatch.setattr(
        "core.model_manager.get_active_model",
        lambda session=None: {
            "id": "model-id-A",
            "litellm_model": "openai/model-A",
            "api_key": "key-A",
            "base_url": "url-A",
        } if session == "session-A" else None,
    )
    captured = {}

    def fake_events(model, key, messages, url, **kwargs):
        captured.update(model=model, key=key, url=url, model_id=kwargs.get("model_id"))
        yield SimpleNamespace(
            kind="assistant_delta",
            payload={"text": "完整结论" * 40},
        )

    monkeypatch.setattr("core.llm.chat_with_model_events", fake_events)
    manager = tools_module.SubagentManager(runner=lambda _task: "unused")
    task = tools_module.SubagentTask(
        task_id="custom-inherit",
        parent_session="session-A",
        parent_turn_id="turn-A",
        description="验证继承",
        model_config=("openai/model-B", "key-B", "url-B"),
        custom_config={"model": "inherit", "base_type": "general"},
    )

    manager._run_default_subagent(task)

    assert captured == {
        "model": "openai/model-A", "key": "key-A", "url": "url-A",
        "model_id": "model-id-A",
    }


def test_unbound_thread_falls_back_to_default_singletons(tmp_path):
    import core.llm as llm
    from core import tools as tools_module

    default_plan = plan_module.get_default_plan_manager()
    assert default_plan is plan_module._DEFAULT_PLAN_MANAGER
    assert app_context.get_thread_session() is None

    llm._grant_command_add(("echo",))
    assert llm._grant_command_hit("echo") is True
    assert ("echo",) in llm.session_allowed_commands

    tools_module.set_session_sandbox_bypass(True)
    assert tools_module.is_session_sandbox_bypass_enabled() is True
    tools_module.set_session_sandbox_bypass(False)


def test_takeover_queue_is_isolated_between_session_schedulers(tmp_path):
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan_a = PlanManager(storage_dir=tmp_path / "plans-a", session_id="iso-a")
    plan_b = PlanManager(storage_dir=tmp_path / "plans-b", session_id="iso-b")
    plan_a.replace([
        {"id": "item", "subject": "work-a"},
        {"id": "item-a2", "subject": "work-a2"},
    ])
    plan_b.replace([
        {"id": "item", "subject": "work-b"},
        {"id": "item-b2", "subject": "work-b2"},
    ])
    scheduler_a = TaskScheduler(plan_a, max_workers=2)
    scheduler_b = TaskScheduler(plan_b, max_workers=2)

    for scheduler, source, turn in (
        (scheduler_a, "subagent-a", "turn-a"),
        (scheduler_b, "subagent-b", "turn-b"),
    ):
        for item_id in ("item", "item-a2" if scheduler is scheduler_a else "item-b2"):
            assert scheduler.start_delegated_item(
                item_id, f"{source}-{item_id}", "subagent:implement", None
            ) is True
        assert scheduler.handoff_to_main(
            "item", f"{source}-item", turn, "timeout"
        ) is not None
        assert scheduler.handoff_to_main(
            "item-a2" if scheduler is scheduler_a else "item-b2",
            f"{source}-{'item-a2' if scheduler is scheduler_a else 'item-b2'}",
            turn,
            "timeout",
        ) is not None

    assert scheduler_a.active_takeover("turn-a")["id"] == "item"
    assert scheduler_b.active_takeover("turn-b")["id"] == "item"
    assert scheduler_a.active_takeover("turn-b") is None
    assert scheduler_b.active_takeover("turn-a") is None

    assert scheduler_a.complete("item", "done") is True
    assert scheduler_a.promote_next_takeover("turn-a")["id"] == "item-a2"
    assert scheduler_a.complete("item-a2", "done") is True
    assert scheduler_a.promote_next_takeover("turn-a") is None

    assert scheduler_b.complete("item", "done") is True
    promoted_b = scheduler_b.promote_next_takeover("turn-b")
    assert promoted_b is not None
    assert promoted_b["id"] == "item-b2"
