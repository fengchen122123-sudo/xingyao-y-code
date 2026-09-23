"""输入框上下文用量面板：上下文来源字符占比快照测试。

设计规格：`待开发/输入框上下文用量面板/设计规格.md` §3.3/§3.4。
口径为字符数估算，仅用于 GUI 展示比例（不进 token 账本）；
所有统计必须纯旁路——任何输入异常都不得抛出。

覆盖：
- core.context_breakdown 的 set/get 往返、副本隔离、update 追加/覆盖、
  非法 source/chars 过滤、clear、并发 smoke；
- gui_bridge.get_token_usage 输出 context_breakdown 字段（含异常兜底分支）；
- 清空历史 / 重算 token 用量时快照同步清理（与 token 记账同生命周期）。
"""

import json
import threading

import core.context_breakdown as cb
import core.llm as llm
import gui_bridge
from core.memory import MemoryManager


# ---------------------------------------------------------------------------
# core.context_breakdown 单元行为
# ---------------------------------------------------------------------------

def test_set_and_get_roundtrip_in_stable_source_order():
    """set 后 get 往返一致；输出按固定来源顺序（与白名单顺序一致）。"""
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
    """返回快照副本：调用方修改列表或其中 dict 不得影响内部状态。"""
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
    """update_source 对未出现来源追加，对已有来源覆盖。"""
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
    """非白名单 source / 非 dict 项被过滤，不抛异常。"""
    cb.clear_breakdown()
    try:
        cb.set_breakdown([
            {"source": "hacker", "chars": 1},        # 非白名单
            {"source": 123, "chars": 1},             # source 非字符串
            {"source": None, "chars": 1},            # source 为 None
            {"chars": 1},                             # 缺 source
            "not-a-dict",                             # 项非 dict
            None,                                     # 项为 None
            {"source": "messages", "chars": 5},      # 合法项保留
        ])
        assert cb.get_breakdown() == [{"source": "messages", "chars": 5}]

        cb.update_source("unknown_source", 10)
        cb.update_source(None, 10)
        cb.update_source("messages", 5)
        assert cb.get_breakdown() == [{"source": "messages", "chars": 5}]

        # 2026-09-23 对齐 ZCode：旧来源 "tools" 已移出白名单，更新被静默过滤
        cb.update_source("tools", 10)
        assert cb.get_breakdown() == [{"source": "messages", "chars": 5}]
    finally:
        cb.clear_breakdown()


def test_invalid_chars_are_filtered_without_raising():
    """chars 必须是可整数化的非负值：负数/非数字/缺失被过滤，浮点截断取整。"""
    cb.clear_breakdown()
    try:
        cb.set_breakdown([
            {"source": "messages", "chars": -1},           # 负数过滤
            {"source": "skills", "chars": "abc"},          # 非数字过滤
            {"source": "system_tools", "chars": None},     # None 过滤
            {"source": "system_prompt"},                   # 缺 chars 过滤
            {"source": "runtime_context", "chars": 3.9},   # 浮点整数化（截断）
            {"source": "messages", "chars": "12"},         # 数字字符串整数化
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
    """整体替换语义：不可迭代输入被吞掉并替换为空快照，绝不抛异常。"""
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
    """多线程 update/get 并发不抛异常，且快照始终只含合法来源。"""
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


# ---------------------------------------------------------------------------
# llm._snapshot_tool_breakdown：工具来源拆分系统工具 / MCP 工具
# ---------------------------------------------------------------------------

def test_tool_breakdown_splits_system_and_mcp(monkeypatch):
    """工具字符量按 MCP 名集拆为 system_tools / mcp_tools 两组。"""
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
    """无 MCP 工具时空组写 0（而非 '[]' 的 2），前端不会渲染 0 行。"""
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
    """统计异常被吞掉绝不抛出（旁路铁律）。"""
    def _boom():
        raise RuntimeError("mcp manager exploded")
    monkeypatch.setattr(llm, "_get_mcp_manager", _boom)
    cb.clear_breakdown()
    try:
        llm._snapshot_tool_breakdown([{"function": {"name": "read_file"}}])  # 不得抛异常
    finally:
        cb.clear_breakdown()


# ---------------------------------------------------------------------------
# gui_bridge.get_token_usage 输出与生命周期清理
# ---------------------------------------------------------------------------

def _bridge_without_init(session_name="ctx-breakdown-session"):
    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._current_session = session_name
    # 与 tests/test_gui_token_usage.py 的旁路桥补齐依赖保持同类型默认值
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
    # 原有字段不受影响
    assert payload["used"] == 880
    assert payload["window"] == 128000


def test_gui_token_usage_failure_fallback_exposes_empty_breakdown(monkeypatch):
    """快照读取异常走兜底分支时，context_breakdown 必须是空列表而非缺失。"""
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
    """回合组装后重算（clear_breakdown_snapshot=False）必须保留刚写入的快照。

    回归（2026-09-23）：回合路径曾误用默认清理语义，组装刚写入的来源占比
    快照被重算清掉，导致组装后推给前端的面板数据永远为空。
    """
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
