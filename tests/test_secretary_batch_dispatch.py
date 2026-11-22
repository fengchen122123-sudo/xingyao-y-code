import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.app_context as app_context
import core.secretary as secretary
from core import secretary_dispatch
from core.memory import MemoryManager
from core.secretary_dispatch import SecretaryDispatchError
from core.secretary_dispatches import DispatchStore
from gui_bridge import ApiBridge


@pytest.fixture(autouse=True)
def _clean_singleton():
    secretary_dispatch.unregister()
    yield
    secretary_dispatch.unregister()


@pytest.fixture()
def manager(tmp_path):
    m = MemoryManager(str(tmp_path / "ycode.db"))
    prev_ctx = app_context.get()
    prev_manager = app_context.get_manager()
    prev_session = app_context.get_current_session()
    app_context.register(None)
    app_context.set_manager(m)
    app_context.set_current_session("")
    secretary.invalidate_secretary_cache()
    try:
        yield m
    finally:
        secretary.invalidate_secretary_cache()
        app_context.set_current_session(prev_session)
        app_context.set_manager(prev_manager)
        app_context.register(prev_ctx)


def _make_bridge(manager, *, is_turn_active=False):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._current_session = "s_secretary"
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._cancel_events = {}
    bridge._is_turn_active = (lambda s: True) if is_turn_active else (lambda s: False)
    bridge._memory = manager
    bridge._active_turn_ids = {}
    bridge._active_turn_lock = threading.Lock()
    bridge._active_turn_snapshots = {}
    bridge._turn_owner_lock = threading.Lock()
    bridge._turn_owner_sessions = {}
    bridge._bg_summary_lock = threading.Lock()
    bridge._bg_summary_triggered = set()
    bridge._bg_summary_pending_sessions = set()
    bridge._user_cancelled_sessions = set()
    bridge._task_status = "idle"
    bridge._undo_turn_impact_for_event = lambda request: None
    bridge._get_cancel_event = lambda s: threading.Event()
    events = []
    bridge.emit_event = lambda etype, payload: events.append((etype, payload))
    spawned = []

    def _spawn(*args, **kwargs):
        spawned.append((args, kwargs))

    bridge._spawn_turn_worker = _spawn
    bridge._register_secretary_dispatch_service()
    return bridge, spawned, events


def _store(manager):
    return DispatchStore(manager)


def _service():
    return secretary_dispatch.get()


def _two_sessions(manager):
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    manager.create_session_explicit("项目乙", workdir="GLOBAL", source="GUI")


def _wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _spawned_sessions(spawned):
    return [str(args[1]) for args, _kwargs in spawned]


def test_batch_two_independent_tasks_both_dispatched(manager):
    _two_sessions(manager)
    bridge, spawned, _events = _make_bridge(manager)

    res = _service().dispatch_batch(tasks=[
        {"title": "甲活", "prompt": "甲的正文", "target": "项目甲"},
        {"title": "乙活", "prompt": "乙的正文", "target": "项目乙"},
    ])

    assert res["ok"] is True
    assert res["batch_id"].startswith("batch_")
    assert [item["index"] for item in res["items"]] == [0, 1]
    assert [item["state"] for item in res["items"]] == ["running", "running"]

    store = _store(manager)
    assert len(store.list()) == 2
    for item in res["items"]:
        row = store.get(item["dispatch_id"])
        assert row["status"] == "running"
        assert row["batch_id"] == res["batch_id"]
        assert row["secretary_session"] == "s_secretary"
        assert row["depends_on"] == []
        assert row["finished_at"] == ""
    assert store.get(res["items"][0]["dispatch_id"])["title"] == "甲活"
    assert store.get(res["items"][0]["dispatch_id"])["prompt"] == "甲的正文"

    assert _spawned_sessions(spawned) == ["项目甲", "项目乙"]
    contexts = [kwargs["secretary_dispatch_context"] for _args, kwargs in spawned]
    assert [ctx["dispatch_id"] for ctx in contexts] == [
        item["dispatch_id"] for item in res["items"]
    ]
    assert [ctx["notify_session"] for ctx in contexts] == ["s_secretary", "s_secretary"]


def test_batch_dependent_task_blocked_until_upstreams(manager):
    _two_sessions(manager)
    bridge, spawned, _events = _make_bridge(manager)

    res = _service().dispatch_batch(tasks=[
        {"title": "一", "prompt": "第一件事", "target": "项目甲"},
        {"title": "二", "prompt": "第二件事", "target": "项目乙"},
        {
            "title": "三",
            "prompt": "汇总前两件",
            "target": "项目甲",
            "depends_on": [0, 1],
        },
    ])

    assert [item["state"] for item in res["items"]] == ["running", "running", "blocked"]
    first_id = res["items"][0]["dispatch_id"]
    second_id = res["items"][1]["dispatch_id"]
    third_id = res["items"][2]["dispatch_id"]

    store = _store(manager)
    assert store.get(third_id)["status"] == "blocked"
    assert store.get(third_id)["depends_on"] == [first_id, second_id]
    assert store.get(third_id)["finished_at"] == ""
    assert store.get(first_id)["status"] == "running"
    assert store.get(second_id)["status"] == "running"
    assert _spawned_sessions(spawned) == ["项目甲", "项目乙"]


def test_batch_depends_on_out_of_range_rejected(manager):
    _two_sessions(manager)
    bridge, spawned, _events = _make_bridge(manager)

    with pytest.raises(SecretaryDispatchError) as excinfo:
        _service().dispatch_batch(tasks=[
            {"prompt": "第一条", "target": "项目甲"},
            {"prompt": "第二条", "target": "项目乙", "depends_on": [5]},
        ])

    assert "越界" in str(excinfo.value)
    assert _store(manager).list() == []
    assert spawned == []


def test_batch_cycle_rejected(manager):
    _two_sessions(manager)
    bridge, spawned, _events = _make_bridge(manager)

    with pytest.raises(SecretaryDispatchError) as excinfo:
        _service().dispatch_batch(tasks=[
            {"prompt": "第一条", "target": "项目甲", "depends_on": [1]},
            {"prompt": "第二条", "target": "项目乙", "depends_on": [0]},
        ])

    message = str(excinfo.value)
    assert "环" in message or "循环" in message
    assert "#1" in message and "#2" in message
    assert _store(manager).list() == []
    assert spawned == []


def test_batch_over_eight_tasks_rejected(manager):
    _two_sessions(manager)
    bridge, spawned, _events = _make_bridge(manager)

    tasks = [{"prompt": f"第{i}件", "target": "项目甲"} for i in range(9)]
    with pytest.raises(SecretaryDispatchError) as excinfo:
        _service().dispatch_batch(tasks=tasks)

    assert "8" in str(excinfo.value)
    assert _store(manager).list() == []
    assert spawned == []


def test_advance_dispatches_downstream_when_upstream_done(manager):
    _two_sessions(manager)
    bridge, spawned, events = _make_bridge(manager)
    res = _service().dispatch_batch(tasks=[
        {"prompt": "上游任务", "target": "项目甲"},
        {"prompt": "下游任务", "target": "项目乙", "depends_on": [0]},
    ])
    upstream_id = res["items"][0]["dispatch_id"]
    downstream_id = res["items"][1]["dispatch_id"]
    assert _store(manager).get(downstream_id)["status"] == "blocked"
    before = len(spawned)

    _store(manager).mark_status(upstream_id, "done", result_excerpt="上游做完了")
    bridge._advance_dependents(upstream_id)

    assert _wait_until(lambda: len(spawned) == before + 1), spawned
    assert _wait_until(
        lambda: _store(manager).get(downstream_id)["status"] == "running"
    ), _store(manager).get(downstream_id)

    args, kwargs = spawned[-1]
    assert args[1] == "项目乙"
    assert args[0] == "下游任务"
    assert kwargs["secretary_dispatch_context"]["dispatch_id"] == downstream_id
    notices = [
        payload["message"]
        for etype, payload in events
        if etype == "notice" and payload.get("session_name") == "s_secretary"
    ]
    assert any(f"任务 #{downstream_id}" in text for text in notices)
    assert any("自动启动" in text for text in notices)


def test_advance_blocks_downstream_chain_when_upstream_failed(manager):
    _two_sessions(manager)
    bridge, spawned, _events = _make_bridge(manager)
    res = _service().dispatch_batch(tasks=[
        {"prompt": "第一层", "target": "项目甲"},
        {"prompt": "第二层", "target": "项目乙", "depends_on": [0]},
        {"prompt": "第三层", "target": "项目甲", "depends_on": [1]},
    ])
    first_id = res["items"][0]["dispatch_id"]
    second_id = res["items"][1]["dispatch_id"]
    third_id = res["items"][2]["dispatch_id"]
    before = len(spawned)

    _store(manager).mark_status(first_id, "failed", error="模型返回 500")
    bridge._advance_dependents(first_id)

    store = _store(manager)
    assert _wait_until(lambda: f"#{first_id}" in store.get(second_id)["error"]), store.get(second_id)
    assert _wait_until(lambda: f"#{first_id}" in store.get(third_id)["error"]), store.get(third_id)
    assert store.get(second_id)["status"] == "blocked"
    assert store.get(third_id)["status"] == "blocked"
    assert "失败" in store.get(second_id)["error"]
    assert f"#{second_id}" in store.get(third_id)["error"]
    assert store.get(second_id)["finished_at"]
    assert store.get(third_id)["finished_at"]
    assert len(spawned) == before


def test_advance_keeps_downstream_blocked_with_partial_dependencies(manager):
    _two_sessions(manager)
    bridge, spawned, _events = _make_bridge(manager)
    res = _service().dispatch_batch(tasks=[
        {"prompt": "上游一", "target": "项目甲"},
        {"prompt": "上游二", "target": "项目乙"},
        {"prompt": "下游", "target": "项目甲", "depends_on": [0, 1]},
    ])
    first_id = res["items"][0]["dispatch_id"]
    downstream_id = res["items"][2]["dispatch_id"]
    before = len(spawned)

    _store(manager).mark_status(first_id, "done", result_excerpt="一半好了")
    bridge._advance_dependents(first_id)

    time.sleep(0.3)
    assert _store(manager).get(downstream_id)["status"] == "blocked"
    assert len(spawned) == before


def test_dispatch_batch_is_secretary_only_tool():
    assert "dispatch_batch" in secretary.SECRETARY_ONLY_TOOLS
    assert secretary.secretary_allows_tool("dispatch_batch", "system") is True


def test_tool_layer_returns_json_and_readable_errors(manager):
    _two_sessions(manager)
    bridge, spawned, _events = _make_bridge(manager)
    from core import tools

    ok_text = tools.dispatch_batch(tasks=[{"prompt": "经工具层派一条", "target": "项目甲"}])
    payload = json.loads(ok_text)
    assert payload["ok"] is True
    assert payload["batch_id"].startswith("batch_")
    assert payload["items"][0]["state"] == "running"

    bad_text = tools.dispatch_batch(tasks=[
        {"prompt": "一", "target": "项目甲", "depends_on": [1]},
        {"prompt": "二", "target": "项目乙", "depends_on": [0]},
    ])
    assert bad_text.startswith("🚨 批量派活失败：")
    assert "环" in bad_text


def test_batch_busy_target_goes_queued_with_ledger_id(manager):
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, _events = _make_bridge(manager, is_turn_active=True)

    res = _service().dispatch_batch(tasks=[
        {"prompt": "排队任务", "target": "项目甲"},
    ])

    dispatch_id = res["items"][0]["dispatch_id"]
    assert res["items"][0]["state"] == "queued"
    assert _store(manager).get(dispatch_id)["status"] == "queued"
    assert spawned == []
    entry = bridge._message_queues["项目甲"][0]
    assert entry["secretary_dispatch_id"] == dispatch_id
    assert entry["secretary_dispatch_task_title"] == "排队任务"
