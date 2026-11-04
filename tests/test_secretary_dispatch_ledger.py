"""秘书派活台账：派活落库 + 回合收尾回写 + notice 文案升级。

风格对齐 tests/test_secretary_dispatch.py：ApiBridge.__new__ 裸实例 + 桩函数，
但台账用真 DispatchStore（临时 sqlite 的 MemoryManager），不 mock 存储层——
要验的正是「派活写进库 → 收尾按终态改库 → notice 带上库里的结论」这条链路。
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.app_context as app_context
import core.secretary as secretary
from core import project_profile, secretary_dispatch
from core.memory import MemoryManager
from core.secretary_dispatches import DispatchStore
from gui_bridge import ApiBridge


@pytest.fixture(autouse=True)
def _clean_singleton():
    """每个用例前后清理模块级单例，避免用例互相污染。"""
    secretary_dispatch.unregister()
    yield
    secretary_dispatch.unregister()


@pytest.fixture()
def manager(tmp_path):
    """独立临时 DB 的 MemoryManager，并注册为 app_context 当前管理器。"""
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
    """构造仅含调度所需内部状态的裸桥实例，并注册调度服务句柄。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    # 秘书会话是派发的发起方：完成通知按它路由
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


def _only_row(manager):
    rows = _store(manager).list()
    assert len(rows) == 1, rows
    return rows[0]


def _secretary_notices(events):
    """只取派活完成通知（按发起会话 s_secretary 路由的那些）。"""
    return [
        e[1]["message"]
        for e in events
        if e[0] == "notice" and e[1].get("session_name") == "s_secretary"
    ]


class _WorkerHarness:
    """为 _run_turn_worker 桩掉模型入口与事件出口（照 test_secretary_dispatch）。"""

    def __init__(self, bridge, manager, tmp_path, *, consume_return=False,
                 terminal_error=""):
        self.bridge = bridge
        self.tmp_path = tmp_path
        self.consume_return = consume_return
        self.terminal_error = terminal_error
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

    def _fake_consume(self, history, model_name, api_key, api_base, session_name,
                      turn_id, model_id=None, error_state=None,
                      turn_source="local"):
        if self.terminal_error and error_state is not None:
            error_state["terminal_message"] = self.terminal_error
        return self.consume_return

    def __enter__(self):
        import gui_bridge as gb
        from core.turn_undo import service as tu_service

        self._prev_manager = tu_service._default_manager
        self._prev_storage = tu_service._default_storage
        try:
            tu_service.configure_default_manager(
                self.bridge._memory, blob_root=self.tmp_path / "blobs"
            )
        except Exception:
            # 环境里存在与本用例无关的启动校验失败（某插件工具声明 mutates_files
            # 但未接入撤销记录器）。manager/storage 在校验前已就绪，忽略即可。
            pass
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


# ---------------------------------------------------------------------------
# 派活落库：running / queued
# ---------------------------------------------------------------------------

def test_dispatch_new_session_writes_running_ledger(manager, tmp_path):
    """新会话派活：台账出现一条 running，字段与派活入参一致。"""
    project = tmp_path / "proj-ledger"
    project.mkdir()
    bridge, spawned, _ = _make_bridge(manager)
    prompt = "把 README 补一版，写清安装步骤与常见问题"

    res = secretary_dispatch.get().dispatch_task(
        target="新会话", prompt=prompt, workdir=str(project)
    )

    assert res["ok"] is True
    assert res["state"] == "running"
    dispatch_id = res["dispatch_id"]
    assert isinstance(dispatch_id, int)

    row = _only_row(manager)
    assert row["id"] == dispatch_id
    assert row["status"] == "running"
    assert row["secretary_session"] == "s_secretary"
    assert row["target_session"] == res["target_session"]
    assert row["target_display"] == "新会话"
    assert row["workdir"] == str(project)
    assert row["prompt"] == prompt
    assert row["title"] == prompt[:40]
    assert row["finished_at"] == ""
    assert row["result_excerpt"] == ""
    assert row["error"] == ""

    # 台账 id 必须随 dispatch_context 交给回合，收尾才能回写
    ctx = spawned[0][1]["secretary_dispatch_context"]
    assert ctx["dispatch_id"] == dispatch_id
    assert ctx["task_title"] == prompt[:40]
    assert res["task_title"] == prompt[:40]


def test_dispatch_explicit_task_title_wins(manager):
    """显式 task_title 优先于 prompt 截断。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, _ = _make_bridge(manager)

    res = secretary_dispatch.get().dispatch_task(
        target="项目甲", prompt="很长很长的一段任务正文" * 5, task_title="补 README"
    )

    assert res["task_title"] == "补 README"
    assert _only_row(manager)["title"] == "补 README"
    assert spawned[0][1]["secretary_dispatch_context"]["task_title"] == "补 README"


def test_dispatch_busy_target_writes_queued_ledger(manager):
    """目标会话忙：台账状态为 queued，排队条目带上台账 id 供补发回填。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, _ = _make_bridge(manager, is_turn_active=True)

    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="跑一下测试")

    assert res["state"] == "queued"
    assert spawned == []
    row = _only_row(manager)
    assert row["status"] == "queued"
    assert row["id"] == res["dispatch_id"]
    assert row["target_session"] == "项目甲"

    entry = bridge._message_queues["项目甲"][0]
    assert entry["secretary_dispatch_id"] == res["dispatch_id"]
    assert entry["secretary_dispatch_task_title"] == "跑一下测试"


def test_queued_dispatch_forwards_ledger_id_on_drain(manager):
    """排队条目补发时，dispatch_context 仍带同一个台账 id。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, _ = _make_bridge(manager, is_turn_active=True)
    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="排队跑一下")

    bridge._is_turn_active = lambda s: False
    bridge._drain_message_queue("项目甲")

    assert len(spawned) == 1
    ctx = spawned[0][1]["secretary_dispatch_context"]
    assert ctx["dispatch_id"] == res["dispatch_id"]
    assert ctx["task_title"] == "排队跑一下"
    assert ctx["notify_session"] == "s_secretary"


# ---------------------------------------------------------------------------
# 收尾回写：done / failed / cancelled
# ---------------------------------------------------------------------------

#: 单行、无首尾空白，长度 > 800（用于验截断）
LONG_ANSWER = "结论：" + "".join(f"第{i}步已完成。" for i in range(120))


def test_finish_done_writes_result_excerpt(manager, tmp_path):
    """done：result_excerpt 取目标会话最后一条 assistant 正文（截 800 字）。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, events = _make_bridge(manager)
    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="补 README")
    ctx = spawned[0][1]["secretary_dispatch_context"]

    manager.add_messages("项目甲", [("assistant", LONG_ANSWER)])
    with _WorkerHarness(bridge, manager, tmp_path):
        bridge._run_turn_worker("补 README", "项目甲", secretary_dispatch_context=ctx)

    row = _store(manager).get(res["dispatch_id"])
    assert row["status"] == "done"
    assert row["result_excerpt"] == LONG_ANSWER[:800]
    assert len(row["result_excerpt"]) == 800
    assert row["finished_at"]

    notices = _secretary_notices(events)
    assert notices == [
        f"「项目甲」已完成：{LONG_ANSWER[:60]}…（任务 #{res['dispatch_id']}）"
    ]


def test_finish_done_without_assistant_falls_back_to_legacy_notice(manager, tmp_path):
    """done 但目标会话没有 assistant 正文：摘要留空，notice 回落原文案 + 编号。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    manager.add_messages("项目甲", [("user", "先聊两句")])
    bridge, spawned, events = _make_bridge(manager)
    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="跑一下")

    with _WorkerHarness(bridge, manager, tmp_path):
        bridge._run_turn_worker(
            "跑一下", "项目甲",
            secretary_dispatch_context=spawned[0][1]["secretary_dispatch_context"],
        )

    row = _store(manager).get(res["dispatch_id"])
    assert row["status"] == "done"
    assert row["result_excerpt"] == ""
    assert _secretary_notices(events) == [
        f"任务已在「项目甲」会话执行完成（任务 #{res['dispatch_id']}）"
    ]


def test_finish_failed_writes_error(manager, tmp_path):
    """terminal_error 非空：台账 status=failed 且写入截断后的 error。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, events = _make_bridge(manager)
    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="跑迁移")
    error_text = "模型返回 500：上游不可用"

    with _WorkerHarness(bridge, manager, tmp_path, terminal_error=error_text):
        bridge._run_turn_worker(
            "跑迁移", "项目甲",
            secretary_dispatch_context=spawned[0][1]["secretary_dispatch_context"],
        )

    row = _store(manager).get(res["dispatch_id"])
    assert row["status"] == "failed"
    assert row["error"] == error_text
    assert row["result_excerpt"] == ""
    assert row["finished_at"]
    assert _secretary_notices(events) == [
        f"「项目甲」执行失败：{error_text}（任务 #{res['dispatch_id']}）"
    ]


def test_finish_cancelled_marks_ledger(manager, tmp_path):
    """用户取消：台账 status=cancelled，notice 用取消文案。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    manager.add_messages("项目甲", [("assistant", "干到一半")])
    bridge, spawned, events = _make_bridge(manager)
    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="跑迁移")

    with _WorkerHarness(bridge, manager, tmp_path, consume_return=True):
        bridge._run_turn_worker(
            "跑迁移", "项目甲",
            secretary_dispatch_context=spawned[0][1]["secretary_dispatch_context"],
        )

    row = _store(manager).get(res["dispatch_id"])
    assert row["status"] == "cancelled"
    assert row["finished_at"]
    assert _secretary_notices(events) == [f"「项目甲」已取消（任务 #{res['dispatch_id']}）"]


# ---------------------------------------------------------------------------
# 台账故障：派活照常成功、收尾照常通知
# ---------------------------------------------------------------------------

class _BrokenStore:
    """模拟台账不可用：任何读写都抛异常。"""

    def create(self, **kwargs):
        raise RuntimeError("台账库被锁死")

    def mark_status(self, *args, **kwargs):
        raise RuntimeError("台账库被锁死")


def test_dispatch_survives_missing_store(manager, tmp_path):
    """store 为 None：派活仍成功，无台账编号，notice 回落原文案（无编号）。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, events = _make_bridge(manager)
    service = secretary_dispatch.get()
    service._store = None

    res = service.dispatch_task(target="项目甲", prompt="照常派活")

    assert res["ok"] is True
    assert res["dispatch_id"] is None
    assert spawned[0][1]["secretary_dispatch_context"]["dispatch_id"] is None
    assert _store(manager).list() == []

    with _WorkerHarness(bridge, manager, tmp_path):
        bridge._run_turn_worker(
            "照常派活", "项目甲",
            secretary_dispatch_context=spawned[0][1]["secretary_dispatch_context"],
        )
    # 没有台账编号 → 不加「（任务 #N）」后缀，保持旧文案
    assert _secretary_notices(events) == ["任务已在「项目甲」会话执行完成"]


def test_dispatch_survives_store_create_error(manager):
    """store.create 抛异常：派活仍返回 ok=True，台账为空。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, _ = _make_bridge(manager)
    service = secretary_dispatch.get()
    service._store = _BrokenStore()

    res = service.dispatch_task(target="项目甲", prompt="照常派活")

    assert res["ok"] is True
    assert res["state"] == "running"
    assert res["dispatch_id"] is None
    assert _store(manager).list() == []
    assert spawned[0][1]["secretary_dispatch_context"]["dispatch_id"] is None


def test_finalize_survives_store_mark_status_error(manager, tmp_path):
    """回写抛异常：收尾不外抛，notice 仍按终态推送（编号照旧）。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, events = _make_bridge(manager)
    service = secretary_dispatch.get()
    res = service.dispatch_task(target="项目甲", prompt="跑一下")
    ctx = spawned[0][1]["secretary_dispatch_context"]
    service._store = _BrokenStore()

    with _WorkerHarness(bridge, manager, tmp_path):
        bridge._run_turn_worker("跑一下", "项目甲", secretary_dispatch_context=ctx)

    assert _secretary_notices(events) == [
        f"任务已在「项目甲」会话执行完成（任务 #{res['dispatch_id']}）"
    ]


# ---------------------------------------------------------------------------
# 设计 5.1：收尾追加项目「任务流水」
# ---------------------------------------------------------------------------

def test_finish_done_appends_project_task_log(manager, tmp_path, monkeypatch):
    """done 收尾必须往该项目画像的「任务流水」追加一行（设计 5.1）。"""
    # 画像路径按 HOME / YCODE_DATA_DIR 在调用时解析：重定向到 tmp，绝不碰真实 ~/.ycode
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / ".ycode"))

    project = tmp_path / "proj-task-log"
    project.mkdir()
    bridge, spawned, _events = _make_bridge(manager)
    res = secretary_dispatch.get().dispatch_task(
        target="新会话", prompt="把 README 补一版", workdir=str(project)
    )
    dispatch_id = res["dispatch_id"]
    ctx = spawned[0][1]["secretary_dispatch_context"]
    manager.add_messages(
        res["target_session"], [("assistant", "README 已补全，安装步骤写完")]
    )

    with _WorkerHarness(bridge, manager, tmp_path):
        bridge._run_turn_worker(
            "把 README 补一版", res["target_session"], secretary_dispatch_context=ctx
        )

    profile = project_profile.read_profile(str(project))
    assert f"#{dispatch_id}" in profile, profile
    assert "「把 README 补一版」" in profile, profile
    assert "done" in profile, profile
    assert "README 已补全" in profile, profile
    # 一次收尾只追加一条流水（不重复写）
    log_lines = [
        line
        for line in profile.splitlines()
        if line.startswith("- ") and f"#{dispatch_id}" in line
    ]
    assert len(log_lines) == 1, log_lines


def test_finish_skips_task_log_for_global_workdir(manager, tmp_path, monkeypatch):
    """workdir 为 GLOBAL（无项目归属）时不写流水：不产生画像文件。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / ".ycode"))

    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, _events = _make_bridge(manager)
    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="跑一下")
    ctx = spawned[0][1]["secretary_dispatch_context"]

    with _WorkerHarness(bridge, manager, tmp_path):
        bridge._run_turn_worker("跑一下", "项目甲", secretary_dispatch_context=ctx)

    # 台账确实落库且状态是 done
    assert _store(manager).get(res["dispatch_id"])["status"] == "done"
    # 但没有画像文件被创建（GLOBAL 无项目归属）
    projects_dir = tmp_path / ".ycode" / "memory" / "projects"
    assert list(projects_dir.glob("*.md")) == []
