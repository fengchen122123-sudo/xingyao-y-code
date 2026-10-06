# -*- coding: utf-8 -*-
"""IDE 语言服务桥方法：请求转发、路径策略、状态查询、诊断事件合批派发。"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core.gui_path_policy as gpp
from fake_lsp_server import make_fake_command
from gui_bridge import ApiBridge
from core.ide.lsp_manager import LspManager, path_to_uri, uri_to_path


def _manager(tmp_path):
    command = make_fake_command(tmp_path)

    def _resolve(server_id):
        return {
            "server": server_id,
            "executable": command[0],
            "extra_args": command[1:],
            "source": "path",
            "msg": "",
        }

    return LspManager(provision=_resolve, restart_backoff=0.0)


def _make_bridge(workdir, tmp_path, monkeypatch, manager=None):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))
    bridge = ApiBridge(mcp_manager=None)
    bridge._session_workdir = lambda session_name="": workdir  # type: ignore[method-assign]
    bridge._current_session = "test-session"
    if manager is not None:
        bridge._lsp_manager = manager
    return bridge


def _wait_for(getter, predicate, timeout=10.0, interval=0.02):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = getter()
        if predicate(last):
            return last
        time.sleep(interval)
    return last


def test_lsp_request_forwards_to_server(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, manager)
    try:
        res = bridge.lsp_request(str(f), "test/echo", {"v": 1})
        assert res["ok"] is True
        assert res["result"] == {"echo": {"v": 1}}
    finally:
        manager.recycle_all()


def test_lsp_request_rejects_file_without_language_server(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "notes.txt"
    f.write_text("hi\n", encoding="utf-8")
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, _manager(tmp_path))
    res = bridge.lsp_request(str(f), "test/echo", {})
    assert res["ok"] is False
    assert "语言服务器" in res["error"]["message"]


def test_lsp_request_maps_server_error_code(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, manager)
    try:
        res = bridge.lsp_request(str(f), "test/error", {})
        assert res["ok"] is False
        assert res["error"]["code"] == -32602
        assert res["error"]["message"] == "bad params"
    finally:
        manager.recycle_all()


def test_lsp_request_denied_for_credential_config(tmp_path, monkeypatch):
    ydata = tmp_path / "ydata"
    ydata.mkdir()
    f = ydata / "config.json"
    f.write_text('{"api_key": "secret"}', encoding="utf-8")
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(ydata))
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: True)
    bridge = _make_bridge("GLOBAL", tmp_path, monkeypatch, _manager(tmp_path))
    res = bridge.lsp_request(str(f), "test/echo", {})
    assert res["ok"] is False


def test_lsp_notify_starts_server_and_pushes_diagnostics(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, manager)
    events = []
    bridge.emit_event = lambda name, payload=None: events.append((name, payload))
    try:
        res = bridge.lsp_notify(str(f), "test/publishDiagnostics", {"uri": path_to_uri(str(f))})
        assert res["ok"] is True
        batcher = bridge._lsp_batcher_instance()
        _wait_for(lambda: batcher._pending, lambda p: len(p) >= 1)
        batcher.flush()
        diag_events = [e for e in events if e[0] == "lsp_diagnostics"]
        assert len(diag_events) == 1
        payload = diag_events[0][1]
        assert os.path.normcase(payload["path"]) == os.path.normcase(str(f))
        assert payload["diagnostics"][0]["message"] == "undefined name x"
    finally:
        manager.recycle_all()


def test_lsp_status_event_is_emitted_on_start(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, manager)
    events = []
    bridge.emit_event = lambda name, payload=None: events.append((name, payload))
    try:
        bridge.lsp_request(str(f), "test/echo", {})
        statuses = _wait_for(
            lambda: [e[1] for e in events if e[0] == "lsp_status"],
            lambda rows: any(r["state"] == "running" for r in rows),
        )
        assert any(r["state"] == "starting" for r in statuses)
        assert any(r["server"] == "jedi" for r in statuses)
    finally:
        manager.recycle_all()


def test_lsp_server_status_lists_every_configured_language(tmp_path, monkeypatch):
    from core.ide import server_provision

    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        server_provision,
        "resolve_executable",
        lambda server_id: {
            "server": server_id,
            "executable": "/fake/" + server_id,
            "extra_args": [],
            "source": "path",
            "msg": "",
        },
    )
    server_provision.invalidate_cache()
    ws = tmp_path / "ws"
    ws.mkdir()
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch)
    res = bridge.lsp_server_status()
    assert res["ok"] is True
    languages = [row["language"] for row in res["servers"]]
    assert "python" in languages
    assert "typescript" in languages
    assert all(row["executable"] for row in res["servers"])


def test_lsp_server_status_shows_missing_with_guidance(tmp_path, monkeypatch):
    from core.ide import server_provision

    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(server_provision, "invalidate_cache", lambda: None)
    monkeypatch.setattr(
        server_provision,
        "resolve_executable",
        lambda server_id: {
            "server": server_id,
            "executable": "",
            "extra_args": [],
            "source": "missing",
            "msg": "未找到 " + server_id,
        },
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch)
    res = bridge.lsp_server_status()
    assert res["ok"] is True
    assert all(row["state"] == "missing" for row in res["servers"])
    assert all("未找到" in row["msg"] for row in res["servers"])


def test_lsp_set_server_path_rejects_bad_path(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, _manager(tmp_path))
    res = bridge.lsp_set_server_path("jedi", str(tmp_path / "nope"))
    assert res["ok"] is False
    assert "路径" in res["msg"]


# ---------------------------------------------------------------------------
# 事件派发的线程正确性：evaluate_js 只能落在 gui-js-dispatcher 单消费者线程
# ---------------------------------------------------------------------------
def test_lsp_diagnostics_dispatch_happens_on_js_dispatcher_thread(tmp_path, monkeypatch):
    """LSP 读线程只 push 进合批器；真正投递前端的事件必须由 dispatcher 线程 evaluate_js。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, manager)
    evaluated = []

    class _FakeWindow:
        def evaluate_js(self, script):
            evaluated.append((threading.current_thread().name, script))

    bridge._window = _FakeWindow()
    try:
        res = bridge.lsp_notify(str(f), "test/publishDiagnostics", {"uri": path_to_uri(str(f))})
        assert res["ok"] is True
        batcher = bridge._lsp_batcher_instance()
        _wait_for(lambda: batcher._pending, lambda p: len(p) >= 1)
        batcher.flush()
        dispatched = _wait_for(
            lambda: evaluated,
            lambda rows: any("lsp_diagnostics" in script for _, script in rows),
        )
    finally:
        manager.recycle_all()
    threads = {name for name, _ in dispatched}
    # 事件确实被投递出去（经 _js_queue 单消费者到达前端）
    assert any("lsp_diagnostics" in script for _, script in dispatched)
    # 且唯一调用 evaluate_js 的线程就是 gui-js-dispatcher——绝不在 lsp-read-* 上
    assert threads == {"gui-js-dispatcher"}
    assert not any(name.startswith("lsp-read") for name in threads)


# ---------------------------------------------------------------------------
# 评审项 A：文档关闭是诊断去重表的唯一清理点
# ---------------------------------------------------------------------------
def test_lsp_notify_didClose_discards_batcher_state(tmp_path, monkeypatch):
    """didClose 必须同时清掉待派发内容与去重指纹，否则去重表随打开过的文件无界累积。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, manager)
    bridge.emit_event = lambda name, payload=None: None
    batcher = bridge._lsp_batcher_instance()
    # 用与服务端推送一致的键（uri 反解路径）灌入诊断并刷新一次以写入去重指纹
    key = uri_to_path(path_to_uri(str(f)))
    batcher.push(key, [{"message": "boom"}])
    batcher.flush()
    batcher.push(key, [{"message": "boom"}])
    assert key in batcher._pending
    assert key in batcher._last_flushed
    try:
        res = bridge.lsp_notify(
            str(f), "textDocument/didClose", {"textDocument": {"uri": path_to_uri(str(f))}}
        )
        assert res["ok"] is True
    finally:
        manager.recycle_all()
    assert key not in batcher._pending
    assert key not in batcher._last_flushed
