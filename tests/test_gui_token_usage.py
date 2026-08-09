import core.llm as llm
import gui_bridge
from core.memory import MemoryManager


def _bridge_without_init(session_name="gui-token-session"):
    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._current_session = session_name
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
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda: {
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


def test_gui_token_payload_keeps_unavailable_distinct_from_zero(monkeypatch):
    bridge = _bridge_without_init()
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda: {
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
