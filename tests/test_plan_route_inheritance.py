"""tests/test_plan_route_inheritance.py —— A6-02 回归：并行工具线程继承计划会话路由。

背景（系统审查报告 A6-02）：同批并发派发的 run_subagent（parallel_safe=True）
经 core.harness.tool_pipeline._submit_daemon_call 派发到工作线程执行时，工作
线程未继承提交线程的计划会话路由——工作线程内 get_bound_plan_session() 为
None，工具层回退进程级单例，误判「计划项不存在」而整批被拒。

本文件以最小探针移植复现脚本（tmp/a6_02_repro/repro_a6_02.py）的核心断言：
提交线程绑定计划会话后派发，工作线程内必须读到同一会话路由（storage_dir 一
并继承）。计划存储目录统一用 pytest tmp_path，规避进程外临时根目录里第三方
inspect.py 对标准库的遮蔽（该目录绝不进入 sys.path）。
"""
from __future__ import annotations

from core.harness.tool_pipeline import _submit_daemon_call
from core.plan import (
    get_bound_plan_session,
    get_plan_route,
    set_current_plan_session,
)


def test_submit_daemon_call_inherits_plan_session_route(tmp_path):
    """核心断言：工作线程内 get_bound_plan_session() 必须继承提交线程的会话。"""
    session = "a6-02-route-inherit"
    storage_dir = str(tmp_path / "plans")
    previous = get_plan_route()
    set_current_plan_session(session, storage_dir=storage_dir)
    try:
        def probe():
            return get_bound_plan_session(), get_plan_route()

        worker_session, worker_route = _submit_daemon_call(probe).result(timeout=30)
        assert worker_session == session
        # storage_dir 必须一并继承：缺省会让会话管理器按工作线程目录重新推导
        # 落盘位置，指向与回合线程不同的目录（修复前的次生问题）。
        assert worker_route == (session, storage_dir)
    finally:
        set_current_plan_session(previous[0], storage_dir=previous[1])


def test_batch_concurrent_dispatch_all_inherit_plan_route(tmp_path):
    """同批多笔并发派发（等同 run_subagent 并行批次）必须全部继承同一路由。"""
    session = "a6-02-route-batch"
    storage_dir = str(tmp_path / "plans")
    previous = get_plan_route()
    set_current_plan_session(session, storage_dir=storage_dir)
    try:
        futures = [
            _submit_daemon_call(get_bound_plan_session) for _ in range(3)
        ]
        assert [f.result(timeout=30) for f in futures] == [session] * 3
    finally:
        set_current_plan_session(previous[0], storage_dir=previous[1])


def test_worker_stays_unbound_when_submitter_unbound():
    """提交线程未绑定计划会话时，工作线程保持未绑定语义（不得伪造绑定）。"""
    previous = get_plan_route()
    set_current_plan_session(None)
    try:
        result = _submit_daemon_call(get_bound_plan_session).result(timeout=30)
        assert result is None
    finally:
        set_current_plan_session(previous[0], storage_dir=previous[1])