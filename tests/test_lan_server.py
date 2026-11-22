import json
import socket
import threading
import time

import pytest

from core.lan_server import LanServer
from gui_bridge import ApiBridge


@pytest.fixture(autouse=True)
def _no_config_writes(monkeypatch):
    import core.lan_server as lan_mod

    monkeypatch.setattr(lan_mod, "_save_config_value", lambda *a, **k: None)


def _register_client(srv, ws):
    srv._clients.append(ws)
    srv._client_locks[ws] = threading.Lock()
    return ws


def test_detect_lan_ip_non_loopback():
    ip = LanServer(bridge=None)._detect_lan_ip()
    assert ip and ip != "127.0.0.1"


def test_bind_url_embeds_token():
    srv = LanServer(bridge=None)
    srv.port = 8765
    srv.lan_ip = "192.168.1.5"
    assert srv.bind_url("abc123") == "http://192.168.1.5:8765/?token=abc123"


def test_issue_and_verify_token():
    srv = LanServer(bridge=None)
    srv._ttl = 1
    t = srv._issue_token()
    assert srv._verify_token(t)
    srv._token_expires = time.time() - 1
    assert not srv._verify_token(t)


def test_broadcast_sends_to_clients():
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, fake)
    srv.broadcast('{"type":"token_delta"}')
    assert fake.sent == ['{"type":"token_delta"}']


def test_route_rejects_window_close():
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, fake)
    srv._route_message(fake, json.dumps({"method": "window_close", "args": {}, "id": 1}))
    assert "method not allowed" in fake.sent[0]


def test_route_allows_non_forbidden_method():
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, fake)
    srv._route_message(fake, json.dumps({"method": "get_background_tasks", "args": {}, "id": 1}))
    assert "method not allowed" not in fake.sent[0]
    resp = json.loads(fake.sent[0])
    assert resp["id"] == 1


def test_route_allows_get_initial_state():
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, fake)
    srv._route_message(fake, json.dumps({"method": "get_initial_state", "args": [], "id": 1}))
    resp = json.loads(fake.sent[0])
    assert "method not allowed" not in fake.sent[0]
    assert resp["id"] == 1


def test_route_allows_whitelisted_send():
    srv = LanServer(bridge=None)
    calls = []

    class B:
        def send_user_message(self, text, **kw):
            calls.append(text)
            return {"ok": True}

    srv.bridge = B()
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, fake)
    srv._route_message(fake, json.dumps({"method": "send_user_message", "args": {"text": "hi"}, "id": 1}))
    assert "method not allowed" not in fake.sent[0]
    resp = json.loads(fake.sent[0])
    assert resp["result"]["ok"] is True
    assert calls == ["hi"]


def test_call_bridge_positional_args():
    srv = LanServer(bridge=None)
    class B:
        def echo(self, a, b=None): return {"ok": True, "a": a, "b": b}
    srv.bridge = B()
    assert srv._call_bridge("echo", [1, 2]) == {"ok": True, "a": 1, "b": 2}


def test_serve_shim_returns_js():
    srv = LanServer(bridge=None)
    resp = srv._serve_shim()
    assert resp.status_code == 200
    assert b"pywebview" in resp.body


def test_serve_shim_falls_back_when_file_missing(monkeypatch):
    import core.lan_server as lan_mod

    srv = LanServer(bridge=None)
    monkeypatch.setattr(
        lan_mod.LanServer, "_dist_dir", staticmethod(lambda: "Z:/nonexistent")
    )
    monkeypatch.setattr(
        lan_mod.os.path,
        "join",
        lambda *parts: (
            "Z:/nonexistent/frontend/mobile-shim.js"
            if parts[-1] == "mobile-shim.js"
            else lan_mod.os.path.join(*parts)
        ),
    )
    resp = srv._serve_shim()
    assert resp.status_code == 200
    assert b"pywebview" in resp.body
    assert b"WebSocket" in resp.body


def test_get_bind_info_returns_url_token_qr():
    srv = LanServer(bridge=None)
    srv.port = 8765
    srv.lan_ip = "192.168.1.5"
    info = srv.get_bind_info()
    assert info["ok"]
    assert info["url"].startswith("http://192.168.1.5:8765/?token=")
    assert info["token"]
    assert info["qr"].startswith("data:image/png;base64,")
    assert 0 < info["expires_in"] <= srv._ttl


def test_ensure_lan_server_concurrent_single_instance(monkeypatch):
    import core.lan_server as lan_mod
    import config as config_mod
    from gui_bridge import ApiBridge

    bridge = ApiBridge.__new__(ApiBridge)
    bridge._lan_server = None
    bridge._lan_lock = threading.Lock()

    created = []
    created_lock = threading.Lock()

    class FakeLanServer:
        def __init__(self, bridge=None):
            with created_lock:
                created.append(self)
            self.port = 0

        def set_ttl(self, seconds):
            pass

        def start(self):
            self.port = 8765

    monkeypatch.setattr(lan_mod, "LanServer", FakeLanServer)
    monkeypatch.setattr(config_mod, "_load", lambda: {})

    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(bridge._ensure_lan_server())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(created) == 1
    assert all(r is bridge._lan_server for r in results)
    assert bridge._lan_server.port == 8765


def _fake_config(monkeypatch, data):
    import config as config_mod
    import core.lan_server as lan_mod

    monkeypatch.setattr(config_mod, "_load", lambda: dict(data))
    monkeypatch.setattr(
        lan_mod, "_save_config_value", lambda k, v: data.__setitem__(k, v) or True
    )
    return data


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_load_persisted_port_validates_range(monkeypatch):
    data = _fake_config(monkeypatch, {"remote_pair_port": 49939})
    assert LanServer(bridge=None)._load_persisted_port() == 49939

    for bad in (0, 80, 1023, 70000, "abc", None):
        _fake_config(monkeypatch, {"remote_pair_port": bad})
        assert LanServer(bridge=None)._load_persisted_port() == 0
    _fake_config(monkeypatch, {})
    assert LanServer(bridge=None)._load_persisted_port() == 0


def test_start_reuses_persisted_port(monkeypatch):
    port = _free_port()
    data = _fake_config(monkeypatch, {"remote_pair_port": port})
    srv = LanServer(bridge=None)
    try:
        res = srv.start()
        assert res["ok"]
        assert srv.port == port
        assert data["remote_pair_port"] == port
    finally:
        srv.stop()


def test_start_falls_back_when_persisted_port_occupied(monkeypatch):
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    occupied = blocker.getsockname()[1]
    data = _fake_config(monkeypatch, {"remote_pair_port": occupied})
    srv = LanServer(bridge=None)
    try:
        res = srv.start()
        assert res["ok"]
        assert srv.port != occupied
        assert data["remote_pair_port"] == srv.port
    finally:
        srv.stop()
        blocker.close()


class _FakeClient:

    def __init__(self):
        self.sent = []
        self.closed = []

    def send(self, payload):
        self.sent.append(payload)

    def close(self, code=None, reason=None):
        self.closed.append((code, reason))


def test_issue_token_kicks_connected_clients():
    srv = LanServer(bridge=None)
    c1, c2 = _FakeClient(), _FakeClient()
    _register_client(srv, c1)
    _register_client(srv, c2)

    srv._issue_token()

    assert srv._clients == []
    assert c1.closed and c2.closed
    assert c1.closed[0][0] == 4401
    assert c2.closed[0][0] == 4401


def test_refresh_bind_info_disconnects_old_clients():
    srv = LanServer(bridge=None)
    srv.port = 8765
    srv.lan_ip = "192.168.1.5"
    old = srv.get_bind_info()
    client = _FakeClient()
    _register_client(srv, client)

    new = srv.get_bind_info(force_new=True)

    assert new["token"] != old["token"]
    assert client.closed and client.closed[0][0] == 4401
    assert srv._clients == []
    assert not srv._verify_token(old["token"])
    assert srv._verify_token(new["token"])


def test_register_rejects_old_token_after_rotation():
    srv = LanServer(bridge=None)
    old = srv._issue_token()
    srv._issue_token()

    stale = _FakeClient()
    assert srv._authenticate_and_register(stale, old) is False
    assert srv._clients == []

    fresh = _FakeClient()
    assert srv._authenticate_and_register(fresh, srv._token.decode()) is True
    assert srv._clients == [fresh]


class _RecordingBridge:

    def __init__(self):
        self.last_turn_source = "unset"
        self.last_permission_mode = "unset"
        self.message_calls = []

    def send_user_message(self, text, session_name=None, attachments=None,
                          workdir=None, model_id=None, permission_mode=None,
                          turn_source="local"):
        self.last_turn_source = turn_source
        self.last_permission_mode = permission_mode
        self.message_calls.append((text, turn_source))
        return {"ok": True}

    def get_computer_control_status(self):
        return {"ok": True}

    def stop_computer_control(self, run_id):
        return {"ok": True}


@pytest.fixture
def bridge():
    return _RecordingBridge()


@pytest.fixture
def server(bridge):
    return LanServer(bridge=bridge)


@pytest.mark.parametrize("method", [
    "set_browser_control_enabled",
    "set_computer_control_enabled",
    "set_computer_browser",
    "pause_computer_control",
    "resume_computer_control",
    "acknowledge_computer_safety",
])
def test_lan_forbids_computer_permission_expansion(server, method):
    response = server.dispatch_rpc(method, [])
    assert response["error"]["code"] == "method_not_allowed"


def test_lan_forbids_drain_js_queue(server):
    response = server.dispatch_rpc("drain_js_queue", [])
    assert response["error"]["code"] == "method_not_allowed"


@pytest.mark.parametrize("method", [
    "ide_write_file",
    "ide_set_open_files",
    "ide_save_layout",
])
def test_lan_forbids_ide_state_mutating_methods(server, method):
    response = server.dispatch_rpc(method, [])
    assert response["error"]["code"] == "method_not_allowed"


@pytest.mark.parametrize("method", [
    "pty_terminal_write",
    "pty_terminal_kill",
    "lsp_set_server_path",
])
def test_lan_forbids_pty_write_kill_and_lsp_set_server_path(server, method):
    response = server.dispatch_rpc(
        method, ["rm -rf /\r"] if method == "pty_terminal_write" else []
    )
    assert response["error"]["code"] == "method_not_allowed"


def test_lan_allows_pty_read_only_methods():
    calls = []

    class PtyBridge:
        def pty_terminal_ensure(self, cols=80, rows=24):
            calls.append(("ensure", cols, rows))
            return {"ok": True, "pid": 4321, "cwd": "E:\\ws", "reused": True}

        def pty_terminal_resize(self, cols=80, rows=24):
            calls.append(("resize", cols, rows))
            return {"ok": True}

    srv = LanServer(bridge=PtyBridge())
    for method, args in (
        ("pty_terminal_ensure", [100, 30]),
        ("pty_terminal_resize", [120, 40]),
    ):
        result = srv.dispatch_rpc(method, args)
        # 绝不能返回 method_not_allowed：只读/查看类方法必须真正到达 bridge
        assert "error" not in result
        assert result["ok"] is True
    assert [c[0] for c in calls] == ["ensure", "resize"]


@pytest.mark.parametrize("method", ["get_computer_control_status", "stop_computer_control"])
def test_lan_allows_status_and_emergency_stop(server, method):
    assert server.dispatch_rpc(method, ["run-a"] if method.startswith("stop") else [])["ok"] is True


def test_lan_user_message_cannot_override_turn_source(server, bridge):
    server.dispatch_rpc("send_user_message", [{"text": "use computer", "turn_source": "local"}])
    assert bridge.last_turn_source == "lan"


def test_lan_user_message_kwargs_payload_turn_source_overridden(server, bridge):
    result = server.dispatch_rpc(
        "send_user_message", {"text": "use computer", "turn_source": "local"}
    )
    assert result["ok"] is True
    assert bridge.last_turn_source == "lan"


def test_lan_plain_text_message_gets_lan_turn_source(server, bridge):
    result = server.dispatch_rpc("send_user_message", ["use computer"])
    assert result["ok"] is True
    assert bridge.last_turn_source == "lan"


def test_lan_payload_dict_args_without_turn_source_binds_lan(server, bridge):
    result = server.dispatch_rpc("send_user_message", [{"text": "use computer"}])
    assert result["ok"] is True
    assert bridge.last_turn_source == "lan"


def test_lan_payload_dict_with_positional_turn_source_stripped(server, bridge):
    result = server.dispatch_rpc(
        "send_user_message",
        [{"text": "use computer"}, None, None, None, None, "local"],
    )
    assert result["ok"] is True
    assert bridge.last_turn_source == "lan"


def test_lan_positional_turn_source_cannot_reach_bridge():
    calls = []

    class StrictBridge:
        def send_user_message(self, text, session_name=None, attachments=None,
                              workdir=None, model_id=None, turn_source="local"):
            calls.append(turn_source)
            return {"ok": True}

    srv = LanServer(bridge=StrictBridge())
    result = srv.dispatch_rpc(
        "send_user_message", ["hi", None, None, None, None, "local"]
    )
    assert result["ok"] is True
    assert calls == ["lan"]


def test_lan_kwargs_permission_mode_stripped(server, bridge):
    result = server.dispatch_rpc(
        "send_user_message",
        {"text": "hi", "session_name": "s1", "permission_mode": "full"},
    )
    assert result["ok"] is True
    assert bridge.last_permission_mode is None
    assert bridge.last_turn_source == "lan"


def test_lan_kwargs_unknown_keys_stripped(server, bridge):
    result = server.dispatch_rpc("send_user_message", {"text": "hi", "evil": 1})
    assert result["ok"] is True
    assert bridge.message_calls[-1][0] == "hi"


def test_lan_payload_dict_permission_mode_stripped(server, bridge):
    result = server.dispatch_rpc(
        "send_user_message", [{"text": "x", "permission_mode": "full"}]
    )
    assert result["ok"] is True
    text = bridge.message_calls[-1][0]
    assert isinstance(text, dict)
    assert "permission_mode" not in text
    assert bridge.last_permission_mode is None
    assert bridge.last_turn_source == "lan"


def test_safe_send_fail_closed_for_unregistered_ws():
    srv = LanServer(bridge=None)
    fake = _FakeClient()
    assert srv._safe_send(fake, "x") is False
    assert fake.sent == []


def test_lan_forbids_private_underscore_methods():
    calls = []

    class PrivateSeamBridge:
        def _queue_js_payload(self, payload):
            calls.append(payload)
            return {"ok": True}

        def _rebind(self):
            calls.append("rebind")
            return {"ok": True}

    srv = LanServer(bridge=PrivateSeamBridge())
    assert srv.dispatch_rpc("_queue_js_payload", ["evil js"]) == {
        "error": {"code": "method_not_allowed"}
    }
    assert srv.dispatch_rpc("_rebind", []) == {
        "error": {"code": "method_not_allowed"}
    }
    assert calls == []


def test_route_rejects_private_underscore_method():
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, fake)
    srv._route_message(
        fake,
        json.dumps({"method": "_queue_js_payload", "args": ["evil js"], "id": 11}),
    )
    assert "method not allowed" in fake.sent[0]


def test_route_rejects_computer_permission_mutation():
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, fake)
    srv._route_message(
        fake, json.dumps({"method": "set_computer_control_enabled", "args": [True], "id": 7})
    )
    assert "method not allowed" in fake.sent[0]


def test_route_injects_lan_turn_source_on_user_message():
    calls = []

    class B:
        def send_user_message(self, text, **kw):
            calls.append((text, kw.get("turn_source")))
            return {"ok": True}

    srv = LanServer(bridge=B())
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, fake)
    srv._route_message(
        fake,
        json.dumps({"method": "send_user_message", "args": {"text": "hi", "turn_source": "local"}, "id": 1}),
    )
    resp = json.loads(fake.sent[0])
    assert resp["result"]["ok"] is True
    assert calls == [("hi", "lan")]


def _make_bare_message_bridge(is_turn_active):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._current_session = "s1"
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._cancel_events = {}
    bridge._is_turn_active = is_turn_active
    spawned = []
    bridge._spawn_turn_worker = lambda *a, **k: spawned.append((a, k))
    events = []
    bridge.emit_event = lambda etype, payload: events.append((etype, payload))
    return bridge, spawned, events


def test_bridge_rejects_uncontrolled_turn_source():
    bridge, spawned, _ = _make_bare_message_bridge(lambda s: False)
    res = bridge.send_user_message("hi", turn_source="remote")
    assert res["ok"] is False
    assert spawned == []


def test_bridge_local_call_keeps_default_local_turn_source():
    bridge, spawned, _ = _make_bare_message_bridge(lambda s: False)
    assert bridge.send_user_message("hi") == {"ok": True}
    assert spawned[0][1]["turn_source"] == "local"


def test_bridge_passes_lan_turn_source_to_turn_worker():
    bridge, spawned, _ = _make_bare_message_bridge(lambda s: False)
    assert bridge.send_user_message("hi", turn_source="lan") == {"ok": True}
    assert spawned[0][1]["turn_source"] == "lan"


def test_bridge_queued_message_preserves_lan_turn_source():
    bridge, _, _ = _make_bare_message_bridge(lambda s: True)
    assert bridge.send_user_message("hi", turn_source="lan") == {"ok": True, "queued": True}
    assert bridge._message_queues["s1"][0]["turn_source"] == "lan"


@pytest.mark.parametrize("method", [
    ["send_user_message"],
    123,
    {"method": "send_user_message"},
    None,
])
def test_dispatch_rpc_rejects_non_string_method_without_type_error(server, method):
    response = server.dispatch_rpc(method, [])
    assert response == {"error": {"code": "method_not_allowed"}}


def test_broadcast_concurrent_thread_safety():
    srv = LanServer(bridge=None)
    sent_messages = []
    lock = threading.Lock()

    class FakeClient:
        def send(self, data):
            time.sleep(0.0005)
            with lock:
                sent_messages.append(data)

    fake = FakeClient()
    token = srv._issue_token()
    assert srv._authenticate_and_register(fake, token) is True

    def worker_broadcast(wid):
        for i in range(10):
            srv.broadcast(json.dumps({"type": "token_delta", "data": {"text": f"t_{wid}_{i}"}}))

    def worker_route(wid):
        for i in range(10):
            srv._route_message(fake, json.dumps({"method": "get_initial_state", "args": [], "id": wid * 100 + i}))

    threads = []
    for wid in range(4):
        threads.append(threading.Thread(target=worker_broadcast, args=(wid,)))
        threads.append(threading.Thread(target=worker_route, args=(wid,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # broadcast 走每客户端出站队列异步发送（真实注册路径建队列），等排空再断言
    assert srv.wait_outbound_drain(timeout=5.0) is True
    # 4 个 worker_broadcast 发送 4*10=40 条，4 个 worker_route 发送 4*10=40 条，共 80 条全部收到
    assert len(sent_messages) == 80


def test_replay_buffer_only_stores_whitelisted_events():
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, fake)

    srv.broadcast('{"type": "token_delta", "data": {"text": "x"}}', event_type="token_delta")
    srv.broadcast('{"type": "turn_error", "data": {"message": "boom"}}', event_type="turn_error")

    assert fake.sent[0] == '{"type": "token_delta", "data": {"text": "x"}}'
    stamped = json.loads(fake.sent[1])
    assert stamped["seq"] == 1
    assert [(seq, payload) for seq, payload in srv._replay_buffer] == [(1, fake.sent[1])]


def test_register_without_last_seq_never_replays():
    srv = LanServer(bridge=None)
    srv._issue_token()
    srv.broadcast('{"type": "turn_error", "data": {"message": "old"}}', event_type="turn_error")

    registered, replay = srv._register_and_collect_replay(object(), srv._token.decode(), None)
    assert registered is True
    assert replay == []


def test_register_with_last_seq_replays_missed_events_in_order():
    srv = LanServer(bridge=None)
    srv._issue_token()
    live = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, live)
    srv.broadcast('{"type": "notice", "data": {"message": "n1"}}', event_type="notice")
    srv.broadcast('{"type": "token_delta", "data": {}}', event_type="token_delta")
    srv.broadcast('{"type": "turn_error", "data": {"message": "boom"}}', event_type="turn_error")

    reconnect = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    registered, replay = srv._register_and_collect_replay(reconnect, srv._token.decode(), 1)
    assert registered is True
    assert len(replay) == 1
    assert json.loads(replay[0])["data"]["message"] == "boom"

    srv._send_replay(reconnect, replay)
    assert reconnect.sent == replay

    registered2, replay2 = srv._register_and_collect_replay(
        type("W", (), {"send": lambda self, p: None})(), srv._token.decode(), 2
    )
    assert registered2 is True
    assert replay2 == []


class _FakeResumeWS:

    def __init__(self, first_message):
        self._first = first_message
        self.sent = []

    def recv(self, timeout=None):
        if self._first is None:
            raise RuntimeError("no more messages")
        msg, self._first = self._first, None
        return msg

    def __iter__(self):
        return iter(())

    def send(self, payload):
        self.sent.append(payload)

    def close(self, code=None, reason=None):
        pass


def test_handle_ws_replays_missed_events_on_resume():
    srv = LanServer(bridge=None)
    token = srv._issue_token()
    srv.broadcast(json.dumps({"type": "turn_error", "data": {"message": "boom"}}), event_type="turn_error")

    from core.lan_server import PROCESS_BOOT_ID

    ws = _FakeResumeWS(json.dumps({
        "token": token,
        "last_seq": 0,
        "last_boot_id": PROCESS_BOOT_ID,
    }))
    srv._handle_ws(ws)

    kinds = [json.loads(item)["type"] for item in ws.sent]
    assert kinds[0] == "hello"
    assert "seq_reset" not in kinds
    assert json.loads(ws.sent[-1])["data"]["message"] == "boom"


def test_handle_ws_rejects_invalid_token_without_replay():
    srv = LanServer(bridge=None)
    srv._issue_token()
    srv.broadcast(json.dumps({"type": "notice", "data": {"message": "n"}}), event_type="notice")

    class _CloseRecorder(_FakeResumeWS):
        def __init__(self, first_message):
            super().__init__(first_message)
            self.closed = None

        def close(self, code=None, reason=None):
            self.closed = (code, reason)

    ws = _CloseRecorder(json.dumps({"token": "wrong", "last_seq": 0}))
    srv._handle_ws(ws)

    assert ws.sent == []
    assert ws.closed is not None and ws.closed[0] == 4401


def test_bind_url_supports_mobile_path():
    srv = LanServer(bridge=None)
    srv.port = 8765
    srv.lan_ip = "192.168.1.5"
    assert srv.bind_url("t1", path="/m/") == "http://192.168.1.5:8765/m/?token=t1"
    assert srv.bind_url("t2", path="m") == "http://192.168.1.5:8765/m/?token=t2"


def test_get_bind_info_mobile_path_embeds_m_prefix():
    srv = LanServer(bridge=None)
    srv.port = 8765
    srv.lan_ip = "192.168.1.5"
    info = srv.get_bind_info(path="/m/")
    assert info["ok"]
    assert "/m/?token=" in info["url"]


@pytest.fixture()
def mobile_dist(tmp_path, monkeypatch):
    import core.lan_server as lan_mod

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<html><head><title>x</title></head><body>m</body></html>", encoding="utf-8"
    )
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(
        lan_mod.LanServer, "_mobile_dist_dir", staticmethod(lambda: str(dist))
    )
    return dist


def _fake_request(path: str):
    return type("R", (), {"path": path, "headers": {}})()


def test_serve_mobile_index_injects_shim(mobile_dist):
    srv = LanServer(bridge=None)
    resp = srv._serve_mobile_index()
    assert resp.status_code == 200
    assert b"/m/mobile-shim.js" in resp.body
    assert resp.headers["Cache-Control"] == "no-store"
    desktop = srv._serve_index()
    assert b'src="/mobile-shim.js"' in desktop.body


def test_serve_mobile_static_serves_assets_and_spa_fallback(mobile_dist):
    srv = LanServer(bridge=None)
    resp = srv._serve_mobile_static("/m/assets/app.js")
    assert resp.status_code == 200
    assert resp.body == b"console.log(1)"
    deep = srv._serve_mobile_static("/m/convo/abc")
    assert deep.status_code == 200
    assert b"/m/mobile-shim.js" in deep.body
    assert srv._serve_mobile_static("/m/assets/missing.js").status_code == 404


def test_serve_mobile_static_blocks_traversal(mobile_dist, tmp_path):
    srv = LanServer(bridge=None)
    assert srv._serve_mobile_static("/m/../secret.txt").status_code == 404
    assert srv._serve_mobile_static("/m/%2e%2e/secret.txt").status_code == 404


def test_serve_http_routes_m_paths(mobile_dist):
    srv = LanServer(bridge=None)
    root = srv._serve_http(None, _fake_request("/m/"))
    assert root.status_code == 200
    assert b"/m/mobile-shim.js" in root.body
    asset = srv._serve_http(None, _fake_request("/m/assets/app.js"))
    assert asset.status_code == 200
    assert asset.body == b"console.log(1)"
    assert srv._serve_http(None, _fake_request("/m/mobile-shim.js")).status_code == 200
    desktop = srv._serve_http(None, _fake_request("/"))
    assert desktop.status_code == 200
    assert b'src="/mobile-shim.js"' in desktop.body


def test_bridge_qr_mobile_flag_uses_m_path(monkeypatch):
    bridge = ApiBridge.__new__(ApiBridge)

    class _Srv:
        def get_bind_info(self, force_new=False, path="/"):
            return {"ok": True, "path": path}

    monkeypatch.setattr(bridge, "_ensure_lan_server", lambda: _Srv())
    assert bridge.get_remote_bind_qr()["path"] == "/"
    assert bridge.get_remote_bind_qr(mobile=True)["path"] == "/m/"


def test_mobile_shim_snapshot_matches_disk():
    import os

    import core.lan_server as lan_mod

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(
        os.path.join(root, "frontend", "mobile-shim.js"), encoding="utf-8"
    ) as f:
        disk = f.read()
    assert lan_mod._MOBILE_SHIM_FALLBACK_JS == disk


def test_only_latest_token_verifies_after_rotation():
    srv = LanServer(bridge=None)
    t1 = srv._issue_token()
    t2 = srv._issue_token()

    assert not srv._verify_token(t1)
    assert srv._verify_token(t2)
    assert not srv._verify_token(t2 + "x")
    assert not srv._verify_token("")


def test_client_registered_before_rotation_is_detached():
    srv = LanServer(bridge=None)
    tok = srv._issue_token()
    client = _FakeClient()
    assert srv._authenticate_and_register(client, tok) is True
    assert srv._clients == [client]

    srv._issue_token()

    assert srv._clients == []
    assert client.closed and client.closed[0][0] == 4401


def test_rotation_rejects_stale_registration_concurrently():
    srv = LanServer(bridge=None)
    old = srv._issue_token()

    registered_stale = []
    stop = threading.Event()

    def stale_worker():
        while not stop.is_set():
            c = _FakeClient()
            if srv._authenticate_and_register(c, old):
                registered_stale.append(c)
            time.sleep(0.0003)

    t = threading.Thread(target=stale_worker)
    t.start()
    try:
        for _ in range(25):
            srv._issue_token()
            time.sleep(0.0015)
    finally:
        stop.set()
        t.join()

    assert not srv._verify_token(old)
    assert srv._verify_token(srv._token.decode())
    assert srv._clients == []
    for c in registered_stale:
        assert c not in srv._clients
    for c in registered_stale:
        assert c not in srv._clients
        assert c.closed and c.closed[0][0] == 4401


class _LeakProbeBridge:

    def __init__(self):
        self.calls = []

    def _probe(self, name, value):
        self.calls.append(name)
        return {"ok": True, **value}

    def get_provider_api_key(self, provider_id):
        return self._probe("get_provider_api_key", {"api_key": "sk-real-secret"})

    def get_feishu_secret(self):
        return self._probe("get_feishu_secret", {"app_secret": "super_secret"})

    def save_hook(self, payload):
        return self._probe("save_hook", {"msg": "已保存"})

    def grant_hook_trust(self, digest):
        return self._probe("grant_hook_trust", {"msg": "已信任"})

    def add_mcp_server(self, name, command, args, env=None, enabled=True):
        return self._probe("add_mcp_server", {"msg": "已新增服务"})

    def save_ui_layout(self, layout):
        return self._probe("save_ui_layout", {"layout": layout})

    def shutdown(self):
        return self._probe("shutdown", {})


@pytest.mark.parametrize("method", ["get_provider_api_key", "get_feishu_secret"])
def test_lan_forbids_credential_plaintext_reads(method):
    bridge = _LeakProbeBridge()
    srv = LanServer(bridge=bridge)
    response = srv.dispatch_rpc(method, ["p1"] if method.endswith("api_key") else [])
    assert response["error"]["code"] == "method_not_allowed"
    # 拦截必须在到达桥之前：桥桩一旦被调用，敏感值就已经经 LAN 出去了
    assert bridge.calls == []


@pytest.mark.parametrize("method", [
    "save_hook",
    "grant_hook_trust",
    "add_mcp_server",
])
def test_lan_forbids_execution_enabling_config_writes(method):
    bridge = _LeakProbeBridge()
    srv = LanServer(bridge=bridge)
    args = {
        "save_hook": [{"scope": "user", "event": "SessionStart", "command": "calc.exe"}],
        "grant_hook_trust": ["deadbeef"],
        "add_mcp_server": ["evil", "npx", ["-y", "evil-pkg"]],
    }[method]
    response = srv.dispatch_rpc(method, args)
    assert response["error"]["code"] == "method_not_allowed"
    assert bridge.calls == []


@pytest.mark.parametrize("method", ["save_ui_layout", "shutdown"])
def test_lan_forbids_desktop_exclusive_layout_and_lifecycle(method):
    bridge = _LeakProbeBridge()
    srv = LanServer(bridge=bridge)
    response = srv.dispatch_rpc(
        method, [{"groups": []}] if method == "save_ui_layout" else []
    )
    assert response["error"]["code"] == "method_not_allowed"
    assert bridge.calls == []


class _EscalationProbeBridge:

    def __init__(self):
        self.calls = []

    def _probe(self, name, payload):
        self.calls.append(name)
        return payload

    def save_automation_task(self, *args, **kwargs):
        return self._probe("save_automation_task", {"ok": True})

    def set_automation_task_enabled(self, *args, **kwargs):
        return self._probe("set_automation_task_enabled", {"ok": True})

    def run_automation_task_now(self, *args, **kwargs):
        return self._probe("run_automation_task_now", {"ok": True})

    def toggle_mcp_server(self, *args, **kwargs):
        return self._probe("toggle_mcp_server", {"ok": True})


@pytest.mark.parametrize("method,args", [
    ("save_automation_task", [{"name": "t", "prompt": "p", "permission_mode": "full"}]),
    ("set_automation_task_enabled", ["t1", True]),
    ("run_automation_task_now", ["t1"]),
])
def test_lan_forbids_automation_escalation_chain(method, args):
    bridge = _EscalationProbeBridge()
    srv = LanServer(bridge=bridge)
    response = srv.dispatch_rpc(method, args)
    assert response["error"]["code"] == "method_not_allowed"
    assert bridge.calls == []


def test_lan_forbids_toggle_mcp_server():
    bridge = _EscalationProbeBridge()
    srv = LanServer(bridge=bridge)
    response = srv.dispatch_rpc("toggle_mcp_server", ["evil", True])
    assert response["error"]["code"] == "method_not_allowed"
    assert bridge.calls == []


class _PermissionModeProbeBridge:
    def __init__(self):
        self.calls = []

    def set_permission_mode(self, mode, session_name=""):
        self.calls.append((mode, session_name))
        return {"ok": True, "permission_mode": mode, "bound": bool(session_name)}


@pytest.mark.parametrize("session_args", [[], [""], ["   "]])
def test_lan_forbids_global_permission_mode_switch(session_args):
    bridge = _PermissionModeProbeBridge()
    srv = LanServer(bridge=bridge)
    response = srv.dispatch_rpc("set_permission_mode", ["full"] + session_args)
    assert response["error"]["code"] == "method_not_allowed"
    assert bridge.calls == []


def test_lan_allows_session_scoped_permission_mode_switch():
    bridge = _PermissionModeProbeBridge()
    srv = LanServer(bridge=bridge)
    result = srv.dispatch_rpc("set_permission_mode", ["full", "s1"])
    assert "error" not in result
    assert result["ok"] is True
    assert bridge.calls == [("full", "s1")]
    result = srv.dispatch_rpc("set_permission_mode", {"mode": "confirm", "session_name": "s2"})
    assert "error" not in result
    assert result["ok"] is True
    assert bridge.calls[-1] == ("confirm", "s2")


def test_lan_allows_adjacent_read_only_methods():
    calls = []

    class ReadOnlyBridge:
        def get_automation_tasks(self):
            calls.append("tasks")
            return {"ok": True, "tasks": []}

        def get_automation_runs(self, task_id="", limit=50):
            calls.append("runs")
            return {"ok": True, "runs": []}

        def get_mcp_servers(self):
            calls.append("mcp")
            return {"ok": True, "servers": []}

        def reconnect_mcp(self, name):
            calls.append("reconnect")
            return {"ok": True}

        def get_ui_layout(self):
            calls.append("layout")
            return {"ok": True, "layout": {}}

        def get_model_settings(self):
            calls.append("model")
            return {"ok": True, "has_api_key": True}

        def get_feishu_settings(self):
            calls.append("feishu")
            return {"ok": True, "has_secret": True}

    srv = LanServer(bridge=ReadOnlyBridge())
    for method, args in (
        ("get_automation_tasks", []),
        ("get_automation_runs", []),
        ("get_mcp_servers", []),
        ("reconnect_mcp", ["srv1"]),
        ("get_ui_layout", []),
        ("get_model_settings", []),
        ("get_feishu_settings", []),
    ):
        result = srv.dispatch_rpc(method, args)
        # 绝不能返回 method_not_allowed：只读/回填类方法必须真正到达 bridge
        assert "error" not in result, method
        assert result["ok"] is True, method
    assert calls == ["tasks", "runs", "mcp", "reconnect", "layout", "model", "feishu"]
