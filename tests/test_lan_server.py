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


def test_serve_shim_falls_back_when_file_missing(monkeypatch):
    """磁盘上 mobile-shim.js 缺失时，_serve_shim 必须回退内嵌快照而不是 404。

    回归保护：1.2.16 安装包漏打包 frontend/mobile-shim.js，手机端 404 导致
    window.pywebview 永不注入、前端弹「pywebview API 不可用」。
    """
    import core.lan_server as lan_mod

    srv = LanServer(bridge=None)
    monkeypatch.setattr(
        lan_mod.LanServer, "_dist_dir", staticmethod(lambda: "Z:/nonexistent")
    )
    # 将 shim 路径指到不存在的目录：直接模拟文件缺失
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
    # expires_in 为剩余有效秒数：不超过完整 TTL 且为正
    assert 0 < info["expires_in"] <= srv._ttl


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


class _FakeClient:
    """模拟已连接的 WebSocket 客户端，记录 send/close 调用。"""

    def __init__(self):
        self.sent = []
        self.closed = []

    def send(self, payload):
        self.sent.append(payload)

    def close(self, code=None, reason=None):
        self.closed.append((code, reason))


def test_issue_token_kicks_connected_clients():
    """重签 token 必须断开所有已连接客户端并清空列表。

    回归保护：修复前 token 只在连接建立时校验一次，刷新二维码后旧手机
    的 WebSocket 依然存活，旧链接持续可控（二维码泄露无法补救）。
    """
    srv = LanServer(bridge=None)
    c1, c2 = _FakeClient(), _FakeClient()
    srv._clients = [c1, c2]

    srv._issue_token()

    assert srv._clients == []
    assert c1.closed and c2.closed
    assert c1.closed[0][0] == 4401
    assert c2.closed[0][0] == 4401


def test_refresh_bind_info_disconnects_old_clients():
    """点击刷新（force_new）后：旧客户端被断开、旧 token 校验失败、新 token 可用。"""
    srv = LanServer(bridge=None)
    srv.port = 8765
    srv.lan_ip = "192.168.1.5"
    old = srv.get_bind_info()
    client = _FakeClient()
    srv._clients = [client]

    new = srv.get_bind_info(force_new=True)

    assert new["token"] != old["token"]
    assert client.closed and client.closed[0][0] == 4401
    assert srv._clients == []
    assert not srv._verify_token(old["token"])
    assert srv._verify_token(new["token"])


def test_register_rejects_old_token_after_rotation():
    """token 轮换后持旧 token 的连接不能注册为客户端（校验与注册需原子）。"""
    srv = LanServer(bridge=None)
    old = srv._issue_token()
    srv._issue_token()  # 轮换，旧 token 失效

    stale = _FakeClient()
    assert srv._authenticate_and_register(stale, old) is False
    assert srv._clients == []

    fresh = _FakeClient()
    assert srv._authenticate_and_register(fresh, srv._token.decode()) is True
    assert srv._clients == [fresh]
