import threading

import core.llm as llm
import gui_bridge
from core.memory import MemoryManager


def _bridge_without_init(session_name="gui-token-session"):
    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._current_session = session_name
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
    import inspect
    source = inspect.getsource(gui_bridge.ApiBridge.__init__)
    assert "set_token_usage_listener(self._emit_token_usage)" in source

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

    assert bg_started.wait(timeout=1.0) is True
    assert bg_finished.is_set() is False

    latch.set()
    assert bg_finished.wait(timeout=1.0) is True


def test_gui_compress_test_latch_catches_sync_regression(tmp_path, monkeypatch):
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

    assert bg_finished.is_set() is True


def test_ensure_fits_active_window_skips_when_turn_active(tmp_path, monkeypatch):
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


_LEGACY_GUI_TOKEN_KEYS = {
    "used", "window", "cached_pct", "cumulative",
    "cache_latest_pct", "cache_hot_pct", "cache_session_pct",
    "cache_available", "cache_hot_turns", "cache_invalidation_reason",
    "prompt_tokens", "cached_tokens",
    "total_prompt", "total_completion", "total_cached", "context_breakdown",
}


def _snapshot_with_scopes():
    snapshot = _snapshot()
    snapshot["cache_scopes"] = {
        "main": dict(snapshot["cache"]),
        "keepalive": {
            "available": True,
            "usage_valid": True,
            "latest": {"prompt_tokens": 1000, "cached_tokens": 100, "rate": 10.0},
            "hot": {"prompt_tokens": 1000, "cached_tokens": 100, "rate": 10.0},
            "session_all": {"prompt_tokens": 1000, "cached_tokens": 100, "rate": 10.0},
            "hot_turns": 1,
            "invalidation_reason": "",
        },
    }
    return snapshot


def test_gui_token_payload_exposes_lane_dimension_keeping_legacy_keys(monkeypatch):
    bridge = _bridge_without_init()
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/deepseek-v4",
        "base_url": "https://gui.example/v1",
        "context_window": 1000000,
    })
    monkeypatch.setattr(
        llm, "get_token_usage_snapshot", lambda *args, **kwargs: _snapshot_with_scopes()
    )

    payload = bridge.get_token_usage()

    assert _LEGACY_GUI_TOKEN_KEYS <= set(payload)
    assert payload["cache_lane_model"] == "openai/deepseek-v4"
    assert payload["cache_lane_api_base"] == "https://gui.example/v1"
    assert payload["cache_keepalive_session_pct"] == 10.0
    assert payload["cache_keepalive_hot_pct"] == 10.0
    assert payload["cache_hot_pct"] == 98.8


def test_gui_token_payload_does_not_duplicate_existing_rate_keys(monkeypatch):
    bridge = _bridge_without_init()
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/deepseek-v4",
        "base_url": "https://gui.example/v1",
        "context_window": 1000000,
    })
    monkeypatch.setattr(
        llm, "get_token_usage_snapshot", lambda *args, **kwargs: _snapshot_with_scopes()
    )

    payload = bridge.get_token_usage()

    assert "cache_lane_hot_pct" not in payload
    assert "cache_lane_session_pct" not in payload
    assert "cache_lane_scope" not in payload


def test_gui_keepalive_lane_rate_unreported_is_none_not_zero(monkeypatch):
    bridge = _bridge_without_init()
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/local-vllm",
        "base_url": "http://127.0.0.1:8000/v1",
        "context_window": 1000000,
    })
    snapshot = _snapshot()
    snapshot["cache_scopes"] = {
        "keepalive": {
            "available": False,
            "usage_valid": False,
            "latest": {"prompt_tokens": 0, "cached_tokens": 0, "rate": None},
            "hot": {"prompt_tokens": 0, "cached_tokens": 0, "rate": None},
            "session_all": {"prompt_tokens": 0, "cached_tokens": 0, "rate": None},
            "hot_turns": 0,
            "invalidation_reason": "usage 不一致",
        },
    }
    monkeypatch.setattr(llm, "get_token_usage_snapshot", lambda *args, **kwargs: snapshot)

    payload = bridge.get_token_usage()

    assert _LEGACY_GUI_TOKEN_KEYS <= set(payload)
    assert payload["cache_keepalive_hot_pct"] is None
    assert payload["cache_keepalive_session_pct"] is None


def test_recalc_token_usage_preserves_deepseek_real_usage_across_session_switch(tmp_path, monkeypatch):
    import core.agent_service as agent_service

    session = "ds-recalc-session"
    bridge = _bridge_without_init(session)
    bridge._memory = MemoryManager(str(tmp_path / "ds-recalc.db"))
    bridge._memory.create_session_explicit(session)
    bridge._memory.add_message(session, "user", "hello")
    bridge._memory.add_token_usage(
        "openai", "deepseek-v4.1-flash-1017", 263184, 1511, 261888, 264695,
        session_id=session, cache_scope="main",
    )

    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/deepseek-v4.1-flash-1017",
        "base_url": "https://api.deepseek.com",
        "context_window": 1000000,
    })
    monkeypatch.setattr(
        agent_service, "estimate_history_tokens_with_system", lambda *_a, **_k: 96690
    )

    saved = dict(llm.session_token_usage)
    try:
        bridge._recalc_token_usage(session)
        payload = bridge.get_token_usage()
        assert payload["used"] == 264695
        assert payload["window"] == 1000000
    finally:
        with llm.token_usage_lock:
            llm.session_token_usage.update(saved)


def test_recalc_token_usage_returns_zero_for_cleared_session(tmp_path, monkeypatch):
    session = "ds-cleared-session"
    bridge = _bridge_without_init(session)
    bridge._memory = MemoryManager(str(tmp_path / "ds-cleared.db"))
    bridge._memory.create_session_explicit(session)
    bridge._memory.add_message(session, "user", "hello")
    bridge._memory.add_token_usage(
        "openai", "deepseek-v4.1-flash-1017", 263184, 1511, 261888, 264695,
        session_id=session, cache_scope="main",
    )
    bridge._memory.clear_session_messages(session)

    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/deepseek-v4.1-flash-1017",
        "base_url": "https://api.deepseek.com",
        "context_window": 1000000,
    })

    saved = dict(llm.session_token_usage)
    try:
        bridge._recalc_token_usage(session)
        payload = bridge.get_token_usage()
        assert payload["used"] == 0
    finally:
        with llm.token_usage_lock:
            llm.session_token_usage.update(saved)


def test_ensure_fits_active_window_forwards_api_base(tmp_path, monkeypatch):
    import core.agent_service as agent_service

    session = "ds-fit-session"
    bridge = _bridge_without_init(session)
    bridge._memory = MemoryManager(str(tmp_path / "ds-fit.db"))
    bridge._memory.create_session_explicit(session)
    bridge._memory.add_message(session, "user", "hello")

    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/deepseek-v4.1-flash-1017",
        "base_url": "https://api.deepseek.com",
        "context_window": 1000000,
    })
    monkeypatch.setattr(bridge, "_is_turn_active", lambda *_a: False)

    captured = {}

    def _fake_baseline(manager, sess, model, history=None, api_base=""):
        captured["sess"] = sess
        captured["model"] = model
        captured["history"] = history
        captured["api_base"] = api_base
        return 1000

    monkeypatch.setattr(agent_service, "get_session_token_baseline", _fake_baseline)

    bridge._ensure_fits_active_window(session)
    assert captured["sess"] == session
    assert captured["model"] == "openai/deepseek-v4.1-flash-1017"
    assert captured["api_base"] == "https://api.deepseek.com"
    assert captured["history"] == [{"role": "user", "content": "hello"}]


def test_background_session_usage_and_recalc_do_not_overwrite_current_session(
    tmp_path, monkeypatch
):
    from core import context_breakdown as cb

    bridge = _bridge_without_init("session-a")
    bridge._memory = MemoryManager(str(tmp_path / "iso.db"))
    bridge._memory.create_session_explicit("session-a")
    bridge._memory.create_session_explicit("session-b")
    bridge._memory.add_message("session-a", "user", "hello a")
    bridge._memory.add_message("session-b", "user", "hello b")

    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/glm-5.3",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "context_window": 1000000,
    })
    monkeypatch.setattr(llm, "_persist_token_usage", lambda *a, **k: None)

    saved = dict(llm.session_token_usage)
    try:
        llm._account_server_usage(
            {"prompt_tokens": 160000, "completion_tokens": 200, "total_tokens": 160200},
            model_name="openai/glm-5.3",
            api_base="https://open.bigmodel.cn/api/paas/v4",
            session_id="session-a",
            cache_scope="main",
            invalidation_reason="",
        )
        cb.set_breakdown([{"source": "messages", "chars": 5000}], session_id="session-a")

        # 后台会话记账 + 重算不得覆盖当前 UI 会话 session-a 的上下文占用与占比
        llm._account_server_usage(
            {"prompt_tokens": 100000, "completion_tokens": 100, "total_tokens": 100100},
            model_name="openai/glm-5.3",
            api_base="https://open.bigmodel.cn/api/paas/v4",
            session_id="session-b",
            cache_scope="main",
            invalidation_reason="",
        )
        cb.set_breakdown([{"source": "messages", "chars": 100}], session_id="session-b")
        bridge._recalc_token_usage("session-b")

        payload = bridge.get_token_usage()
        assert payload["used"] == 160000
        assert payload["context_breakdown"] == [{"source": "messages", "chars": 5000}]
    finally:
        llm.clear_cache_usage_session("session-a")
        llm.clear_cache_usage_session("session-b")
        cb.clear_breakdown()
        with llm.token_usage_lock:
            llm.session_token_usage.update(saved)


def test_async_recalc_skips_emit_when_session_no_longer_current(tmp_path, monkeypatch):
    bridge = _bridge_without_init("session-new")
    bridge._memory = MemoryManager(str(tmp_path / "async-iso.db"))
    bridge._memory.create_session_explicit("session-old")
    bridge._memory.create_session_explicit("session-new")

    emitted = []
    bridge._emit_raw = lambda kind, data: emitted.append((kind, data))
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/glm-5.3",
        "base_url": "",
        "context_window": 1000000,
    })

    bridge._async_recalc_token_usage("session-old")
    assert emitted == []
    llm.clear_cache_usage_session("session-old")
    llm.clear_cache_usage_session("session-new")


def test_turn_worker_preserves_latest_prompt_unless_compressed(tmp_path, monkeypatch):
    from tests.test_gui_message_final import _bare_turn_bridge

    session = "turn-preserve-session"
    bridge = _bare_turn_bridge()
    bridge._current_session = session
    bridge._memory = MemoryManager(str(tmp_path / "preserve.db"))
    # _bare_turn_bridge 已把 turn_undo 默认 manager 绑在旧的 :memory: 库上，
    # 换库后必须重绑：否则 _run_turn_worker 建检查点时 resolve_session_id
    # 在新库里查不到会话，直接抛 TurnUndoError 让整个回合失败。
    from core.turn_undo import service as _tu_service

    _tu_service.configure_default_manager(
        bridge._memory, blob_root=tmp_path / "undo-blobs"
    )
    bridge._memory.create_session_explicit(session)
    bridge._memory.add_token_usage(
        "openai", "glm-5.3", 160000, 200, 0, 160200,
        session_id=session, cache_scope="main",
    )

    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))
    bridge._session_workdir = lambda _name: str(tmp_path)
    bridge._sync_active_model_context = lambda *_a: None
    bridge._subagent_progress_poller = lambda *_a, **_k: None
    bridge._maybe_auto_rename = lambda _name: None
    bridge._consume_turn_events = lambda *_a, **_k: False
    # _bare_turn_bridge 把 _emit_token_usage 换成了 no-op；本测试要观察推送的
    # token_usage 事件，这里接回 ApiBridge 的真实实现。
    bridge._emit_token_usage = gui_bridge.ApiBridge._emit_token_usage.__get__(bridge)

    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_a, **_k: {
        "litellm_model": "openai/glm-5.3",
        "api_key": "key",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "context_window": 1000000,
    })
    monkeypatch.setattr(
        "core.input_parser.parse_user_input", lambda text, **_kwargs: (text, [])
    )
    monkeypatch.setattr(
        "core.agent_service.estimate_history_tokens_with_system",
        lambda *_a, **_k: 100000,
    )

    saved = dict(llm.session_token_usage)
    try:
        # 1) 未触发压缩：已有 latest_prompt=160000 应保留，不得在回合起始瞬间跌回 100000
        llm.set_session_window_usage(session, latest_total=160200, latest_prompt=160000)
        monkeypatch.setattr(
            "core.agent_service.prepare_turn_messages", lambda *_a, **_k: []
        )
        bridge._run_turn_worker("下一轮", session)
        usage_events = [d for kind, d in emitted if kind == "token_usage"]
        assert usage_events and usage_events[0]["used"] == 160000

        # 2) 实际触发压缩（notify("compress", ...)）：必须重算并回落到新基线
        emitted.clear()

        def _compressed_prepare(*_a, **kwargs):
            kwargs["notify"]("compress", "已折叠")
            return []

        monkeypatch.setattr("core.agent_service.prepare_turn_messages", _compressed_prepare)
        bridge._run_turn_worker("再来一轮", session)
        usage_events = [d for kind, d in emitted if kind == "token_usage"]
        assert usage_events and usage_events[0]["used"] == 100000
    finally:
        llm.clear_cache_usage_session(session)
        with llm.token_usage_lock:
            llm.session_token_usage.update(saved)
