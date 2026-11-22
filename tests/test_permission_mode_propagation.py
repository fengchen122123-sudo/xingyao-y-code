import threading

import pytest

from core import app_context
from core import tools as tools_module


@pytest.fixture(autouse=True)
def _reset_permission_state():
    app_context.set_thread_permission_mode(None)
    app_context.set_permission_mode("confirm")
    yield
    app_context.set_thread_permission_mode(None)
    app_context.set_permission_mode("confirm")


def test_subagent_thread_inherits_parent_permission_mode(monkeypatch):
    app_context.set_permission_mode("confirm")
    app_context.set_thread_permission_mode("full")

    captured = {}

    def runner(_task):
        captured["mode"] = app_context.get_permission_mode()
        return "完成"

    manager = tools_module.SubagentManager(runner=runner)
    manager.run("检查档位", "parent-session", "turn-1", timeout_seconds=30)

    assert captured["mode"] == "full"


def test_subagent_thread_does_not_leak_mode_after_finish(monkeypatch):
    app_context.set_permission_mode("confirm")
    app_context.set_thread_permission_mode("full")

    manager = tools_module.SubagentManager(runner=lambda _task: "完成")
    manager.run("检查档位", "parent-session", "turn-1", timeout_seconds=30)

    assert app_context._permission_mode_fallback == "confirm"
    assert app_context.get_permission_mode() == "full"

    seen = {}
    t = threading.Thread(target=lambda: seen.setdefault("mode", app_context.get_permission_mode()))
    t.start()
    t.join()
    assert seen["mode"] == "confirm"


def test_subagent_without_bound_parent_falls_back_to_global():
    app_context.set_permission_mode("accept_edits")
    captured = {}

    def runner(_task):
        captured["mode"] = app_context.get_permission_mode()
        return "完成"

    manager = tools_module.SubagentManager(runner=runner)
    manager.run("检查档位", "parent-session", "turn-1", timeout_seconds=30)

    assert captured["mode"] == "accept_edits"


def test_parallel_tool_thread_inherits_permission_mode():
    from core.harness import tool_pipeline

    app_context.set_permission_mode("confirm")
    app_context.set_thread_permission_mode("full")

    captured = {}

    def work():
        captured["mode"] = app_context.get_permission_mode()
        captured["thread"] = threading.current_thread().name
        return "ok"

    future = tool_pipeline._submit_daemon_call(work)
    assert future.result(timeout=2) == "ok"
    assert captured["mode"] == "full"
    assert captured["thread"] == "ycode-tool-call"


def test_set_permission_mode_updates_bound_thread_mode():
    app_context.set_thread_permission_mode("confirm")
    app_context.set_permission_mode("full")
    assert app_context.get_permission_mode() == "full"

    app_context.set_thread_permission_mode(None)
    app_context.set_permission_mode("accept_edits")
    assert app_context.get_permission_mode() == "accept_edits"


def test_permission_mode_scope_restores_previous_binding():
    app_context.set_thread_permission_mode("accept_edits")
    with app_context.permission_mode_scope("full"):
        assert app_context.get_permission_mode() == "full"
        with app_context.permission_mode_scope("plan"):
            assert app_context.get_permission_mode() == "plan"
        assert app_context.get_permission_mode() == "full"
    assert app_context.get_permission_mode() == "accept_edits"