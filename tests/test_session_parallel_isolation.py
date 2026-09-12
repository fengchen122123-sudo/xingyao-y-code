"""多会话并发隔离回归测试。

覆盖三组修复的隔离契约：
- H1：PlanManager 按会话实例化 + 线程路由，计划互不可见；
- H2：授权白名单（命令/写/编辑）与会话级沙盒放行按会话分桶，
      UI 切换全局会话不再收回运行中回合的授权；
- M 组：项目级目录（subagent_registry / skills 相对路径）跟随线程工作目录。

模拟 gui_bridge 回合线程的完整绑定协议：
    app_context.set_thread_session + plan.set_current_plan_session + set_workdir
"""

import threading
from types import SimpleNamespace

import pytest

import core.app_context as app_context
from core import plan as plan_module
from core.workspace_context import clear_workdir, set_workdir


# ---------------------------------------------------------------------------
# 基础设施：会话路由绑定 / 状态清理
# ---------------------------------------------------------------------------

class SessionRoute:
    """模拟 gui_bridge 回合线程入口的会话绑定协议（进入绑定、退出解绑）。"""

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
    """启动一个绑定会话路由的线程执行 fn（不阻塞等待）。

    返回 finalize 闭包：调用时 join 线程并把结果写入 results[key]。
    需要并行的用例先把所有 worker 启动完，再统一 finalize。
    """
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
    """单 worker 便捷版：立即启动并等待完成，结果写入 results[key]。"""
    finalize = start_session_worker(session_id, workdir, fn, results, key, barrier)
    finalize()


@pytest.fixture(autouse=True)
def clean_session_state():
    """测试前后清空所有按会话分桶的全局状态，避免用例间串扰。"""
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


# ---------------------------------------------------------------------------
# H1：计划按会话隔离
# ---------------------------------------------------------------------------

def test_plan_managers_isolated_between_parallel_sessions(tmp_path):
    """两个不同目录的会话并发写计划，各自只看到自己的计划项。"""
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
            # 并发窗口内反复读取，验证对端写入不会串进来
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

    # 落盘文件也按会话分开，各自目录下只有自己的计划
    plan_file_a = dir_a / ".ycode_tasks" / "plans" / "sess-A.json"
    plan_file_b = dir_b / ".ycode_tasks" / "plans" / "sess-B.json"
    assert plan_file_a.exists()
    assert plan_file_b.exists()


def test_plan_isolated_between_sessions_in_same_directory(tmp_path):
    """同一目录下的两个会话（GUI 多会话打开同一项目）计划互不可见。

    计划存储文件按 session_id 命名（<sid>.json），即使 workdir 相同也天然分离。
    """
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
    # 同一存储目录下两个会话各自的持久化文件
    plans_dir = shared / ".ycode_tasks" / "plans"
    assert (plans_dir / "same-dir-A.json").exists()
    assert (plans_dir / "same-dir-B.json").exists()


def test_plan_manager_registry_reuses_instance_per_session(tmp_path):
    """同会话多次路由拿到同一实例；释放后重建能从磁盘恢复计划。"""
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
    # 未释放时同会话路由拿到同一实例
    assert results["first"][1] is results["second"][1][0]
    assert results["second"][1][1] == ["p1"]

    # 释放注册表条目后重建：新实例从磁盘完整恢复
    plan_module.release_plan_manager("reuse-sess")
    run_in_session("reuse-sess", workdir, _second, results, "restored")
    assert results["restored"][0] == "ok", results["restored"]
    assert results["restored"][1][1] == ["p1"]
    assert results["restored"][1][0] is not results["first"][1]


# ---------------------------------------------------------------------------
# H2：授权白名单 / 沙盒放行按会话分桶
# ---------------------------------------------------------------------------

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
    """会话 A 授权的命令前缀，会话 B 不继承。"""
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
    # 未授权的会话 B 命不中 A 的授权
    assert results["B"] == ("ok", False)

    # 会话 A 重新进入后自己的授权仍然有效
    run_in_session("grant-A", dir_a, _check_other, results, "A2")
    assert results["A2"] == ("ok", True)


def test_write_edit_grants_isolated_between_sessions(tmp_path):
    """文件写入/编辑授权同样按会话分桶。"""
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
    """UI 切换只改变路由，不清理绑定线程或可见任务的授权。"""
    import core.llm as llm

    workdir = tmp_path / "proj"
    workdir.mkdir()
    results = {}

    def _grant_and_park():
        llm._grant_command_add(("npm", "run"))
        # 模拟 UI 线程在回合运行中切换全局会话：未绑定线程视角下
        # 全局会话从 sess-running 变为 sess-other
        app_context._current_session_fallback = "sess-other"
        llm.check_session_reset()
        return llm._grant_command_hit("npm run build")

    # 回合线程绑定 sess-running 时授权不被切换收回
    run_in_session("sess-running", workdir, _grant_and_park, results, "bound")
    assert results["bound"] == ("ok", True)

    # 未绑定线程也按当前任务 ID 分桶，切换后隔离，切回来仍保留。
    app_context.set_current_session("cli-sess-1")
    llm._grant_command_add(("ls",))
    assert llm._grant_command_hit("ls") is True
    app_context.set_current_session("cli-sess-2")
    llm.check_session_reset()
    assert llm._grant_command_hit("ls") is False
    app_context.set_current_session("cli-sess-1")
    assert llm._grant_command_hit("ls") is True


def test_sandbox_bypass_isolated_between_sessions(tmp_path):
    """会话级沙盒放行按会话分桶，A 放行不影响 B。"""
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

    # B 自行关闭后 A 的放行不受影响
    def _disable_b():
        tools_module.set_session_sandbox_bypass(False)
        return tools_module.is_session_sandbox_bypass_enabled()

    run_in_session("sandbox-B", dir_b, _disable_b, results, "B2")
    run_in_session("sandbox-A", dir_a, _check_other, results, "A2")
    assert results["B2"] == ("ok", False)
    assert results["A2"] == ("ok", True)


# ---------------------------------------------------------------------------
# 并发压力：多会话同时操作，零串扰
# ---------------------------------------------------------------------------

def test_many_sessions_parallel_zero_crosstalk(tmp_path):
    """8 个会话（两两共享目录）并发写计划 + 授权 + 沙盒放行，结果零串扰。"""
    import core.llm as llm
    from core import tools as tools_module

    session_count = 8
    dirs = []
    for i in range(session_count):
        # 两两共享同一目录：目录隔离之外，session_id 隔离也必须成立
        workdir = tmp_path / f"proj{i // 2}"
        workdir.mkdir(exist_ok=True)
        dirs.append(workdir)

    barrier = threading.Barrier(session_count)
    results = {}

    def _make_worker(index):
        sid = f"stress-{index}"

        def _fn():
            # 计划写入
            manager = plan_module.get_default_plan_manager()
            manager.replace([
                {"id": f"item-{index}", "subject": f"任务{index}", "status": "pending"},
            ])
            # 授权（二元组规则=子命令前缀规则，可匹配带参数的命令）
            llm._grant_command_add((f"tool{index}", "run"))
            # 沙盒放行（偶数会话开启）
            tools_module.set_session_sandbox_bypass(index % 2 == 0)
            barrier.wait(timeout=30)
            # 并发窗口后校验，全部只应看到自己的状态
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


# ---------------------------------------------------------------------------
# M 组：项目级目录路由跟随线程工作目录
# ---------------------------------------------------------------------------

def test_subagent_dirs_follow_thread_workdir(tmp_path):
    """项目级 .ycode/agents 扫描跟随线程工作目录，而非进程 cwd。"""
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
    """skills 相对路径（配置项 / 沙盒白名单）按线程工作目录解析。"""
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
    """会话 B 后启动覆盖全局 tuple 后，会话 A 的子代理仍必须继承 A。"""
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
    """显式会话解析为空时必须失败，不能把最后写入者 B 当成 A。"""
    from core import tools as tools_module

    app_context.set_active_model_tuple("openai/model-B", "key-B", "url-B")
    monkeypatch.setattr(
        "core.model_manager.get_secondary_roles", lambda: {"subagent": False}
    )
    monkeypatch.setattr("core.model_manager.get_active_model", lambda _session=None: None)

    assert tools_module._snapshot_model_config(session_name="session-A") is None


def test_subagent_manager_run_snapshots_parent_session_model(monkeypatch):
    """验证实际 run 入口把 parent_session 对应模型冻结到任务。"""
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
    """自定义子代理 inherit 的真实模型请求必须使用父会话，而非任务 fallback。"""
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
    """未绑定线程（CLI 主流程 / headless）走进程级默认单例，行为不回归。"""
    import core.llm as llm
    from core import tools as tools_module

    default_plan = plan_module.get_default_plan_manager()
    assert default_plan is plan_module._DEFAULT_PLAN_MANAGER
    assert app_context.get_thread_session() is None

    # 未绑定线程授权写入模块级默认集合（单元素规则=精确匹配裸命令）
    llm._grant_command_add(("echo",))
    assert llm._grant_command_hit("echo") is True
    assert ("echo",) in llm.session_allowed_commands

    # 未绑定线程沙盒放行写进程级全局标志
    tools_module.set_session_sandbox_bypass(True)
    assert tools_module.is_session_sandbox_bypass_enabled() is True
    tools_module.set_session_sandbox_bypass(False)


def test_takeover_queue_is_isolated_between_session_schedulers(tmp_path):
    """接管租约按会话调度器隔离：同名计划项/同 turn 查询不得跨会话命中。

    接管状态只允许存放在会话自己的 PlanManager/TaskScheduler 实例内；
    任何进程级全局 active-id 都会造成跨会话串扰。
    """
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

    # 各会话的 active 接管项只归属自己的 turn
    assert scheduler_a.active_takeover("turn-a")["id"] == "item"
    assert scheduler_b.active_takeover("turn-b")["id"] == "item"
    assert scheduler_a.active_takeover("turn-b") is None
    assert scheduler_b.active_takeover("turn-a") is None

    # 会话 A 完成并清空自己的队列后，不得跨会话认领会话 B 的排队项
    assert scheduler_a.complete("item", "done") is True
    assert scheduler_a.promote_next_takeover("turn-a")["id"] == "item-a2"
    assert scheduler_a.complete("item-a2", "done") is True
    assert scheduler_a.promote_next_takeover("turn-a") is None

    # 会话 B 的队列不受会话 A 影响
    assert scheduler_b.complete("item", "done") is True
    promoted_b = scheduler_b.promote_next_takeover("turn-b")
    assert promoted_b is not None
    assert promoted_b["id"] == "item-b2"
