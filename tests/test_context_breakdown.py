import json
import threading

import core.context_breakdown as cb
import core.llm as llm
import gui_bridge
from core.memory import MemoryManager


def test_set_and_get_roundtrip_in_stable_source_order():
    cb.clear_breakdown()
    try:
        cb.set_breakdown([
            {"source": "mcp_tools", "chars": 45},
            {"source": "system_tools", "chars": 40},
            {"source": "messages", "chars": 10},
            {"source": "skills", "chars": 30},
            {"source": "system_prompt", "chars": 20},
            {"source": "runtime_context", "chars": 50},
        ])
        assert cb.get_breakdown() == [
            {"source": "messages", "chars": 10},
            {"source": "system_prompt", "chars": 20},
            {"source": "skills", "chars": 30},
            {"source": "runtime_context", "chars": 50},
            {"source": "system_tools", "chars": 40},
            {"source": "mcp_tools", "chars": 45},
        ]
    finally:
        cb.clear_breakdown()


def test_get_breakdown_returns_independent_copy():
    cb.clear_breakdown()
    try:
        cb.set_breakdown([{"source": "messages", "chars": 10}])
        first = cb.get_breakdown()
        first.append({"source": "system_tools", "chars": 99})
        first[0]["chars"] = 999
        assert cb.get_breakdown() == [{"source": "messages", "chars": 10}]
    finally:
        cb.clear_breakdown()


def test_update_source_appends_then_overwrites():
    cb.clear_breakdown()
    try:
        cb.update_source("messages", 100)
        assert cb.get_breakdown() == [{"source": "messages", "chars": 100}]
        cb.update_source("system_tools", 7)
        assert cb.get_breakdown() == [
            {"source": "messages", "chars": 100},
            {"source": "system_tools", "chars": 7},
        ]
        cb.update_source("messages", 250)
        assert cb.get_breakdown() == [
            {"source": "messages", "chars": 250},
            {"source": "system_tools", "chars": 7},
        ]
    finally:
        cb.clear_breakdown()


def test_invalid_sources_are_filtered_without_raising():
    cb.clear_breakdown()
    try:
        cb.set_breakdown([
            {"source": "hacker", "chars": 1},
            {"source": 123, "chars": 1},
            {"source": None, "chars": 1},
            {"chars": 1},
            "not-a-dict",
            None,
            {"source": "messages", "chars": 5},
        ])
        assert cb.get_breakdown() == [{"source": "messages", "chars": 5}]

        cb.update_source("unknown_source", 10)
        cb.update_source(None, 10)
        cb.update_source("messages", 5)
        assert cb.get_breakdown() == [{"source": "messages", "chars": 5}]

        cb.update_source("tools", 10)
        assert cb.get_breakdown() == [{"source": "messages", "chars": 5}]
    finally:
        cb.clear_breakdown()


def test_invalid_chars_are_filtered_without_raising():
    cb.clear_breakdown()
    try:
        cb.set_breakdown([
            {"source": "messages", "chars": -1},
            {"source": "skills", "chars": "abc"},
            {"source": "system_tools", "chars": None},
            {"source": "system_prompt"},
            {"source": "runtime_context", "chars": 3.9},
            {"source": "messages", "chars": "12"},
        ])
        assert cb.get_breakdown() == [
            {"source": "messages", "chars": 12},
            {"source": "runtime_context", "chars": 3},
        ]

        cb.update_source("system_tools", -5)
        cb.update_source("system_tools", "xyz")
        assert cb.get_breakdown() == [
            {"source": "messages", "chars": 12},
            {"source": "runtime_context", "chars": 3},
        ]
    finally:
        cb.clear_breakdown()


def test_set_breakdown_tolerates_garbage_input_without_raising():
    cb.clear_breakdown()
    try:
        cb.set_breakdown(None)
        assert cb.get_breakdown() == []
        cb.set_breakdown("not-a-list")
        assert cb.get_breakdown() == []
        cb.set_breakdown(12345)
        assert cb.get_breakdown() == []
    finally:
        cb.clear_breakdown()


def test_clear_breakdown_empties_snapshot_and_is_idempotent():
    cb.set_breakdown([{"source": "messages", "chars": 1}])
    cb.clear_breakdown()
    assert cb.get_breakdown() == []
    cb.clear_breakdown()
    assert cb.get_breakdown() == []


def test_get_breakdown_on_empty_snapshot_returns_empty_list():
    cb.clear_breakdown()
    assert cb.get_breakdown() == []


def test_concurrent_updates_smoke():
    cb.clear_breakdown()
    errors = []

    def worker():
        try:
            for i in range(300):
                cb.update_source("messages", i)
                cb.update_source("system_tools", i * 2)
                cb.get_breakdown()
        except Exception as exc:  # pragma: no cover - 仅在回归时触发
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    try:
        assert errors == []
        snapshot = cb.get_breakdown()
        assert [item["source"] for item in snapshot] == ["messages", "system_tools"]
        assert all(isinstance(item["chars"], int) for item in snapshot)
    finally:
        cb.clear_breakdown()


def test_tool_breakdown_splits_system_and_mcp(monkeypatch):
    class _FakeMCP:
        def get_schemas(self):
            return [{"function": {"name": "firecrawl__scrape_url"}}]
    monkeypatch.setattr(llm, "_get_mcp_manager", lambda: _FakeMCP())
    cb.clear_breakdown()
    try:
        llm._snapshot_tool_breakdown([
            {"function": {"name": "read_file"}},
            {"function": {"name": "firecrawl__scrape_url"}},
        ])
        snapshot = {item["source"]: item["chars"] for item in cb.get_breakdown()}
        assert snapshot["system_tools"] == len(
            json.dumps([{"function": {"name": "read_file"}}], ensure_ascii=False)
        )
        assert snapshot["mcp_tools"] == len(
            json.dumps([{"function": {"name": "firecrawl__scrape_url"}}], ensure_ascii=False)
        )
    finally:
        cb.clear_breakdown()


def test_tool_breakdown_empty_group_writes_zero(monkeypatch):
    class _FakeMCP:
        def get_schemas(self):
            return []
    monkeypatch.setattr(llm, "_get_mcp_manager", lambda: _FakeMCP())
    cb.clear_breakdown()
    try:
        llm._snapshot_tool_breakdown([{"function": {"name": "read_file"}}])
        snapshot = {item["source"]: item["chars"] for item in cb.get_breakdown()}
        assert snapshot["mcp_tools"] == 0
        assert snapshot["system_tools"] > 0
    finally:
        cb.clear_breakdown()


def test_tool_breakdown_failure_is_silent(monkeypatch):
    def _boom():
        raise RuntimeError("mcp manager exploded")
    monkeypatch.setattr(llm, "_get_mcp_manager", _boom)
    cb.clear_breakdown()
    try:
        llm._snapshot_tool_breakdown([{"function": {"name": "read_file"}}])
    finally:
        cb.clear_breakdown()


def _bridge_without_init(session_name="ctx-breakdown-session"):
    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._current_session = session_name
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._lan_server = None
    bridge._lan_lock = threading.Lock()
    return bridge


def _minimal_snapshot():
    return {
        "window": {
            "latest_total": 900,
            "latest_prompt": 880,
            "latest_cached": 0,
            "max_window": 128000,
        },
        "process": {
            "cumulative_total": 0,
            "cumulative_prompt": 0,
            "cumulative_completion": 0,
            "cumulative_cached": 0,
            "cumulative_cache_prompt": 0,
        },
        "cache": {
            "available": False,
            "usage_valid": False,
            "latest": {"prompt_tokens": 0, "cached_tokens": 0, "rate": None},
            "hot": {"prompt_tokens": 0, "cached_tokens": 0, "rate": None},
            "session_all": {"prompt_tokens": 0, "cached_tokens": 0, "rate": None},
            "hot_turns": 0,
            "invalidation_reason": "",
        },
    }


def test_gui_token_usage_payload_includes_context_breakdown(monkeypatch):
    bridge = _bridge_without_init()
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/test",
        "base_url": "",
        "context_window": 128000,
    })
    monkeypatch.setattr(
        llm, "get_token_usage_snapshot", lambda *args, **kwargs: _minimal_snapshot()
    )

    cb.set_breakdown([
        {"source": "messages", "chars": 1000},
        {"source": "skills", "chars": 200},
    ])
    try:
        payload = bridge.get_token_usage()
    finally:
        cb.clear_breakdown()

    assert payload["context_breakdown"] == [
        {"source": "messages", "chars": 1000},
        {"source": "skills", "chars": 200},
    ]
    assert payload["used"] == 880
    assert payload["window"] == 128000


def test_gui_token_usage_failure_fallback_exposes_empty_breakdown(monkeypatch):
    bridge = _bridge_without_init()
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/test",
        "base_url": "",
        "context_window": 128000,
    })

    def _boom(*_args, **_kwargs):
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(llm, "get_token_usage_snapshot", _boom)
    payload = bridge.get_token_usage()
    assert payload["context_breakdown"] == []
    assert payload["used"] == 0


def test_clear_session_history_clears_context_breakdown(monkeypatch):
    bridge = _bridge_without_init("ctx-clear")
    bridge._memory = MemoryManager(":memory:")
    bridge._memory.create_session_explicit("ctx-clear")
    bridge._emit_token_usage = lambda: None

    cb.set_breakdown([{"source": "messages", "chars": 42}])
    try:
        result = bridge.clear_session_history("ctx-clear")
        assert result["ok"] is True
        assert cb.get_breakdown() == []
    finally:
        cb.clear_breakdown()


def test_recalc_token_usage_clears_context_breakdown(monkeypatch):
    bridge = _bridge_without_init("ctx-recalc")
    bridge._memory = MemoryManager(":memory:")
    bridge._memory.create_session_explicit("ctx-recalc")
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/test",
        "base_url": "",
        "context_window": 128000,
    })
    monkeypatch.setattr(
        "core.agent_service.get_session_token_baseline", lambda *args, **kwargs: 1234
    )

    cb.set_breakdown([{"source": "messages", "chars": 42}])
    try:
        bridge._recalc_token_usage("ctx-recalc")
        assert cb.get_breakdown() == []
    finally:
        cb.clear_breakdown()


def test_recalc_token_usage_keeps_breakdown_when_snapshot_preserved(monkeypatch):
    bridge = _bridge_without_init("ctx-keep")
    bridge._memory = MemoryManager(":memory:")
    bridge._memory.create_session_explicit("ctx-keep")
    monkeypatch.setattr(gui_bridge, "get_active_model", lambda *_args: {
        "litellm_model": "openai/test",
        "base_url": "",
        "context_window": 128000,
    })
    monkeypatch.setattr(
        "core.agent_service.get_session_token_baseline", lambda *args, **kwargs: 1234
    )

    cb.set_breakdown([{"source": "messages", "chars": 42}])
    try:
        bridge._recalc_token_usage("ctx-keep", clear_breakdown_snapshot=False)
        assert cb.get_breakdown() == [{"source": "messages", "chars": 42}]
    finally:
        cb.clear_breakdown()
