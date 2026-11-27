import json
import threading
import time

import pytest

import config
from core import app_context, credentials, lan_peers
from core.harness.tool_pipeline import _REPEAT_GUARD_EXEMPT_TOOLS
from core.lan_server import LanServer
from core.mcp_server import McpServer
from core.tool_registry import build_default_registry


@pytest.fixture(autouse=True)
def _isolated_peers(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "YCODE_DIR", str(tmp_path))
    lan_peers.lan_peer_manager.reset_for_tests()
    yield
    lan_peers.lan_peer_manager.reset_for_tests()


def test_peer_crud_encrypts_token_and_redacts_output(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "protect_secret", lambda v: f"enc:{v}")
    monkeypatch.setattr(credentials, "unprotect_secret", lambda v: v[4:] if v.startswith("enc:") else v)

    saved = lan_peers.save_peer({
        "id": "pc-b",
        "name": "B电脑",
        "url": "http://192.168.1.25:8765/a2a",
        "token": "super-secret",
        "description": "测试主机",
    })
    assert saved["id"] == "pc-b"
    assert saved["has_token"] is True
    assert saved["token_plaintext"] is False
    assert "token" not in saved

    raw_file = json.loads((tmp_path / "lan_peers.json").read_text(encoding="utf-8"))
    assert raw_file["peers"][0]["token"] == "enc:super-secret"

    # 编辑留空 token 应保留原加密 token
    updated = lan_peers.save_peer({
        "id": "pc-b",
        "name": "B电脑-更新",
        "url": "http://192.168.1.25:8765/a2a",
        "token": "",
    })
    assert updated["has_token"] is True
    assert lan_peers.get_peer("pc-b", include_secret=True)["token"] == "super-secret"

    assert lan_peers.delete_peer("pc-b") is True
    assert lan_peers.list_peers() == []


def test_save_peer_plaintext_fallback_when_encrypt_fails(monkeypatch):
    def _boom(_v):
        raise RuntimeError("no keyring")

    monkeypatch.setattr(credentials, "protect_secret", _boom)
    saved = lan_peers.save_peer({
        "id": "pc-plain",
        "name": "明文降级节点",
        "url": "http://192.168.1.26:8765/a2a",
        "token": "raw-tok",
    })
    assert saved["has_token"] is True
    assert saved["token_plaintext"] is True


def test_probe_and_send_over_real_a2a_server():
    class FakeB:
        def __init__(self):
            self.srv = None
            self.calls = []

        def send_user_message(self, text, session_name=None, **_kw):
            self.calls.append((text, session_name))
            sess = session_name or "a2a-remote"

            def _bg():
                time.sleep(0.05)
                self.srv.on_event("turn_start", {"session_name": sess})
                self.srv.on_event(
                    "message_final",
                    {"session_name": sess, "role": "assistant", "content": f"B已完成:{text}"},
                )
                self.srv.on_event("turn_finish", {"session_name": sess})

            threading.Thread(target=_bg, daemon=True).start()
            return {"ok": True}

    bridge_b = FakeB()
    srv = McpServer(bridge=bridge_b, port=0, host="127.0.0.1", token_provider=lambda: "tok-b")
    bridge_b.srv = srv
    assert srv.start()["ok"]
    try:
        lan_peers.save_peer({
            "id": "b-a2a",
            "name": "B星瑶A2A",
            "url": f"http://127.0.0.1:{srv.port}/a2a",
            "token": "tok-b",
        })
        probe = lan_peers.probe_peer("b-a2a")
        assert probe["ok"] is True
        assert probe["online"] is True
        assert probe["agent_name"] == "星瑶 (Y-code)"

        app_context.set_thread_session("origin-sess-1")
        try:
            res = lan_peers.lan_peer_manager.send_to_peer("b-a2a", "查看git状态", wait_seconds=5)
        finally:
            app_context.set_thread_session(None)
        assert res["ok"] is True
        assert res["status"] == "completed"
        assert res["reply"] == "B已完成:查看git状态"
        assert bridge_b.calls[0][0] == "查看git状态"
    finally:
        srv.stop()


def test_send_over_real_lan_ws_server(monkeypatch):
    import core.lan_server as lan_mod

    monkeypatch.setattr(lan_mod, "_save_config_value", lambda *a, **k: None)

    class FakeWsBridge:
        def __init__(self):
            self.lan = None
            self.calls = []

        def send_user_message(self, text, session_name=None, **_kw):
            self.calls.append((text, session_name))
            sess = session_name or "peer-a"

            def _bg():
                time.sleep(0.05)
                self.lan.broadcast(
                    json.dumps(
                        {"type": "message_final", "data": {"session_name": sess, "role": "assistant", "content": "WS回传内容"}},
                        ensure_ascii=False,
                    ),
                    event_type="message_final",
                )
                self.lan.broadcast(
                    json.dumps({"type": "turn_finish", "data": {"session_name": sess}}, ensure_ascii=False),
                    event_type="turn_finish",
                )

            threading.Thread(target=_bg, daemon=True).start()
            return {"ok": True}

    b_bridge = FakeWsBridge()
    lan = LanServer(bridge=b_bridge, port=0, bind="127.0.0.1")
    b_bridge.lan = lan
    info = lan.start(strict_port=True)
    assert info["ok"]
    token = lan._issue_token()
    try:
        lan_peers.save_peer({
            "id": "b-ws",
            "name": "B星瑶WS",
            "url": f"ws://127.0.0.1:{lan.port}/",
            "protocol": "lan_ws",
            "token": token,
        })
        probe = lan_peers.probe_peer("b-ws")
        assert probe["ok"] is True
        assert probe["protocol"] == "lan_ws"

        res = lan_peers.lan_peer_manager.send_to_peer("b-ws", "检查磁盘", wait_seconds=5)
        assert res["ok"] is True
        assert res["status"] == "completed"
        assert res["reply"] == "WS回传内容"
    finally:
        lan.stop()


def test_short_wait_timeout_triggers_background_callback():
    class SlowB:
        def __init__(self):
            self.srv = None

        def send_user_message(self, text, session_name=None, **_kw):
            sess = session_name or "a2a-remote"

            def _bg():
                time.sleep(0.35)
                self.srv.on_event(
                    "message_final",
                    {"session_name": sess, "role": "assistant", "content": "异步后台完成结果"},
                )
                self.srv.on_event("turn_finish", {"session_name": sess})

            threading.Thread(target=_bg, daemon=True).start()
            return {"ok": True}

    b = SlowB()
    srv = McpServer(bridge=b, port=0, host="127.0.0.1", token_provider=lambda: "tok-slow")
    b.srv = srv
    assert srv.start()["ok"]

    done_evt = threading.Event()
    received = []

    def _cb(record):
        received.append(record)
        done_evt.set()

    try:
        lan_peers.lan_peer_manager.set_task_callback(_cb)
        lan_peers.save_peer({
            "id": "b-slow",
            "name": "B慢任务",
            "url": f"http://127.0.0.1:{srv.port}/a2a",
            "token": "tok-slow",
        })
        app_context.set_thread_session("sess-origin-a")
        try:
            res = lan_peers.lan_peer_manager.send_to_peer(
                "b-slow", "跑长耗时编译", wait_seconds=0, auto_report=True
            )
        finally:
            app_context.set_thread_session(None)

        assert res["status"] == "working"
        assert res["watching"] is True
        assert done_evt.wait(timeout=5.0), "后台监听应在任务完成后触发回传回调"
        assert received[0]["origin_session"] == "sess-origin-a"
        assert received[0]["status"] == "completed"
        assert received[0]["reply"] == "异步后台完成结果"
    finally:
        srv.stop()


def test_loop_prevention_blocks_nested_remote_send():
    lan_peers.save_peer({
        "id": "b-loop",
        "name": "循环测试",
        "url": "http://127.0.0.1:65500/a2a",
        "token": "t",
    })
    for source in ("mcp", "lan"):
        app_context.set_thread_turn_source(source)
        try:
            res = lan_peers.lan_peer_manager.send_to_peer("b-loop", "再转发回去")
            assert res["ok"] is False
            assert res["status"] == "rejected"
            assert "循环派活" in res["msg"]
        finally:
            app_context.set_thread_turn_source(None)


def test_builtin_tools_registered_and_poll_exempt():
    registry = build_default_registry()
    names = {spec.name for spec in registry.all()}
    assert {
        "lan_peer_list",
        "lan_peer_inspect",
        "lan_peer_send",
        "lan_peer_poll",
        "lan_peer_approve",
        "lan_peer_manage",
    }.issubset(names)
    assert "lan_peer_poll" in _REPEAT_GUARD_EXEMPT_TOOLS


def test_lan_server_forbids_peer_management_methods():
    srv = LanServer(bridge=None)
    for method in (
        "get_lan_peers",
        "save_lan_peer",
        "delete_lan_peer",
        "test_lan_peer",
        "get_lan_peer_local_info",
    ):
        res = srv.dispatch_rpc(method, {})
        assert res == {"error": {"code": "method_not_allowed"}}


def test_gui_bridge_skips_lan_peer_summary_when_session_cancelled(monkeypatch):
    from gui_bridge import ApiBridge

    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._user_cancelled_sessions = {"sess-cancel"}
    bridge._turn_start_lock = threading.Lock()
    bridge._active_turns = {}
    emitted = []
    bridge.emit_event = lambda etype, data: emitted.append((etype, data))

    spawned = []
    monkeypatch.setattr(
        bridge,
        "_start_lan_peer_summary",
        lambda *a, **k: spawned.append((a, k)),
    )
    bridge._on_lan_peer_task_update({
        "local_task_id": "ltask-1",
        "peer_id": "b",
        "peer_name": "B",
        "status": "completed",
        "origin_session": "sess-cancel",
        "reply": "ok",
    })
    assert len(emitted) == 1 and emitted[0][0] == "lan_peer_task_updated"
    assert spawned == []


def test_peer_inspect_manage_and_binding_persistence(tmp_path, monkeypatch):
    from core import credentials
    from core.memory import MemoryManager

    monkeypatch.setattr(credentials, "protect_secret", lambda v: f"enc:{v}")
    monkeypatch.setattr(credentials, "unprotect_secret", lambda v: v[4:] if v.startswith("enc:") else v)

    ws_dir = tmp_path / "nb_ws"
    ws_dir.mkdir()
    mem = MemoryManager(db_path=str(tmp_path / "b.db"))
    mem.set_workdir(str(ws_dir))
    mem.add_workspace(str(ws_dir))
    mem.ensure_session("chat-old", workdir=str(ws_dir), source="GUI")

    class PeerBBridge:
        _memory = mem
        _current_session = "b-fg"
        sent = []

        def _load_config(self):
            return {
                "active_model_id": "m1",
                "providers": [{
                    "id": "p1",
                    "name": "P1",
                    "base_url": "https://api.example.com",
                    "api_key": "secret-k",
                    "models": [
                        {"id": "m1", "name": "Model-1", "context_window": 64000},
                        {"id": "m2", "name": "Model-2", "context_window": 128000},
                    ],
                }],
            }

        def set_model(self, mid, session_name=None):
            if session_name:
                mem.set_session_model_id(session_name, mid)
            return {"ok": True}

        def send_user_message(self, text, session_name=None, **_kw):
            self.sent.append((text, session_name))
            sess = session_name or "a2a-remote"

            def _bg():
                time.sleep(0.03)
                srv.on_event("turn_start", {"session_name": sess})
                srv.on_event("message_final", {"session_name": sess, "role": "assistant", "content": f"ok:{sess}"})
                srv.on_event("turn_finish", {"session_name": sess})

            threading.Thread(target=_bg, daemon=True).start()
            return {"ok": True}

    b = PeerBBridge()
    srv = McpServer(bridge=b, port=0, host="127.0.0.1", token_provider=lambda: "tok-nb")
    assert srv.start()["ok"]
    try:
        lan_peers.save_peer({
            "id": "nb",
            "name": "笔记本",
            "url": f"http://127.0.0.1:{srv.port}/a2a",
            "token": "tok-nb",
        })
        out_ws = lan_peers.tool_lan_peer_inspect("笔记本", target="workspaces")
        assert "nb_ws" in out_ws

        out_create = lan_peers.tool_lan_peer_manage(
            "笔记本",
            action="create_session",
            workdir=str(ws_dir),
            session_name="pinned-chat",
        )
        assert "pinned-chat" in out_create

        # 持久化绑定必须保留已加密令牌且写回 default_session/default_workdir
        peer_cfg = lan_peers.get_peer("nb", include_secret=True)
        assert peer_cfg["default_session"] == "pinned-chat"
        assert peer_cfg["token"] == "tok-nb"

        out_model = lan_peers.tool_lan_peer_manage("笔记本", action="select_model", model="Model-2")
        assert "Model-2" in out_model
        assert mem.get_session_model_id("pinned-chat") == "m2"

        # 后续 send_to_peer 未指定 session_name 时自动复用 pinned-chat
        res = lan_peers.lan_peer_manager.send_to_peer("笔记本", "hello", wait_seconds=5)
        assert res["status"] == "completed"
        assert b.sent[-1][1] == "pinned-chat"
    finally:
        srv.stop()
        mem.close()


def test_watcher_does_not_duplicate_callback_after_manual_poll():
    mgr = lan_peers.lan_peer_manager
    callbacks = []
    mgr.set_task_callback(lambda rec: callbacks.append(dict(rec)))

    class _DoneBridge:
        def send_user_message(self, text, session_name=None, **_kw):
            return {"ok": True}

    srv = McpServer(bridge=_DoneBridge(), port=0, host="127.0.0.1", token_provider=lambda: "tok-d")
    assert srv.start()["ok"]
    try:
        lan_peers.save_peer({
            "id": "b-dup",
            "name": "去重节点",
            "url": f"http://127.0.0.1:{srv.port}/a2a",
            "token": "tok-d",
        })
        app_context.set_thread_session("orig-s")
        try:
            res = mgr.send_to_peer("b-dup", "task", wait_seconds=0, auto_report=True)
        finally:
            app_context.set_thread_session(None)
        tid = res["local_task_id"]
        poll_results = []
        t_poll = threading.Thread(
            target=lambda: poll_results.append(mgr.poll_task(tid, wait_seconds=5, from_watcher=False)),
            daemon=True,
        )
        t_poll.start()
        time.sleep(0.05)

        # 模拟服务端完成：人工 poll 已在场等待，不应再触发后台回调二次汇报
        srv.on_event("message_final", {"session_name": res["remote_session"], "role": "assistant", "content": "done!"})
        srv.on_event("turn_finish", {"session_name": res["remote_session"]})

        t_poll.join(timeout=3.0)
        assert poll_results and poll_results[0]["status"] == "completed"
        time.sleep(0.2)
        assert callbacks == []
    finally:
        srv.stop()
