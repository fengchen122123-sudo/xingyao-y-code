import json
import socket
import threading
import time

import pytest

from core.lan_server import LanServer
from gui_bridge import ApiBridge


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


def _fake_config(monkeypatch, data):
    """把 config._load / lan_server._save_config_value 替换为内存字典。"""
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
    """重启后复用上次持久化的端口：二维码 URL 不变，手机无需重新扫码。"""
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
    """持久化端口被其他进程占用时回退动态分配，并把新端口回填持久化。"""
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


# ---------------------------------------------------------------------------
# Task 15：LAN 后端权限边界与不可伪造 Turn source（设计 18）
# 手机可查看/停止本机已有 run，但不得远程开启权限；LAN 消息回合来源由
# 服务器注入 turn_source="lan"，客户端 payload 同名字段无法伪造。
# ---------------------------------------------------------------------------

class _RecordingBridge:
    """LAN 边界测试用的 bridge 替身：记录消息入口实际收到的 turn_source。

    与真实 ApiBridge.send_user_message 签名同形（turn_source 是第 6 个位置
    参数，gui_bridge.py:send_user_message）：*args 桩会从首参 dict 里"读出"
    注入的 turn_source，掩盖位置参数绑定绕过（Task 15 审查 Critical-1 根因）。
    """

    def __init__(self):
        self.last_turn_source = "unset"
        self.message_calls = []

    def send_user_message(self, text, session_name=None, attachments=None,
                          workdir=None, model_id=None, turn_source="local"):
        self.last_turn_source = turn_source
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
    """计划外默认裁决（Task 13a 审查 Minor 1，fail-closed）：LAN 不得排空桌面事件队列。"""
    response = server.dispatch_rpc("drain_js_queue", [])
    assert response["error"]["code"] == "method_not_allowed"


@pytest.mark.parametrize("method", ["get_computer_control_status", "stop_computer_control"])
def test_lan_allows_status_and_emergency_stop(server, method):
    assert server.dispatch_rpc(method, ["run-a"] if method.startswith("stop") else [])["ok"] is True


def test_lan_user_message_cannot_override_turn_source(server, bridge):
    server.dispatch_rpc("send_user_message", [{"text": "use computer", "turn_source": "local"}])
    assert bridge.last_turn_source == "lan"


def test_lan_user_message_kwargs_payload_turn_source_overridden(server, bridge):
    """关键字式 payload 的 turn_source 同样被服务器覆盖为 lan。"""
    result = server.dispatch_rpc(
        "send_user_message", {"text": "use computer", "turn_source": "local"}
    )
    assert result["ok"] is True
    assert bridge.last_turn_source == "lan"


def test_lan_plain_text_message_gets_lan_turn_source(server, bridge):
    """移动端 shim 的位置参数调用（纯文本）同样注入 turn_source="lan"。"""
    result = server.dispatch_rpc("send_user_message", ["use computer"])
    assert result["ok"] is True
    assert bridge.last_turn_source == "lan"


def test_lan_payload_dict_args_without_turn_source_binds_lan(server, bridge):
    """对抗回归（Task 15 审查 Critical-1 向量 1）：首参 dict 不带 turn_source。

    旧实现把注入的 "lan" 只写进首参 payload dict，真实签名桥桩的 turn_source
    走默认值 "local"——LAN 回合伪装成本地回合。规范化后桥桩必须收到 "lan"。"""
    result = server.dispatch_rpc("send_user_message", [{"text": "use computer"}])
    assert result["ok"] is True
    assert bridge.last_turn_source == "lan"


def test_lan_payload_dict_with_positional_turn_source_stripped(server, bridge):
    """对抗回归（Task 15 审查 Critical-1 向量 2）：首参 dict + 第 6 位置参数。

    客户端在第 6 个位置参数直接绑定 "local"：旧实现只改写首参 payload，
    位置绑定原样到达 bridge。规范化裁剪后桥桩必须收到 "lan"。"""
    result = server.dispatch_rpc(
        "send_user_message",
        [{"text": "use computer"}, None, None, None, None, "local"],
    )
    assert result["ok"] is True
    assert bridge.last_turn_source == "lan"


def test_lan_positional_turn_source_cannot_reach_bridge():
    """客户端把 turn_source 塞进第 6 个位置参数：规范化裁剪剥掉该绑定通道，
    服务器以 keyword 唯一注入——桥桩只可能收到 "lan"，绝不收到客户端的 "local"。"""
    calls = []

    class StrictBridge:
        # 与真实 ApiBridge.send_user_message 同形：第 6 个位置参数即 turn_source
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


def test_lan_forbids_private_underscore_methods():
    """对抗回归（Task 15 审查 Important-1）：下划线私有方法一律拒绝。

    _queue_js_payload 是桌面 JS 事件桥的内部 seam，dispatcher 拼接
    evaluate_js 无转义——放行等价于远程向桌面 WebView 注入任意 JS，
    可调用被禁 setter 绕过全部 LAN 边界（fail-closed 默认裁决）。"""
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
    """WebSocket 路径与 dispatch_rpc 共用同一边界：私有方法走 wire 也被拒。"""
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    srv._route_message(
        fake,
        json.dumps({"method": "_queue_js_payload", "args": ["evil js"], "id": 11}),
    )
    assert "method not allowed" in fake.sent[0]


def test_route_rejects_computer_permission_mutation():
    """WebSocket 路径与 dispatch_rpc 共用同一边界：电脑操控 mutation 走 wire 也被拒。"""
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    srv._route_message(
        fake, json.dumps({"method": "set_computer_control_enabled", "args": [True], "id": 7})
    )
    assert "method not allowed" in fake.sent[0]


def test_route_injects_lan_turn_source_on_user_message():
    """WS 路径的用户消息同样由服务器注入 turn_source="lan"（客户端 local 被覆盖）。"""
    calls = []

    class B:
        def send_user_message(self, text, **kw):
            calls.append((text, kw.get("turn_source")))
            return {"ok": True}

    srv = LanServer(bridge=B())
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    srv._route_message(
        fake,
        json.dumps({"method": "send_user_message", "args": {"text": "hi", "turn_source": "local"}, "id": 1}),
    )
    resp = json.loads(fake.sent[0])
    assert resp["result"]["ok"] is True
    assert calls == [("hi", "lan")]


def _make_bare_message_bridge(is_turn_active):
    """构造仅含消息入口所需内部状态的裸桥（沿用 test_gui_message_queue 的桩模式）。

    返回 (bridge, spawned, events)：spawned 记录回合启动调用的 (args, kwargs)。
    """
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
    """消息入口只接受受控值 local|lan：非法来源直接拒绝，不启动回合。"""
    bridge, spawned, _ = _make_bare_message_bridge(lambda s: False)
    res = bridge.send_user_message("hi", turn_source="remote")
    assert res["ok"] is False
    assert spawned == []


def test_bridge_local_call_keeps_default_local_turn_source():
    """本地 GUI/CLI 调用不传 turn_source：默认 local，既有行为不变。"""
    bridge, spawned, _ = _make_bare_message_bridge(lambda s: False)
    assert bridge.send_user_message("hi") == {"ok": True}
    assert spawned[0][1]["turn_source"] == "local"


def test_bridge_passes_lan_turn_source_to_turn_worker():
    """服务器注入的 turn_source="lan" 原样透传到回合 worker。"""
    bridge, spawned, _ = _make_bare_message_bridge(lambda s: False)
    assert bridge.send_user_message("hi", turn_source="lan") == {"ok": True}
    assert spawned[0][1]["turn_source"] == "lan"


def test_bridge_queued_message_preserves_lan_turn_source():
    """回合运行中入队的 LAN 消息保留 turn_source：补发回合不得降级为 local。"""
    bridge, _, _ = _make_bare_message_bridge(lambda s: True)
    assert bridge.send_user_message("hi", turn_source="lan") == {"ok": True, "queued": True}
    assert bridge._message_queues["s1"][0]["turn_source"] == "lan"


# ---------------------------------------------------------------------------
# Task 16 累积小修：非字符串 method 的 fail-closed 分发守卫
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", [
    ["send_user_message"],
    123,
    {"method": "send_user_message"},
    None,
])
def test_dispatch_rpc_rejects_non_string_method_without_type_error(server, method):
    """非字符串 method（list/int/dict/None）统一回 method_not_allowed。

    修复前：unhashable method 在 FORBIDDEN_METHODS 判定或 getattr 分发处抛
    TypeError，WebSocket 路由未捕获直接断连；int/None 则在 getattr 处 TypeError。
    """
    response = server.dispatch_rpc(method, [])
    assert response == {"error": {"code": "method_not_allowed"}}
