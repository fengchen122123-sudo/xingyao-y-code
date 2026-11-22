import threading
import time

import pytest

import core.idle_maintenance as im
from core.idle_maintenance import IdleMaintenanceService
from core.memory import MemoryManager


@pytest.fixture
def manager():
    m = MemoryManager(":memory:")
    m.create_session_explicit("idle-session", workdir="GLOBAL", source="GUI")
    return m


@pytest.fixture(autouse=True)
def _env(monkeypatch, manager):
    monkeypatch.setattr(im, "get_idle_maintenance_seconds", lambda: 0.05)
    monkeypatch.setattr("core.app_context.get_manager", lambda: manager)
    fake_active = {
        "litellm_model": "fake/model",
        "api_key": "k",
        "base_url": "",
        "context_window": 128000,
    }
    monkeypatch.setattr("core.model_manager.get_active_model", lambda s: fake_active)


def test_local_tiers_run_and_no_compression_below_watermark(manager, monkeypatch):
    calls = []
    done = threading.Event()

    def fake_maintain(session, tier):
        calls.append(tier)
        if len(calls) >= 2:
            done.set()
        return (0, 0)

    monkeypatch.setattr(manager, "maintain_tool_results", fake_maintain)
    monkeypatch.setattr(
        "core.agent_service.get_session_token_baseline", lambda *a, **k: 1.0
    )
    pre = []
    monkeypatch.setattr(
        "core.agent_service._trigger_background_precompress",
        lambda *a, **k: pre.append(a) or False,
    )

    svc = IdleMaintenanceService()
    svc.on_turn_finished("idle-session", notify=None)
    assert done.wait(5)
    assert sorted(calls) == [1, 2]
    assert pre == []
    svc.shutdown()


def test_compression_triggers_above_watermark(manager, monkeypatch):
    monkeypatch.setattr(
        manager, "maintain_tool_results", lambda session, tier: (0, 0)
    )
    monkeypatch.setattr(
        "core.agent_service.get_session_token_baseline", lambda *a, **k: 200000.0
    )
    fired = threading.Event()
    holder = {}

    def fake_precompress(manager_, session, model, key, url, cancel_event, notify,
                         done_event=None):
        holder["session"] = session
        holder["cancel_event"] = cancel_event
        holder["notify"] = notify
        fired.set()
        if done_event is not None:
            done_event.set()
        return True

    monkeypatch.setattr(
        "core.agent_service._trigger_background_precompress", fake_precompress
    )

    notices = []
    svc = IdleMaintenanceService()
    svc.on_turn_finished("idle-session", notify=lambda kind, msg: notices.append(msg))
    assert fired.wait(5)
    assert holder["session"] == "idle-session"
    holder["notify"]("compress", "提示")
    assert notices == ["提示"]
    svc.shutdown()


def test_turn_start_cancels_pending_job():
    svc = IdleMaintenanceService()
    svc.on_turn_finished("s")
    svc.on_turn_started("s")
    time.sleep(0.3)
    assert svc.status()["pending"] == 0
    svc.shutdown()


def test_turn_start_sets_cancel_event_of_running_compression():
    svc = IdleMaintenanceService()
    started = threading.Event()
    release = threading.Event()
    holder = {}

    def fake_do(session, notify, cancel_event):
        holder["event"] = cancel_event
        started.set()
        release.wait(5)

    svc._do_maintenance = fake_do
    svc.on_turn_finished("s")
    assert started.wait(5)
    svc.on_turn_started("s")
    assert holder["event"].wait(2)
    release.set()
    svc.shutdown()


def test_cancel_event_stays_registered_until_compression_done(manager, monkeypatch):
    monkeypatch.setattr(
        manager, "maintain_tool_results", lambda session, tier: (0, 0)
    )
    monkeypatch.setattr(
        "core.agent_service.get_session_token_baseline", lambda *a, **k: 200000.0
    )
    started = threading.Event()
    release = threading.Event()

    def fake_precompress(*args, **kwargs):
        started.set()
        assert release.wait(5)
        if kwargs.get("done_event") is not None:
            kwargs["done_event"].set()
        return True

    monkeypatch.setattr(
        "core.agent_service._trigger_background_precompress", fake_precompress
    )

    svc = IdleMaintenanceService()
    svc.on_turn_finished("idle-session")
    assert started.wait(5)
    with svc._condition:
        assert "idle-session" in svc._cancel_events

    svc.on_turn_started("idle-session")
    with svc._condition:
        assert svc._cancel_events["idle-session"].is_set()

    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with svc._condition:
            if "idle-session" not in svc._cancel_events:
                break
        time.sleep(0.02)
    else:
        pytest.fail("压缩结束后 cancel_event 未从注册表清理")
    svc.shutdown()


def test_disabled_when_zero(monkeypatch):
    monkeypatch.setattr(im, "get_idle_maintenance_seconds", lambda: 0)
    svc = IdleMaintenanceService()
    svc.on_turn_finished("s")
    assert svc.status()["pending"] == 0
    svc.shutdown()


def test_busy_session_is_skipped_at_fire_time(monkeypatch, manager):
    ran = threading.Event()

    def fake_maintain(session, tier):
        ran.set()
        return (0, 0)

    monkeypatch.setattr(manager, "maintain_tool_results", fake_maintain)
    svc = IdleMaintenanceService()
    # 直接注入一个已到期的条目并标记忙，复现"到期瞬间开始回合"的竞态窗口
    with svc._condition:
        svc._busy.add("s")
        svc._entries["s"] = {"deadline": time.monotonic() - 0.01, "notify": None}
        svc._ensure_thread_locked()
        svc._condition.notify_all()
    time.sleep(0.3)
    assert not ran.is_set()
    assert svc.status()["pending"] == 0
    svc._busy.discard("s")
    svc.shutdown()


def test_self_learning_thread_binds_own_session_instead_of_active_ui_session(
    manager, monkeypatch
):
    from core import app_context

    observed = {}
    monkeypatch.setattr(app_context, "get_current_session", lambda: "ui-viewing-other")

    def fake_run_turn_learning(session, mgr, model_cfg):
        observed["session_arg"] = session
        observed["thread_session"] = app_context.get_thread_session()
        return False

    monkeypatch.setattr("core.learning.run_turn_learning", fake_run_turn_learning)
    app_context.set_thread_session("outer-session")
    try:
        im._run_self_learning("idle-session", manager, ("m", "k", ""))
        assert observed == {
            "session_arg": "idle-session",
            "thread_session": "idle-session",
        }
        assert app_context.get_thread_session() == "outer-session"
    finally:
        app_context.set_thread_session(None)
