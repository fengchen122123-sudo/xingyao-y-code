"""闲时自动维护调度回归。

空闲超时后跑本地水位维护（tier1/tier2）+ 水位达标才触发 LLM 压缩（复用
_trigger_background_precompress）；回合开始撤销未触发任务并协作中断在跑
的闲时压缩；配置 0 = 关闭。
"""

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
    # 测试用极短延时 + 直接注入 manager，不触碰真实 app_context 单例
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
    assert pre == []  # 水位远未达标：不产生任何 LLM 调用
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
            done_event.set()  # 模拟压缩线程结束信号
        return True

    monkeypatch.setattr(
        "core.agent_service._trigger_background_precompress", fake_precompress
    )

    notices = []
    svc = IdleMaintenanceService()
    svc.on_turn_finished("idle-session", notify=lambda kind, msg: notices.append(msg))
    assert fired.wait(5)
    assert holder["session"] == "idle-session"
    # notify 原样透传给压缩管道
    holder["notify"]("compress", "提示")
    assert notices == ["提示"]
    svc.shutdown()


def test_turn_start_cancels_pending_job():
    svc = IdleMaintenanceService()
    svc.on_turn_finished("s")  # 0.05s 后将触发
    svc.on_turn_started("s")   # 回合开始：立即撤销
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

    svc._do_maintenance = fake_do  # 拦截实际维护，只验证取消传播
    svc.on_turn_finished("s")
    assert started.wait(5)
    svc.on_turn_started("s")
    assert holder["event"].wait(2)  # 协作取消事件必须被置位
    release.set()
    svc.shutdown()


def test_cancel_event_stays_registered_until_compression_done(manager, monkeypatch):
    """压缩在飞期间 cancel_event 必须留在注册表，回合开始才能中断它。

    回归背景：_trigger_background_precompress 发射后即返回，若 worker 不等
    压缩结束就弹事件，压缩在飞的整个过程里 on_turn_started 拿不到事件，
    协作中断承诺对 LLM 阶段失效。
    """
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
        assert "idle-session" in svc._cancel_events  # 压缩未结束：事件必须仍在注册表

    svc.on_turn_started("idle-session")  # 用户发消息：必须能拿到并置位该事件
    with svc._condition:
        assert svc._cancel_events["idle-session"].is_set()

    release.set()  # 压缩结束
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
    """到期的瞬间恰好有新回合开始：锁内复查忙闲后必须跳过本轮维护。"""
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
    assert not ran.is_set()          # 忙会话被跳过
    assert svc.status()["pending"] == 0  # 条目已消费，不会反复空转
    svc._busy.discard("s")
    svc.shutdown()
