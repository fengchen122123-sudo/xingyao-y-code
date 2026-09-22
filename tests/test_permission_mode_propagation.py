"""回归：会话级权限档位必须随回合/子代理/并行工具线程传播。

历史缺陷：`permission_mode_scope` 只在 GUI 回合线程绑定一处（gui_bridge），
子代理线程、并行工具守护线程都不继承线程本地档位，鉴权路径
（app_context.get_permission_mode）回退到全局默认，导致父会话 full +
全局 confirm/accept_edits 时仍反复弹审批。
"""
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
    """子代理线程执行工具期间，鉴权路径读到父回合的会话档位。"""
    # 父回合线程绑定 full，全局刻意设为 confirm
    app_context.set_permission_mode("confirm")
    app_context.set_thread_permission_mode("full")

    captured = {}

    def runner(_task):
        captured["mode"] = app_context.get_permission_mode()
        return "完成"

    manager = tools_module.SubagentManager(runner=runner)
    manager.run("检查档位", "parent-session", "turn-1", timeout_seconds=30)

    # 子代理线程内读到快照的 full，而非全局 confirm
    assert captured["mode"] == "full"


def test_subagent_thread_does_not_leak_mode_after_finish(monkeypatch):
    """子代理线程的档位作用域不污染全局与调用线程。"""
    app_context.set_permission_mode("confirm")
    app_context.set_thread_permission_mode("full")

    manager = tools_module.SubagentManager(runner=lambda _task: "完成")
    manager.run("检查档位", "parent-session", "turn-1", timeout_seconds=30)

    # 子代理线程只在自己的作用域内改档位：全局兜底值仍是 confirm，
    # 调用线程仍保持自己绑定的 full（scope 退出恢复的是子代理线程的绑定）
    assert app_context._permission_mode_fallback == "confirm"
    assert app_context.get_permission_mode() == "full"

    # 新建的未绑定线程读到全局 confirm，说明子代理未把档位写进全局
    seen = {}
    t = threading.Thread(target=lambda: seen.setdefault("mode", app_context.get_permission_mode()))
    t.start()
    t.join()
    assert seen["mode"] == "confirm"


def test_subagent_without_bound_parent_falls_back_to_global():
    """父回合未绑定档位时子代理回退全局（不回归单会话语义）。"""
    app_context.set_permission_mode("accept_edits")
    captured = {}

    def runner(_task):
        captured["mode"] = app_context.get_permission_mode()
        return "完成"

    manager = tools_module.SubagentManager(runner=runner)
    manager.run("检查档位", "parent-session", "turn-1", timeout_seconds=30)

    assert captured["mode"] == "accept_edits"


def test_parallel_tool_thread_inherits_permission_mode():
    """并行工具守护线程继承提交线程的权限档位。"""
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
    """线程已绑定时 set_permission_mode 同步更新绑定，避免语义割裂。"""
    app_context.set_thread_permission_mode("confirm")
    app_context.set_permission_mode("full")
    assert app_context.get_permission_mode() == "full"

    # 未绑定线程：set_permission_mode 只改全局
    app_context.set_thread_permission_mode(None)
    app_context.set_permission_mode("accept_edits")
    assert app_context.get_permission_mode() == "accept_edits"


def test_permission_mode_scope_restores_previous_binding():
    """scope 退出后恢复原绑定（支持嵌套）。"""
    app_context.set_thread_permission_mode("accept_edits")
    with app_context.permission_mode_scope("full"):
        assert app_context.get_permission_mode() == "full"
        with app_context.permission_mode_scope("plan"):
            assert app_context.get_permission_mode() == "plan"
        assert app_context.get_permission_mode() == "full"
    assert app_context.get_permission_mode() == "accept_edits"