"""秘书调度台：调度服务骨架 + 检索 / 派活 / 跳转后端实现的单元测试。

风格对齐 tests/test_gui_automation_dispatch.py：ApiBridge.__new__ 裸实例 + 桩函数，
重点验证「派活等价于用户在目标会话亲手发一条消息」这一语义（spawn 入参里绝不出现
display_text / internal_kind），避免在单测里真起回合线程。
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.app_context as app_context
import core.secretary as secretary
from core import secretary_dispatch
from core.memory import MemoryManager
from gui_bridge import ApiBridge


@pytest.fixture(autouse=True)
def _clean_singleton():
    """每个用例前后清理模块级单例，避免用例互相污染。"""
    secretary_dispatch.unregister()
    yield
    secretary_dispatch.unregister()


@pytest.fixture()
def manager(tmp_path):
    """独立临时 DB 的 MemoryManager，并注册为 app_context 当前管理器。

    同 test_secretary_identity：`import main` 注册的 CLI ctx 以只读视图
    影子化 set_manager fallback，需先注销 ctx 才能让本管理器生效。
    """
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
    # 真回合（_run_turn_worker）用例会触达的身份/快照/汇总结构
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


# ---------------------------------------------------------------------------
# 服务骨架：注册 / 注销 / 未注册
# ---------------------------------------------------------------------------

def test_register_unregister_and_get(manager):
    """未注册 → get() 为 None；注册后可取回；注销后回到 None 且幂等。"""
    assert secretary_dispatch.get() is None

    _make_bridge(manager)
    service = secretary_dispatch.get()
    assert service is not None

    secretary_dispatch.unregister()
    assert secretary_dispatch.get() is None
    # 未注册时再次注销不抛异常
    secretary_dispatch.unregister()
    assert secretary_dispatch.get() is None


def test_protocol_class_is_declaration_only():
    """协议类只作文档说明：直接实例化调用必须抛 NotImplementedError。"""
    proto = secretary_dispatch.SecretaryDispatchService()
    with pytest.raises(NotImplementedError):
        proto.search_conversations(keyword="x")
    with pytest.raises(NotImplementedError):
        proto.dispatch_task(target="x", prompt="y")
    with pytest.raises(NotImplementedError):
        proto.open_session(target="x")


# ---------------------------------------------------------------------------
# 能力一：会话检索 search_conversations
# ---------------------------------------------------------------------------

def test_search_returns_only_hits_with_expected_fields(manager, tmp_path):
    """构造一个含关键词的会话与一个不含的：只返回命中项，字段齐全。"""
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
    """秘书会话的 display_name 走 secretary.get_secretary_name，并标记 is_secretary。"""
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
    """workdir 过滤：只返回该项目目录下的会话。"""
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
    """关键词为空且无日期 → 拒绝（避免把全部会话倾倒出来）。"""
    _make_bridge(manager)
    with pytest.raises(secretary_dispatch.SecretaryDispatchError):
        secretary_dispatch.get().search_conversations()


def test_search_no_hit_returns_empty_list(manager):
    """无命中返回空列表（不抛错，由调用方给友好提示）。"""
    manager.create_session_explicit("会话甲", workdir="GLOBAL", source="GUI")
    manager.add_messages("会话甲", [("user", "无关内容")])

    _make_bridge(manager)
    assert secretary_dispatch.get().search_conversations(keyword="绝对不存在的词") == []


def test_search_supports_date_only_query(manager):
    """只给日期（无关键词）也要能检索：按会话 updated_at 过滤。"""
    manager.create_session_explicit("会话甲", workdir="GLOBAL", source="GUI")
    manager.add_messages("会话甲", [("user", "内容")])

    _make_bridge(manager)
    service = secretary_dispatch.get()
    # 未来区间必然为空
    assert service.search_conversations(date_from="2999-01-01") == []
    # 覆盖全部历史：能拿到该会话
    assert [r["session_name"] for r in service.search_conversations(date_from="1970-01-01")] == ["会话甲"]
    # 非法日期格式直接拒绝
    with pytest.raises(secretary_dispatch.SecretaryDispatchError):
        service.search_conversations(date_from="2026/01/01")


# ---------------------------------------------------------------------------
# 能力二：派活 dispatch_task
# ---------------------------------------------------------------------------

def test_dispatch_existing_session_spawns_plain_user_turn(manager):
    """已有会话：spawn 入参与「用户亲手发消息」逐项一致，无 display_text/internal_kind。"""
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
    # 完成通知标记：按发起会话（秘书会话）路由
    assert kwargs["secretary_dispatch_context"]["display_name"] == "项目甲"
    assert kwargs["secretary_dispatch_context"]["notify_session"] == "s_secretary"


def test_dispatch_existing_session_returns_own_workdir(manager, tmp_path):
    """已有会话沿用自身 workdir / 权限档位，派活不改写会话归属。"""
    project = tmp_path / "proj"
    project.mkdir()
    manager.create_session_explicit("项目甲", workdir=str(project), source="GUI")
    bridge, spawned, _ = _make_bridge(manager)

    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="跑一下测试")

    assert res["workdir"] == str(project)
    assert spawned[0][1].get("pending_workdir", "") == ""


def test_dispatch_busy_target_enqueues_like_user_message(manager):
    """目标会话忙 → 走既有 _message_queues 入队（与自动化同一口径）。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, events = _make_bridge(manager, is_turn_active=True)

    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="跑一下测试")

    assert res["state"] == "queued"
    assert spawned == []
    entry = bridge._message_queues["项目甲"][0]
    assert entry["text"] == "跑一下测试"
    assert entry["turn_source"] == "local"
    # 排队条目同样不得带显示标记：补发后仍是普通用户消息
    assert not entry.get("display_text")
    assert entry["secretary_dispatch_notify_session"] == "s_secretary"
    assert [e[0] for e in events][-1] == "message_queue"


def test_dispatch_new_session_passes_pending_workdir(manager, tmp_path):
    """新建会话：workdir 透传为 pending_workdir（懒创建语义）。"""
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
    """workdir 不存在（或不是目录）→ 拒绝，并给出相近目录候选。"""
    (tmp_path / "proj-like").mkdir()
    bridge, spawned, _ = _make_bridge(manager)

    with pytest.raises(secretary_dispatch.SecretaryDispatchError) as excinfo:
        secretary_dispatch.get().dispatch_task(
            target="", prompt="干点活", workdir=str(tmp_path / "proj-lik")
        )
    message = str(excinfo.value)
    assert "proj-lik" in message
    assert spawned == []

    # 传文件路径同样拒绝（必须是目录）
    a_file = tmp_path / "not-a-dir.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(secretary_dispatch.SecretaryDispatchError):
        secretary_dispatch.get().dispatch_task(target="", prompt="干点活", workdir=str(a_file))


def test_dispatch_rejects_empty_prompt_and_unknown_target(manager):
    """空 prompt 与不存在的目标都要给出可读拒绝，且不启动回合。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    bridge, spawned, _ = _make_bridge(manager)
    service = secretary_dispatch.get()

    with pytest.raises(secretary_dispatch.SecretaryDispatchError):
        service.dispatch_task(target="项目甲", prompt="   ")
    with pytest.raises(secretary_dispatch.SecretaryDispatchError):
        service.dispatch_task(target="根本不存在的会话", prompt="干点活")
    assert spawned == []


def test_dispatch_ambiguous_target_returns_candidates(manager):
    """target 命中多条 → 返回候选列表让上层追问，不擅自挑一个。"""
    manager.create_session_explicit("项目甲-前端", workdir="GLOBAL", source="GUI")
    manager.create_session_explicit("项目甲-后端", workdir="GLOBAL", source="GUI")
    bridge, spawned, _ = _make_bridge(manager)

    res = secretary_dispatch.get().dispatch_task(target="项目甲", prompt="干点活")

    assert res["ok"] is False
    assert {c["session_name"] for c in res["candidates"]} == {"项目甲-前端", "项目甲-后端"}
    assert spawned == []


# ---------------------------------------------------------------------------
# 能力三：跳转 open_session
# ---------------------------------------------------------------------------

def test_open_session_switches_current_and_emits_event(manager):
    """跳转 = 系统层切换：调 _set_current_session + 推 open_session 事件。"""
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
    """目标已归档 → 先取消归档，再切换并推事件。"""
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


# ---------------------------------------------------------------------------
# 真实回合：角色落库为普通 user 消息 + 收尾通知（真 worker + 桩模型入口）
# ---------------------------------------------------------------------------

class _WorkerHarness:
    """为 _run_turn_worker 桩掉模型入口与事件出口（照 test_gui_automation_dispatch）。"""

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
    """真回合：prompt 以 role=user 落库（无显示标记），收尾推「执行完成」通知。"""
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
    # 通知按发起会话路由，避免串到用户正在查看的会话
    assert all(e[1]["session_name"] == "s_secretary" for e in notices)


def test_drain_message_queue_forwards_secretary_dispatch_marker(manager, tmp_path):
    """忙时入队的派活条目：补发时仍透传懒创建 workdir 与完成通知标记。"""
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
    # 补发的仍是普通用户消息：显示标记与内部回合标记都为空
    assert kwargs.get("display_text", "") == ""
    assert kwargs.get("internal_kind", "") == ""


def test_real_turn_without_dispatch_context_emits_no_notice(manager, tmp_path):
    """普通回合（无派发上下文）：零附加通知，用户亲手发消息不受影响。"""
    manager.create_session_explicit("项目甲", workdir="GLOBAL", source="GUI")
    manager.add_messages("项目甲", [("user", "预热")])
    bridge, _, events = _make_bridge(manager)

    with _WorkerHarness(bridge, manager, tmp_path):
        bridge._run_turn_worker("你好", "项目甲")

    assert [e for e in events if e[0] == "notice"] == []
