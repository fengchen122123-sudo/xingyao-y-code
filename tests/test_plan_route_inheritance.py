from __future__ import annotations

from core.harness.tool_pipeline import _submit_daemon_call
from core.plan import (
    get_bound_plan_session,
    get_plan_route,
    set_current_plan_session,
)


def test_submit_daemon_call_inherits_plan_session_route(tmp_path):
    session = "a6-02-route-inherit"
    storage_dir = str(tmp_path / "plans")
    previous = get_plan_route()
    set_current_plan_session(session, storage_dir=storage_dir)
    try:
        def probe():
            return get_bound_plan_session(), get_plan_route()

        worker_session, worker_route = _submit_daemon_call(probe).result(timeout=30)
        assert worker_session == session
        assert worker_route == (session, storage_dir)
    finally:
        set_current_plan_session(previous[0], storage_dir=previous[1])


def test_batch_concurrent_dispatch_all_inherit_plan_route(tmp_path):
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
    previous = get_plan_route()
    set_current_plan_session(None)
    try:
        result = _submit_daemon_call(get_bound_plan_session).result(timeout=30)
        assert result is None
    finally:
        set_current_plan_session(previous[0], storage_dir=previous[1])