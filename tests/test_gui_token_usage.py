import threading

import core.llm as llm
import gui_bridge
from core.memory import MemoryManager


def _bridge_without_init(session_name="gui-token-session"):
    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._current_session = session_name
    # 绕过 __init__ 的旁路桥需补齐 rename_session / _ensure_lan_server 依赖的属性，
    # 与 __init__（gui_bridge.py）中的真实初始化保持同类型默认值
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._lan_server = None
    bridge._lan_lock = threading.Lock()
    return bridge


def _snapshot(*, available=True):
    rate = (99.0, 98.8, 91.0) if available else (None, None, None)
    return {
        "window": {
            "latest_total": 37100,
            "latest_prompt": 36800,
            "latest_cached": 35200,
            "max_window": 1000000,
        },
        "process": {
            "cumulative_total": 500000,
            "cumulative_prompt": 480000,
            "cumulative_completion": 20000,
            "cumulative_cached": 382200,
            "cumulative_cache_prompt": 420000,
        },
        "cache": {
            "available": available,
            "usage_valid": available,
            "latest": {"prompt_tokens": 36800, "cached_tokens": 35200, "rate": rate[0]},
            "hot": {"prompt_tokens": 386800, "cached_tokens": 382200, "rate": rate[1]},
            "session_all": {"prompt_tokens": 420000, "cached_tokens": 382200, "rate": rate[2]},
            "hot_turns": 8,
            "invalidation_reason": "" if available else "usage 不一致",
        },
    }


def test_gui_token_payload_uses_session_prompt_as_cache_denominator(monkeypatch):
    bridge = _bridge_without_init()
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/deepseek-v4",
        "base_url": "https://gui.example/v1",
        "context_window": 1000000,
    })
    monkeypatch.setattr(llm, "get_token_usage_snapshot", lambda *args, **kwargs: _snapshot())

    payload = bridge.get_token_usage()

    assert payload["used"] == 36800
    assert payload["window"] == 1000000
    assert payload["cumulative"] == 500000
    assert payload["cached_pct"] == 91.0
    assert payload["cache_latest_pct"] == 99.0
    assert payload["cache_hot_pct"] == 98.8
    assert payload["cache_session_pct"] == 91.0
    assert payload["cache_available"] is True
    assert payload["cache_hot_turns"] == 8
    assert payload["prompt_tokens"] == 420000
    assert payload["cached_tokens"] == 382200


def test_gui_token_payload_exposes_cli_style_totals(monkeypatch):
    """状态栏与 CLI 口径一致：输入/输出/命中取自 process 累计（计费口径）。"""
    bridge = _bridge_without_init()
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/deepseek-v4",
        "base_url": "https://gui.example/v1",
        "context_window": 1000000,
    })
    monkeypatch.setattr(llm, "get_token_usage_snapshot", lambda *args, **kwargs: _snapshot())

    payload = bridge.get_token_usage()

    assert payload["total_prompt"] == 480000
    assert payload["total_completion"] == 20000
    assert payload["total_cached"] == 382200


def test_gui_token_payload_keeps_unavailable_distinct_from_zero(monkeypatch):
    bridge = _bridge_without_init()
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/deepseek-v4",
        "base_url": "https://gui.example/v1",
        "context_window": 1000000,
    })
    monkeypatch.setattr(
        llm, "get_token_usage_snapshot", lambda *args, **kwargs: _snapshot(available=False)
    )

    payload = bridge.get_token_usage()

    assert payload["cached_pct"] == 0.0
    assert payload["cache_latest_pct"] is None
    assert payload["cache_hot_pct"] is None
    assert payload["cache_session_pct"] is None
    assert payload["cache_available"] is False
    assert payload["cache_invalidation_reason"] == "usage 不一致"


def test_gui_session_rename_and_clear_update_cache_lifecycle(monkeypatch):
    old_name = "gui-cache-old"
    new_name = "gui-cache-new"
    model = "openai/deepseek-v4"
    base = "https://gui-lifecycle.example/v1"
    llm._record_cache_usage(old_name, "main", 100, 90, "", model, base)

    bridge = _bridge_without_init(old_name)
    bridge._memory = MemoryManager(":memory:")
    bridge._memory.create_session_explicit(old_name)
    bridge._memory.add_message(old_name, "user", "需要清理的历史")
    bridge._set_current_session = lambda value: setattr(bridge, "_current_session", value)
    bridge._emit_token_usage = lambda: None

    renamed = bridge.rename_session(old_name, new_name)
    assert renamed["ok"] is True
    assert llm.get_token_usage_snapshot(old_name, model, base)["cache"]["available"] is False
    assert llm.get_token_usage_snapshot(new_name, model, base)["cache"]["available"] is True

    cleared = bridge.clear_session_history(new_name)
    assert cleared["ok"] is True
    assert llm.get_token_usage_snapshot(new_name, model, base)["cache"]["available"] is False
    with llm.token_usage_lock:
        assert llm.session_token_usage["latest_total"] == 0
        assert llm.session_token_usage["latest_prompt"] == 0


def test_gui_registers_realtime_listener_on_init(monkeypatch):
    """GUI 启动必须把 _emit_token_usage 注册为记账监听器：
    每次 LLM 请求（含子代理）记账完立即推送 token_usage 事件，
    前端统计栏与上下文圆圈逐请求实时刷新，而非等回合结束。"""
    # __init__ 会初始化 DB/窗口等重资源，既有测试均不构造完整实例；
    # 这里只断言注册语句存在且传参正确（语句缺失即回归）。
    import inspect
    source = inspect.getsource(gui_bridge.ApiBridge.__init__)
    assert "set_token_usage_listener(self._emit_token_usage)" in source

    # 再验证真实回调链：注册 _emit_token_usage 后，监听器触发即推送事件。
    # 裸实例缺少 delta 缓冲等属性，_flush_deltas 会抛错并被
    # _emit_token_usage 吞掉，因此一并补齐最小依赖。
    import threading

    pushed = []
    bridge = _bridge_without_init()
    bridge._shutting_down = False
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_lock = threading.Lock()
    bridge._delta_last_flush = 0.0
    bridge._delta_flusher = None
    bridge.emit_event = lambda kind, data: pushed.append((kind, data))
    llm.set_token_usage_listener(bridge._emit_token_usage)
    try:
        monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
            "litellm_model": "openai/deepseek-v4",
            "base_url": "https://rt.example/v1",
            "context_window": 1000000,
        })
        monkeypatch.setattr(
            llm, "get_token_usage_snapshot",
            lambda *args, **kwargs: _snapshot(),
        )
        monkeypatch.setattr(llm, "_persist_token_usage", lambda *a, **k: None)
        llm._account_server_usage(
            {
                "prompt_tokens": 1000, "completion_tokens": 100,
                "total_tokens": 1100,
                "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100,
            },
            model_name="openai/deepseek-v4",
            api_base="https://rt.example/v1",
            session_id="gui-realtime",
            cache_scope="main",
            invalidation_reason="",
        )
    finally:
        llm.set_token_usage_listener(None)

    usage_events = [d for kind, d in pushed if kind == "token_usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["used"] == 36800
    assert usage_events[0]["total_prompt"] == 480000


def test_ensure_fits_active_window_runs_asynchronously(tmp_path, monkeypatch):
    """验证 _ensure_fits_active_window 触发压缩时派发到后台线程，不阻塞调用方（门闩机制消除假阳性）。"""
    bridge = _bridge_without_init("async-session")
    db = tmp_path / "async.db"
    bridge._memory = MemoryManager(str(db))
    bridge._current_permission_mode = "confirm"
    bridge._window = None
    bridge._shutting_down = False
    bridge.emit_event = lambda *a, **kw: None

    monkeypatch.setattr(gui_bridge, "get_active_model", lambda session: {
        "litellm_model": "openai/small",
        "context_window": 1000,
        "api_key": "k",
        "base_url": "",
    })
    monkeypatch.setattr(
        "core.agent_service.get_session_token_baseline",
        lambda *a, **kw: 5000,
    )

    bg_started = threading.Event()
    bg_finished = threading.Event()
    latch = threading.Event()

    def fake_prepare(*args, **kwargs):
        bg_started.set()
        latch.wait(timeout=3.0)
        bg_finished.set()

    monkeypatch.setattr("core.agent_service.prepare_turn_messages", fake_prepare)

    bridge._ensure_fits_active_window("async-session")

    # 核心断言：主线程返回瞬间，后台线程已启动但被 latch 门闩阻断（同步实现必在此前已 set finished）
    assert bg_started.wait(timeout=1.0) is True
    assert bg_finished.is_set() is False

    # 释放门闩，后台平稳退出
    latch.set()
    assert bg_finished.wait(timeout=1.0) is True


def test_gui_compress_test_latch_catches_sync_regression(tmp_path, monkeypatch):
    """守门：若 _ensure_fits_active_window 退化为同步实现，门闩断言必须失败。

    用 start() 即同步执行 target 的 Thread 替身替换 gui_bridge.threading：
    替身生效时 bg_finished 在 _ensure_fits_active_window 返回前必然已置位
    （同步特征），证明 runs_asynchronously 用例的「返回瞬间未完成」断言不是
    假阳性——两条用例合起来才封死同步回退。
    """
    bridge = _bridge_without_init("sync-guard-session")
    db = tmp_path / "sync-guard.db"
    bridge._memory = MemoryManager(str(db))
    bridge._current_permission_mode = "confirm"
    bridge._window = None
    bridge._shutting_down = False
    bridge.emit_event = lambda *a, **kw: None
    monkeypatch.setattr(bridge, "_recalc_token_usage", lambda *a, **kw: None)
    monkeypatch.setattr(bridge, "_emit_token_usage", lambda *a, **kw: None)

    monkeypatch.setattr(gui_bridge, "get_active_model", lambda session: {
        "litellm_model": "openai/small",
        "context_window": 1000,
        "api_key": "k",
        "base_url": "",
    })
    monkeypatch.setattr(
        "core.agent_service.get_session_token_baseline",
        lambda *a, **kw: 5000,
    )

    bg_finished = threading.Event()
    monkeypatch.setattr(
        "core.agent_service.prepare_turn_messages",
        lambda *a, **kw: bg_finished.set(),
    )

    class _SyncThread:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

    class _SyncThreadingModule:
        Thread = _SyncThread

    monkeypatch.setattr(gui_bridge, "threading", _SyncThreadingModule)

    bridge._ensure_fits_active_window("sync-guard-session")

    # 同步替身下：调用返回时后台任务已执行完（证明替身确实生效且被路由）；
    # 真实异步实现下此处应为 False——与 runs_asynchronously 用例互补。
    assert bg_finished.is_set() is True


def test_ensure_fits_active_window_skips_when_turn_active(tmp_path, monkeypatch):
    """当会话已有活跃回合时，跳过切模型异步压缩。"""
    bridge = _bridge_without_init("active-session")
    db = tmp_path / "active.db"
    bridge._memory = MemoryManager(str(db))
    bridge._current_permission_mode = "confirm"
    bridge._window = None
    bridge._shutting_down = False
    bridge.emit_event = lambda *a, **kw: None

    monkeypatch.setattr(gui_bridge, "get_active_model", lambda session: {
        "litellm_model": "openai/small",
        "context_window": 1000,
    })
    monkeypatch.setattr(
        "core.agent_service.get_session_token_baseline",
        lambda *a, **kw: 5000,
    )
    monkeypatch.setattr(bridge, "_is_turn_active", lambda session: True)

    bg_started = threading.Event()
    monkeypatch.setattr(
        "core.agent_service.prepare_turn_messages",
        lambda *a, **kw: bg_started.set(),
    )

    bridge._ensure_fits_active_window("active-session")
    assert bg_started.wait(timeout=0.1) is False


def test_ensure_fits_active_window_skips_when_compressing(tmp_path, monkeypatch):
    """当会话后台已在进行记忆压缩时，跳过切模型异步压缩。"""
    bridge = _bridge_without_init("comp-session")
    db = tmp_path / "comp.db"
    bridge._memory = MemoryManager(str(db))
    bridge._current_permission_mode = "confirm"
    bridge._window = None
    bridge._shutting_down = False
    bridge.emit_event = lambda *a, **kw: None

    monkeypatch.setattr(gui_bridge, "get_active_model", lambda session: {
        "litellm_model": "openai/small",
        "context_window": 1000,
    })
    monkeypatch.setattr(
        "core.agent_service.get_session_token_baseline",
        lambda *a, **kw: 5000,
    )
    monkeypatch.setattr(
        "core.memory_compressor.is_session_compressing",
        lambda mgr, s: True,
    )

    bg_started = threading.Event()
    monkeypatch.setattr(
        "core.agent_service.prepare_turn_messages",
        lambda *a, **kw: bg_started.set(),
    )

    bridge._ensure_fits_active_window("comp-session")
    assert bg_started.wait(timeout=0.1) is False

