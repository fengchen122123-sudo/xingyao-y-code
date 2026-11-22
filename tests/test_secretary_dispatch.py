import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.app_context as app_context
import core.secretary as secretary
from core import secretary_dispatch
from core import tools
from core.memory import MemoryManager
from core.secretary_dispatches import STATUS_DONE, STATUS_FAILED, DispatchStore
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


def test_register_unregister_and_get(manager):
    assert secretary_dispatch.get() is None

    _make_bridge(manager)
    service = secretary_dispatch.get()
    assert service is not None

    secretary_dispatch.unregister()
    assert secretary_dispatch.get() is None
    secretary_dispatch.unregister()
    assert secretary_dispatch.get() is None


def test_protocol_class_is_declaration_only():
    proto = secretary_dispatch.SecretaryDispatchService()
    with pytest.raises(NotImplementedError):
        proto.search_conversations(keyword="x")
    with pytest.raises(NotImplementedError):
        proto.dispatch_task(target="x", prompt="y")
    with pytest.raises(NotImplementedError):
        proto.open_session(target="x")


def test_search_returns_only_hits_with_expected_fields(manager, tmp_path):
    manager.create_session_explicit("会话甲", workdir=str(tmp_path), source="GUI")
    manager.add_messages("会话甲", [("user", "帮我看看登录报错的问题"), ("assistant", "收到")])
    manager.create_session_explicit("会话乙", workdir="GLOBAL", source="GUI")
    manager.add_messages("会话乙", [("user", "今天天气不错")])

    _make_bridge(manager)
    results = secretary_dispatch.get().search_conversations(keyword="登录报错")

    assert [r["session_name"] for r in results] == ["会话甲"]
    row = results[0]
    for field in ("session_name", "display_name", "is_secretary", "workdir",
                  "updated_at", "message_count", "snippets"):
        assert field in row
    assert row["display_name"] == "会话甲"
    assert row["is_secretary"] is False
    assert row["message_count"] == 2
    assert row["updated_at"]
    assert row["snippets"] and row["snippets"][0]["role"] == "user"
    assert "登录报错" in row["snippets"][0]["excerpt"]


def test_search_secretary_session_uses_secretary_name(manager):
    manager.create_session_explicit(
        "s_sec", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    manager.add_messages("s_sec", [("user", "记住这个口令：蓝鲸")])
    secretary.invalidate_secretary_cache()

    _make_bridge(manager)
    results = secretary_dispatch.get().search_conversations(keyword="蓝鲸")

    assert [r["session_name"] for r in results] == ["s_sec"]
    assert results[0]["is_secretary"] is True
    assert results[0]["display_name"] == "星河"


def test_search_filters_by_workdir(manager, tmp_path):
    project = tmp_path / "proj-a"
    project.mkdir()
    manager.create_session_explicit("项目会话", workdir=str(project), source="GUI")
    manager.add_messages("项目会话", [("user", "关键词：星轨")])
    manager.create_session_explicit("自由会话", workdir="GLOBAL", source="GUI")
    manager.add_messages("自由会话", [("user", "关键词：星轨")])

    _make_bridge(manager)
    got = secretary_dispatch.get().search_conversations(keyword="星轨", workdir=str(project))
    assert [r["session_name"] for r in got] == ["项目会话"]


def test_search_rejects_empty_keyword_and_dates(manager):
    _make_bridge(manager)
    with pytest.raises(secretary_dispatch.SecretaryDispatchError):
        secretary_dispatch.get().search_conversations()


def test_search_no_hit_returns_empty_list(manager):
    manager.create_session_explicit("会话甲", workdir="GLOBAL", source="GUI")
    manager.add_messages("会话甲", [("user", "无关内容")])

    _make_bridge(manager)
    assert secretary_dispatch.get().search_conversations(keyword="绝对不存在的词") == []


def test_search_supports_date_only_query(manager):
    manager.create_session_explicit("会话甲", workdir="GLOBAL", source="GUI")
    manager.add_messages("会话甲", [("user", "内容")])

    _make_bridge(manager)
    service = secretary_dispatch.get()
    assert service.search_conversations(date_from="2999-01-01") == []
    assert [r["session_name"] for r in service.search_conversations(date_from="1970-01-01")] == ["会话甲"]
    with pytest.raises(secretary_dispatch.SecretaryDispatchError):
        service.search_conversations(date_from="2026/01/01")


def test_dispatch_existing_session_spawns_plain_user_turn(manager):
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, _ = _make_bridge(manager)

    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="把 README 补一版")

    assert res["ok"] is True
    assert res["target_session"] == "项目甲"
    assert res["display_name"] == "项目甲"
    assert res["state"] == "running"
    assert len(spawned) == 1
    args, kwargs = spawned[0]
    assert args == ("把 README 补一版", "项目甲")
    assert kwargs.get("pending_workdir", "") == ""
    assert kwargs["turn_source"] == "local"
    # 硬要求：等同用户亲手发消息，绝不带显示标记与内部回合标记
    assert "display_text" not in kwargs
    assert "internal_kind" not in kwargs
    assert kwargs["secretary_dispatch_context"]["display_name"] == "项目甲"
    assert kwargs["secretary_dispatch_context"]["notify_session"] == "s_secretary"


def test_dispatch_existing_session_returns_own_workdir(manager, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    manager.create_session_explicit("项目甲", workdir=str(project), source="GUI")
    bridge, spawned, _ = _make_bridge(manager)

    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="跑一下测试")

    assert res["workdir"] == str(project)
    assert spawned[0][1].get("pending_workdir", "") == ""


def test_dispatch_busy_target_enqueues_like_user_message(manager):
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, events = _make_bridge(manager, is_turn_active=True)

    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="跑一下测试")

    assert res["state"] == "queued"
    assert spawned == []
    entry = bridge._message_queues["项目甲"][0]
    assert entry["text"] == "跑一下测试"
    assert entry["turn_source"] == "local"
    assert not entry.get("display_text")
    assert entry["secretary_dispatch_notify_session"] == "s_secretary"
    assert [e[0] for e in events][-1] == "message_queue"


def test_dispatch_new_session_passes_pending_workdir(manager, tmp_path):
    project = tmp_path / "proj-new"
    project.mkdir()
    bridge, spawned, _ = _make_bridge(manager)

    res = secretary_dispatch.get().dispatch_task(
        target="新会话", prompt="初始化项目", workdir=str(project)
    )

    assert res["ok"] is True
    assert res["target_session"].startswith("session_")
    assert res["state"] == "running"
    assert res["workdir"] == str(project)
    args, kwargs = spawned[0]
    assert args == ("初始化项目", res["target_session"])
    assert kwargs["pending_workdir"] == str(project)
    assert "display_text" not in kwargs
    assert "internal_kind" not in kwargs


def test_dispatch_new_session_rejects_bad_workdir(manager, tmp_path):
    (tmp_path / "proj-like").mkdir()
    bridge, spawned, _ = _make_bridge(manager)

    with pytest.raises(secretary_dispatch.SecretaryDispatchError) as excinfo:
        secretary_dispatch.get().dispatch_task(
            target="", prompt="干点活", workdir=str(tmp_path / "proj-lik")
        )
    message = str(excinfo.value)
    assert "proj-lik" in message
    assert spawned == []

    a_file = tmp_path / "not-a-dir.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(secretary_dispatch.SecretaryDispatchError):
        secretary_dispatch.get().dispatch_task(target="", prompt="干点活", workdir=str(a_file))


def test_dispatch_rejects_empty_prompt_and_unknown_target(manager):
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, _ = _make_bridge(manager)
    service = secretary_dispatch.get()

    with pytest.raises(secretary_dispatch.SecretaryDispatchError):
        service.dispatch_task(target="项目甲", prompt="   ")
    with pytest.raises(secretary_dispatch.SecretaryDispatchError):
        service.dispatch_task(target="根本不存在的会话", prompt="干点活")
    assert spawned == []


def test_dispatch_ambiguous_target_returns_candidates(manager):
    manager.create_session_explicit("项目甲-前端", workdir="GLOBAL", source="GUI")
    manager.create_session_explicit("项目甲-后端", workdir="GLOBAL", source="GUI")
    bridge, spawned, _ = _make_bridge(manager)

    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="干点活")

    assert res["ok"] is False
    assert {c["session_name"] for c in res["candidates"]} == {"项目甲-前端", "项目甲-后端"}
    assert spawned == []


def test_open_session_switches_current_and_emits_event(manager):
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, _, events = _make_bridge(manager)
    switched = []
    bridge._set_current_session = lambda name: switched.append(name)

    res = secretary_dispatch.get().open_session("项目甲")

    assert res["ok"] is True
    assert switched == ["项目甲"]
    opened = [e for e in events if e[0] == "open_session"]
    assert len(opened) == 1
    payload = opened[0][1]
    assert payload["session_name"] == "项目甲"
    assert payload["display_name"] == "项目甲"
    assert payload["workdir"] == "GLOBAL"


def test_open_session_unarchives_then_switches(manager):
    manager.create_session_explicit("旧会话", workdir="GLOBAL", source="GUI")
    manager.add_messages("旧会话", [("user", "老内容")])
    assert manager.archive_session("旧会话") is True

    bridge, _, events = _make_bridge(manager)
    bridge.get_sessions = lambda: {}
    bridge.get_archived_sessions = lambda: {}
    switched = []
    bridge._set_current_session = lambda name: switched.append(name)

    res = secretary_dispatch.get().open_session("旧会话")

    assert res["ok"] is True
    assert manager.get_all_sessions(workdir=None, include_empty=True,
                                    archived_only=True) == []
    assert switched == ["旧会话"]
    assert [e[0] for e in events if e[0] == "open_session"] == ["open_session"]


def test_open_session_unknown_target_raises(manager):
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, _, events = _make_bridge(manager)

    with pytest.raises(secretary_dispatch.SecretaryDispatchError):
        secretary_dispatch.get().open_session("不存在的会话")
    assert [e for e in events if e[0] == "open_session"] == []


class _WorkerHarness:

    def __init__(self, bridge, manager, tmp_path, consume_return=False):
        self.bridge = bridge
        self.tmp_path = tmp_path
        self.consume_return = consume_return
        bridge._session_workdir = lambda s: str(tmp_path)
        bridge._sync_active_model_context = lambda *a, **k: None
        bridge._recalc_token_usage = lambda *a, **k: None
        bridge._emit_token_usage = lambda *a, **k: None
        bridge._maybe_auto_rename = lambda s: s
        bridge._subagent_progress_poller = lambda *a, **k: None
        bridge._emit_message_final = lambda *a, **k: None
        bridge._clear_session_last_error = lambda *a, **k: None
        bridge._emit_owned_error = lambda *a, **k: None
        bridge._consume_turn_events = self._fake_consume

    def _fake_consume(self, history, model_name, api_key, api_base, session_name, turn_id,
                      model_id=None, error_state=None, turn_source="local"):
        return self.consume_return

    def __enter__(self):
        import gui_bridge as gb
        from core.turn_undo import service as tu_service

        self._prev_manager = tu_service._default_manager
        self._prev_storage = tu_service._default_storage
        tu_service.configure_default_manager(
            self.bridge._memory, blob_root=self.tmp_path / "blobs"
        )
        self._orig_get_active_model = gb.get_active_model
        self.model = {
            "litellm_model": "openai/model-one",
            "api_key": "",
            "base_url": "",
            "context_window": 128000,
            "id": "m1",
        }
        gb.get_active_model = lambda session_name: self.model
        return self

    def __exit__(self, *exc):
        import gui_bridge as gb
        from core.turn_undo import service as tu_service

        gb.get_active_model = self._orig_get_active_model
        tu_service._default_manager = self._prev_manager
        tu_service._default_storage = self._prev_storage
        return False


def test_real_turn_persists_plain_user_message_and_completion_notice(manager, tmp_path):
    from core.agent_service import strip_context_suffix

    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    manager.add_messages("项目甲", [("user", "预热"), ("assistant", "好")])
    bridge, _, events = _make_bridge(manager)

    with _WorkerHarness(bridge, manager, tmp_path):
        bridge._run_turn_worker(
            "把 README 补一版", "项目甲",
            secretary_dispatch_context={"display_name": "项目甲", "notify_session": "s_secretary"},
        )

    history = manager.get_history("项目甲")
    user_msgs = [m for m in history if m.get("role") == "user"]
    assert strip_context_suffix(str(user_msgs[-1]["content"])).strip() == "把 README 补一版"
    notices = [e for e in events if e[0] == "notice"]
    assert any("任务已在「项目甲」会话执行完成" in e[1]["message"] for e in notices)
    assert all(e[1]["session_name"] == "s_secretary" for e in notices)


def test_drain_message_queue_forwards_secretary_dispatch_marker(manager, tmp_path):
    project = tmp_path / "proj-q"
    project.mkdir()
    bridge, spawned, _ = _make_bridge(manager, is_turn_active=True)

    res = secretary_dispatch.get().dispatch_task(
        target="新会话", prompt="排队跑一下", workdir=str(project)
    )
    assert res["state"] == "queued"

    bridge._is_turn_active = lambda s: False
    bridge._drain_message_queue(res["target_session"])

    assert len(spawned) == 1
    args, kwargs = spawned[0]
    assert args == ("排队跑一下", res["target_session"])
    assert kwargs["pending_workdir"] == str(project)
    assert kwargs["secretary_dispatch_context"]["notify_session"] == "s_secretary"
    assert kwargs.get("display_text", "") == ""
    assert kwargs.get("internal_kind", "") == ""


def test_real_turn_without_dispatch_context_emits_no_notice(manager, tmp_path):
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    manager.add_messages("项目甲", [("user", "预热")])
    bridge, _, events = _make_bridge(manager)

    with _WorkerHarness(bridge, manager, tmp_path):
        bridge._run_turn_worker("你好", "项目甲")

    assert [e for e in events if e[0] == "notice"] == []


def test_create_project_creates_nested_dir_and_registers_workspace(manager, tmp_path):
    _make_bridge(manager)
    target = tmp_path / "work" / "demo"

    res = secretary_dispatch.get().create_project(path=str(target))

    assert res["ok"] is True
    assert res["created"] is True
    assert res["display_name"] == "demo"
    assert os.path.isdir(target)
    assert str(target.resolve()) in [str(p) for p in manager.get_all_workspaces()]


def test_create_project_reuses_existing_dir(manager, tmp_path):
    _make_bridge(manager)
    target = tmp_path / "已有项目"
    target.mkdir()

    res = secretary_dispatch.get().create_project(path=str(target))

    assert res["ok"] is True
    assert res["created"] is False
    assert str(target.resolve()) in [str(p) for p in manager.get_all_workspaces()]


def test_create_project_rejects_relative_path(manager):
    _make_bridge(manager)

    with pytest.raises(secretary_dispatch.SecretaryDispatchError) as exc:
        secretary_dispatch.get().create_project(path="demo")

    assert "绝对路径" in str(exc.value)


def test_create_project_rejects_reserved_marker(manager):
    _make_bridge(manager)

    for marker in ("GLOBAL", "SECRETARY"):
        with pytest.raises(secretary_dispatch.SecretaryDispatchError) as exc:
            secretary_dispatch.get().create_project(path=marker)
        assert "保留分组标识" in str(exc.value)


def test_create_project_rejects_existing_file(manager, tmp_path):
    _make_bridge(manager)
    target = tmp_path / "占位.txt"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(secretary_dispatch.SecretaryDispatchError) as exc:
        secretary_dispatch.get().create_project(path=str(target))

    assert "同名文件已存在" in str(exc.value)
    assert target.read_text(encoding="utf-8") == "x"


_LONG_EXCERPT = "结论" * 250


def _seed_ledger(manager, **overrides):
    params = dict(
        secretary_session="s_secretary",
        target_session="proj_a",
        target_display="项目甲",
        workdir="D:/proj/a",
        title="补 README",
        prompt="把 README 补一版",
        status=STATUS_DONE,
    )
    params.update(overrides)
    return DispatchStore(manager).create(**params)


def test_ledger_tools_visible_in_secretary_schema_only(manager, monkeypatch):
    import core.llm as llm

    class _NoMcp:
        def get_schemas(self):
            return []

    monkeypatch.setattr(llm, "_get_mcp_manager", lambda: _NoMcp())

    secretary_names = {s["function"]["name"] for s in llm._secretary_tools_schema()}
    assert "list_dispatches" in secretary_names
    assert "get_dispatch_result" in secretary_names

    registry_names = {s["function"]["name"] for s in llm.tool_registry.api_schemas()}
    ordinary_names = {
        s["function"]["name"]
        for s in llm._ordinary_tools_schema(secretary.SECRETARY_ONLY_TOOLS)
    }
    assert "list_dispatches" not in ordinary_names
    assert "get_dispatch_result" not in ordinary_names
    assert ordinary_names == registry_names - set(secretary.SECRETARY_ONLY_TOOLS)


def test_ledger_tools_pass_secretary_gate(manager):
    manager.create_session_explicit(
        "s_sec", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    app_context.set_current_session("s_sec")
    secretary.invalidate_secretary_cache()

    assert secretary.is_secretary_session() is True
    for name in ("list_dispatches", "get_dispatch_result"):
        assert name in secretary.SECRETARY_ONLY_TOOLS
        assert name in secretary.SECRETARY_ALLOWED_TOOLS
        assert secretary.is_secretary_tool_blocked(name, "none") is False


def test_list_dispatches_filters_and_truncates_excerpt(manager):
    done_id = _seed_ledger(manager, status=STATUS_DONE)
    failed_id = _seed_ledger(
        manager,
        target_session="proj_b",
        target_display="项目乙",
        workdir="D:/proj/b",
        title="跑测试",
        prompt="跑一遍测试",
        status=STATUS_FAILED,
    )
    store = DispatchStore(manager)
    store.mark_status(done_id, STATUS_DONE, result_excerpt=_LONG_EXCERPT)
    store.mark_status(failed_id, STATUS_FAILED, error="命令退出码 1")
    _make_bridge(manager)

    all_rows = json.loads(tools.list_dispatches())
    assert all_rows["count"] == 2
    assert [row["id"] for row in all_rows["dispatches"]] == [failed_id, done_id]
    for field in ("id", "title", "target_display", "workdir", "status",
                  "started_at", "finished_at", "result_excerpt"):
        assert field in all_rows["dispatches"][0]

    by_status = json.loads(tools.list_dispatches(status=STATUS_FAILED))
    assert [row["id"] for row in by_status["dispatches"]] == [failed_id]

    by_workdir = json.loads(tools.list_dispatches(workdir="D:/proj/a"))
    assert [row["id"] for row in by_workdir["dispatches"]] == [done_id]
    excerpt = by_workdir["dispatches"][0]["result_excerpt"]
    assert len(excerpt) == 200
    assert excerpt == _LONG_EXCERPT[:200]

    assert [row["id"] for row in json.loads(tools.list_dispatches(target="项目乙"))["dispatches"]] == [failed_id]
    assert [row["id"] for row in json.loads(tools.list_dispatches(target="proj_a"))["dispatches"]] == [done_id]

    assert tools.list_dispatches(status="running") == "没有符合条件的派活记录"


def test_get_dispatch_result_returns_full_fields(manager):
    dispatch_id = _seed_ledger(
        manager,
        prompt="把 README 补一版并跑测试",
        depends_on=[7],
        batch_id="batch-1",
    )
    DispatchStore(manager).mark_status(
        dispatch_id, STATUS_DONE, result_excerpt=_LONG_EXCERPT
    )
    _make_bridge(manager)

    payload = json.loads(tools.get_dispatch_result(dispatch_id))
    assert payload["ok"] is True
    assert payload["id"] == dispatch_id
    assert payload["status"] == STATUS_DONE
    assert payload["prompt"] == "把 README 补一版并跑测试"
    assert payload["result_excerpt"] == _LONG_EXCERPT
    assert len(payload["result_excerpt"]) == 500
    assert payload["depends_on"] == [7]
    assert payload["batch_id"] == "batch-1"
    assert payload["target_display"] == "项目甲"
    assert payload["workdir"] == "D:/proj/a"
    assert payload["finished_at"]
    assert payload["error"] == ""

    brief = json.loads(tools.list_dispatches())["dispatches"][0]
    assert len(brief["result_excerpt"]) == 200


def test_get_dispatch_result_reports_missing_and_bad_id(manager):
    _make_bridge(manager)

    missing = tools.get_dispatch_result(999)
    assert "没有找到任务 #999" in missing

    assert "不能为空" in tools.get_dispatch_result()
    assert "不能为空" in tools.get_dispatch_result("")
    bad = tools.get_dispatch_result("abc")
    assert "需要是整数" in bad
    assert "abc" in bad
    secretary_dispatch.get()._store = None
    assert "派活台账不可用" in tools.list_dispatches()
    assert "派活台账不可用" in tools.get_dispatch_result(1)


def test_list_dispatches_clamps_limit(manager):
    for index in range(55):
        _seed_ledger(manager, title=f"任务{index}")
    _make_bridge(manager)

    capped = json.loads(tools.list_dispatches(limit=999))
    assert capped["count"] == 50
    assert capped["dispatches"][0]["title"] == "任务54"

    fallback = json.loads(tools.list_dispatches(limit="abc"))
    assert fallback["count"] == 20


def test_ledger_tools_report_unavailable_without_service(manager):
    assert "秘书调度能力当前不可用" in tools.list_dispatches()
    assert "秘书调度能力当前不可用" in tools.get_dispatch_result(1)


def test_dispatch_rejects_secretary_target(manager):
    manager.create_session_explicit(
        "s_secretary", workdir="GLOBAL", source="SECRETARY", secretary_name="星河"
    )
    manager.create_session_explicit("s_work", workdir="GLOBAL", source="GUI")
    secretary.invalidate_secretary_cache()

    _bridge, spawned, _events = _make_bridge(manager)
    service = secretary_dispatch.get()

    with pytest.raises(secretary_dispatch.SecretaryDispatchError) as excinfo:
        service.dispatch_task(target="s_secretary", prompt="跑一下测试")
    assert "秘书会话" in str(excinfo.value)
    assert spawned == []

    result = service.dispatch_task(target="s_work", prompt="跑一下测试")
    assert result["ok"] is True
    assert len(spawned) == 1
