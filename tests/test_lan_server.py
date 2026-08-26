import json
import threading
import time

from core.lan_server import LanServer


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
    srv._clients = [fake]
    srv.broadcast('{"type":"token_delta"}')
    assert fake.sent == ['{"type":"token_delta"}']


def test_route_rejects_window_close():
    """桌面专属窗口方法应被拒绝（无论黑名单/白名单）。"""
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    srv._route_message(fake, json.dumps({"method": "window_close", "args": {}, "id": 1}))
    assert "method not allowed" in fake.sent[0]


def test_route_allows_non_forbidden_method():
    """黑名单语义：不在 FORBIDDEN_METHODS 内的普通 bridge 方法（如 get_background_tasks）应被放行。"""
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    srv._route_message(fake, json.dumps({"method": "get_background_tasks", "args": {}, "id": 1}))
    assert "method not allowed" not in fake.sent[0]
    resp = json.loads(fake.sent[0])
    assert resp["id"] == 1


def test_route_allows_get_initial_state():
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    # bridge 无 get_initial_state 时 _call_bridge 返回 {"ok": False, "msg": "未知方法"}，但不应被白名单拒绝
    srv._route_message(fake, json.dumps({"method": "get_initial_state", "args": [], "id": 1}))
    resp = json.loads(fake.sent[0])
    assert "method not allowed" not in fake.sent[0]
    assert resp["id"] == 1


def test_route_allows_whitelisted_send():
    """白名单内的 send_user_message 应放行并路由到 bridge。"""
    srv = LanServer(bridge=None)
    calls = []

    class B:
        def send_user_message(self, text, **kw):
            calls.append(text)
            return {"ok": True}

    srv.bridge = B()
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
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


def test_get_bind_info_returns_url_token_qr():
    srv = LanServer(bridge=None)
    srv.port = 8765
    srv.lan_ip = "192.168.1.5"
    info = srv.get_bind_info()
    assert info["ok"]
    assert info["url"].startswith("http://192.168.1.5:8765/?token=")
    assert info["token"]
    assert info["qr"].startswith("data:image/png;base64,")
    assert info["expires_in"] == srv._ttl


def test_ensure_lan_server_concurrent_single_instance(monkeypatch):
    """并发调用 _ensure_lan_server 只创建一个 LanServer 且返回同一实例。

    回归保护：修复前无锁，多个线程会各自 new 一个 LanServer 并各自 start()，
    造成双监听 socket 泄漏；且 _lan_server 在 start() 前就发布引用，并发方
    可能拿到 port=0 的半成品。修复后应满足：单实例 + 先 start 后发布。
    """
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
