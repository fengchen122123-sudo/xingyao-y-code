import json
import socket
import threading
import time

import pytest

from core.lan_server import LanServer
from gui_bridge import ApiBridge


@pytest.fixture(autouse=True)
def _no_config_writes(monkeypatch):
    """屏蔽 lan_server 对真实 ~/.ycode/config.json 的写盘。

    _issue_token/_persist_port 会把新 token/端口持久化到用户配置——单测
    调用 get_bind_info()/start() 时会重签 token 并写盘，导致用户已扫码的
    旧 token 全部失效（4401 invalid token）。测试实例一律不落盘。
    """
    import core.lan_server as lan_mod

    monkeypatch.setattr(lan_mod, "_save_config_value", lambda *a, **k: None)


def _register_client(srv, ws):
    """把 fake ws 登记为「已连接客户端」（客户端列表 + 每客户端发送锁）。

    _safe_send 对未注册连接 fail-closed（不发送、返回 False）：直连桩的 ws
    必须像真实注册路径（_register_and_collect_replay）一样同时登记发送锁，
    否则服务器的回包会被 fail-closed 丢弃。
    """
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
    """桌面专属窗口方法应被拒绝（无论黑名单/白名单）。"""
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, fake)
    srv._route_message(fake, json.dumps({"method": "window_close", "args": {}, "id": 1}))
    assert "method not allowed" in fake.sent[0]


def test_route_allows_non_forbidden_method():
    """黑名单语义：不在 FORBIDDEN_METHODS 内的普通 bridge 方法（如 get_background_tasks）应被放行。"""
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
    _register_client(srv, c1)
    _register_client(srv, c2)

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
    _register_client(srv, client)

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
    """LAN 边界测试用的 bridge 替身：记录消息入口实际收到的受控参数。

    与真实 ApiBridge.send_user_message 签名同形（permission_mode 是第 6、
    turn_source 是第 7 个位置参数，gui_bridge.py:send_user_message）：*args
    桩会从首参 dict 里"读出"注入的 turn_source，掩盖位置参数绑定绕过
    （Task 15 审查 Critical-1 根因）。不接 **kwargs，未知键会以 TypeError
    暴露（kwargs 白名单回归的观测面）。
    """

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
    """计划外默认裁决（Task 13a 审查 Minor 1，fail-closed）：LAN 不得排空桌面事件队列。"""
    response = server.dispatch_rpc("drain_js_queue", [])
    assert response["error"]["code"] == "method_not_allowed"


@pytest.mark.parametrize("method", [
    "ide_write_file",
    "ide_set_open_files",
    "ide_save_layout",
])
def test_lan_forbids_ide_state_mutating_methods(server, method):
    """IDE 桥写类方法的黑名单项仍在（历史项：2026-10-17 IDE 子系统已移除）。

    这些方法在 gui_bridge 侧已不存在，名单项保留作纵深防御。本用例锁定
    FORBIDDEN_METHODS 不因清理而丢掉这些边界——若将来同名的远程可写方法被
    重新引入，仍必须 fail-closed（写文件 / 改打开集合 / 覆盖持久化布局都不是
    手机远程控制该有的能力，与 open_path / save_clipboard_image 同一边界，
    设计 18）。
    """
    response = server.dispatch_rpc(method, [])
    assert response["error"]["code"] == "method_not_allowed"


# 原 test_lan_allows_ide_read_only_methods（IDE 只读类放行断言）已删除：
# 2026-10-17 IDE 子系统整体移除后，ide_read_file / ide_stat_files /
# ide_get_layout 在 gui_bridge 侧已不存在，该用例靠自建 ReadBridge 假桩维持
# 「放行」断言，恒绿且零信息量。只读放行的边界原则仍由
# test_lan_allows_pty_read_only_methods 等活用例覆盖。


@pytest.mark.parametrize("method", [
    "pty_terminal_write",
    "pty_terminal_kill",
    "lsp_set_server_path",
])
def test_lan_forbids_pty_write_kill_and_lsp_set_server_path(server, method):
    """交互式终端的「写入/销毁」类与 LSP 路径覆写必须 fail-closed 拒绝（Task 17）。

    dispatch_rpc 是 fail-open 分发（只拦黑名单 + 下划线前缀）：
    - pty_terminal_write 把任意字符串原样写进桌面常驻 shell，等于远程任意
      命令执行——比已封的 ide_run_file 更直接（连文件路径都不校验），是
      「远程执行任意命令」这条通道的本体；
    - pty_terminal_kill 终止用户的常驻 shell（销毁桌面用户态会话）；
    - lsp_set_server_path 写盘 override 并 recycle_all() 换掉 LSP 可执行
      文件，属写配置类，与 ide_write_file / ide_save_layout 同一边界。
    """
    response = server.dispatch_rpc(
        method, ["rm -rf /\r"] if method == "pty_terminal_write" else []
    )
    assert response["error"]["code"] == "method_not_allowed"


def test_lan_allows_pty_read_only_methods():
    """终端的「查看/调尺寸」类必须保持放行（防止后人顺手多封砍掉手机端能力）。

    设计原则：手机只可查看状态与紧急停止——pty_terminal_ensure 只确保会话
    存活（不写任何输入），pty_terminal_resize 只同步 xterm 视口尺寸，两者
    都属「查看」：手机端仍能看桌面终端的输出、能调尺寸，只是敲不进命令。
    误拦会砍掉手机端既有能力，与 ide_read_file 保持放行的先例同口径。
    """
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


# ---------------------------------------------------------------------------
# 安全审查回归：kwargs 白名单化 / 首参 payload 档位剥离 / 发送 fail-closed
# 客户端不得经 dict 通道注入 permission_mode（会话级权限档位）或未知键；
# 未注册（已被摘除/轮换）连接的发送必须放弃，而非无锁直发。
# ---------------------------------------------------------------------------

def test_lan_kwargs_permission_mode_stripped(server, bridge):
    """对抗回归：kwargs 形态注入 permission_mode 被白名单剥离。

    修复前 dict 形态只删 turn_source，permission_mode 经 fn(**merged) 直接
    生效——手机可把会话权限档位抬到 full。白名单化后档位必须回到签名默认 None。"""
    result = server.dispatch_rpc(
        "send_user_message",
        {"text": "hi", "session_name": "s1", "permission_mode": "full"},
    )
    assert result["ok"] is True
    assert bridge.last_permission_mode is None
    assert bridge.last_turn_source == "lan"


def test_lan_kwargs_unknown_keys_stripped(server, bridge):
    """kwargs 形态的未知键同样被白名单剥离，不会落进 fn(**merged)。

    桥桩不接未知关键字：修复前未知键透传会 TypeError，回包 {"ok": False}。"""
    result = server.dispatch_rpc("send_user_message", {"text": "hi", "evil": 1})
    assert result["ok"] is True
    assert bridge.message_calls[-1][0] == "hi"


def test_lan_payload_dict_permission_mode_stripped(server, bridge):
    """对抗回归：首参 payload dict 内 permission_mode 被剥离（防夹带）。

    修复前只删 payload 里的 turn_source，夹带的 permission_mode 随首参 dict
    原样到达 bridge；剥离后桥桩收到的 text 不含该键、档位为默认 None。"""
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
    """未注册（无发送锁）的 ws 直接放弃发送（fail-closed），不再无锁直发。

    修复前 _safe_send 对已注销/被轮换摘除的连接走无锁直发并返回 True，
    与摘除/关闭并发时存在竞态窗口。"""
    srv = LanServer(bridge=None)
    fake = _FakeClient()
    assert srv._safe_send(fake, "x") is False
    assert fake.sent == []


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
    _register_client(srv, fake)
    srv._route_message(
        fake,
        json.dumps({"method": "_queue_js_payload", "args": ["evil js"], "id": 11}),
    )
    assert "method not allowed" in fake.sent[0]


def test_route_rejects_computer_permission_mutation():
    """WebSocket 路径与 dispatch_rpc 共用同一边界：电脑操控 mutation 走 wire 也被拒。"""
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, fake)
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
    _register_client(srv, fake)
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


def test_broadcast_concurrent_thread_safety():
    """并发多线程调用 broadcast 和 _route_message 向同一个客户端发送消息，确保 _client_locks 保护下无竞争丢失。"""
    srv = LanServer(bridge=None)
    sent_messages = []
    lock = threading.Lock()

    class FakeClient:
        def send(self, data):
            # 模拟网络发送耗时
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

    # 4 个 worker_broadcast 发送 4*10=40 条，4 个 worker_route 发送 4*10=40 条，共 80 条全部收到
    assert len(sent_messages) == 80


def test_replay_buffer_only_stores_whitelisted_events():
    """白名单事件打 seq 进补发缓冲；高频事件原样发送不入缓冲。"""
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    _register_client(srv, fake)

    srv.broadcast('{"type": "token_delta", "data": {"text": "x"}}', event_type="token_delta")
    srv.broadcast('{"type": "turn_error", "data": {"message": "boom"}}', event_type="turn_error")

    # 高频事件原样透传，无 seq
    assert fake.sent[0] == '{"type": "token_delta", "data": {"text": "x"}}'
    # 白名单事件带单调 seq 并入缓冲
    stamped = json.loads(fake.sent[1])
    assert stamped["seq"] == 1
    assert [(seq, payload) for seq, payload in srv._replay_buffer] == [(1, fake.sent[1])]


def test_register_without_last_seq_never_replays():
    """全新页面（无 last_seq）不补发：避免新客户端收到陈旧事件。"""
    srv = LanServer(bridge=None)
    srv._issue_token()
    srv.broadcast('{"type": "turn_error", "data": {"message": "old"}}', event_type="turn_error")

    registered, replay = srv._register_and_collect_replay(object(), srv._token.decode(), None)
    assert registered is True
    assert replay == []


def test_register_with_last_seq_replays_missed_events_in_order():
    """断线重连按 last_seq 水位补发缺失白名单事件，顺序与全局 seq 一致。"""
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

    # 补发经每客户端发送锁按序写出
    srv._send_replay(reconnect, replay)
    assert reconnect.sent == replay

    # 已追平水位（last_seq >= 最新 seq）则无补发
    registered2, replay2 = srv._register_and_collect_replay(
        type("W", (), {"send": lambda self, p: None})(), srv._token.decode(), 2
    )
    assert registered2 is True
    assert replay2 == []


class _FakeResumeWS:
    """模拟断线重连的 ws：首条消息带 last_seq，随后立即结束。"""

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
    """_handle_ws 全链路：带 last_seq 的重连在进入 RPC 循环前收到补发。"""
    srv = LanServer(bridge=None)
    token = srv._issue_token()
    srv.broadcast(json.dumps({"type": "turn_error", "data": {"message": "boom"}}), event_type="turn_error")

    ws = _FakeResumeWS(json.dumps({"token": token, "last_seq": 0}))
    srv._handle_ws(ws)

    assert len(ws.sent) == 1
    assert json.loads(ws.sent[0])["data"]["message"] == "boom"


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


# --------------------------------------------------------------------------- #
# 独立移动端子应用（frontend-mobile/dist，/m/ 路径托管）
# --------------------------------------------------------------------------- #

def test_bind_url_supports_mobile_path():
    srv = LanServer(bridge=None)
    srv.port = 8765
    srv.lan_ip = "192.168.1.5"
    assert srv.bind_url("t1", path="/m/") == "http://192.168.1.5:8765/m/?token=t1"
    # 缺尾斜杠自动补齐；缺前导斜杠自动补齐
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
    """构造一个最小 frontend-mobile/dist 并把 _mobile_dist_dir 指过去。"""
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
    # 桌面 index 注入路径不受影响（仍指向 /mobile-shim.js）
    desktop = srv._serve_index()
    assert b'src="/mobile-shim.js"' in desktop.body


def test_serve_mobile_static_serves_assets_and_spa_fallback(mobile_dist):
    srv = LanServer(bridge=None)
    # 静态资源命中
    resp = srv._serve_mobile_static("/m/assets/app.js")
    assert resp.status_code == 200
    assert resp.body == b"console.log(1)"
    # SPA 深链（无扩展名）回退到移动端 index
    deep = srv._serve_mobile_static("/m/convo/abc")
    assert deep.status_code == 200
    assert b"/m/mobile-shim.js" in deep.body
    # 带扩展名的缺失资源 404
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
    # 根路径仍路由到桌面 index（不注入 /m/ shim 引用）
    desktop = srv._serve_http(None, _fake_request("/"))
    assert desktop.status_code == 200
    assert b'src="/mobile-shim.js"' in desktop.body


def test_bridge_qr_mobile_flag_uses_m_path(monkeypatch):
    """get_remote_bind_qr(mobile=True) 应绑定 /m/ 路径；默认仍是 /。"""
    bridge = ApiBridge.__new__(ApiBridge)  # 跳过庞大 __init__

    class _Srv:
        def get_bind_info(self, force_new=False, path="/"):
            return {"ok": True, "path": path}

    monkeypatch.setattr(bridge, "_ensure_lan_server", lambda: _Srv())
    assert bridge.get_remote_bind_qr()["path"] == "/"
    assert bridge.get_remote_bind_qr(mobile=True)["path"] == "/m/"


# ---------------------------------------------------------------------------
# token 唯一性：轮换与「校验+注册」原子互斥（缺口 1 回归保护）
# ---------------------------------------------------------------------------


def test_mobile_shim_snapshot_matches_disk():
    """内嵌 shim 快照必须与磁盘 frontend/mobile-shim.js 完全一致。

    手机端 shim 有两份来源（磁盘文件 + 打包缺失时的内嵌回退）。两份漂移会让
    打包版手机拿到旧协议（例如丢失 token 持久化/4401 处理），必须锁死一致。
    """
    import os

    import core.lan_server as lan_mod

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(
        os.path.join(root, "frontend", "mobile-shim.js"), encoding="utf-8"
    ) as f:
        disk = f.read()
    assert lan_mod._MOBILE_SHIM_FALLBACK_JS == disk


def test_only_latest_token_verifies_after_rotation():
    """轮换后只有最新 token 能通过校验，旧 token 立即失效。"""
    srv = LanServer(bridge=None)
    t1 = srv._issue_token()
    t2 = srv._issue_token()

    assert not srv._verify_token(t1)
    assert srv._verify_token(t2)
    assert not srv._verify_token(t2 + "x")
    assert not srv._verify_token("")


def test_client_registered_before_rotation_is_detached():
    """轮换瞬间已注册的旧 token 客户端必须被摘除并以 4401 断开。"""
    srv = LanServer(bridge=None)
    tok = srv._issue_token()
    client = _FakeClient()
    assert srv._authenticate_and_register(client, tok) is True
    assert srv._clients == [client]

    srv._issue_token()

    assert srv._clients == []
    assert client.closed and client.closed[0][0] == 4401


def test_rotation_rejects_stale_registration_concurrently():
    """并发场景：旧 token 的注册绝不能在轮换后存活。

    校验+注册持 _clients_lock，轮换的 token 切换也在同一把锁内完成，
    因此任一时刻只有一个 token 能通过校验；用旧 token 抢注册的客户端
    要么被拒绝、要么被随后摘除并 4401 断开。
    """
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

    # 旧 token 已彻底失效，当前 token 可用
    assert not srv._verify_token(old)
    assert srv._verify_token(srv._token.decode())
    # 任何用旧 token 注册成功的客户端最终都不在客户端列表中，且被 4401 断开
    assert srv._clients == []
    for c in registered_stale:
        assert c not in srv._clients
    for c in registered_stale:
        assert c not in srv._clients
        assert c.closed and c.closed[0][0] == 4401


# ---------------------------------------------------------------------------
# LAN 边界审计（本轮）：明文凭据读取 / 写桌面持久配置且决定后续执行 /
# 与已封方法同类同边界。用「会真的返回敏感值」的桥桩，证明拦截发生在
# LAN 分发边界（dispatch_rpc），而不是因为桥桩没有这个方法。
# ---------------------------------------------------------------------------

class _LeakProbeBridge:
    """记录被调用次数并返回敏感值的桥桩：LAN 侧一旦放行就会泄露。"""

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
    """明文凭据读取必须 fail-closed 拒绝（本轮审计：密钥/隐私读取类）。

    快照类方法只回 has_* 布尔（get_model_settings 的 has_api_key /
    get_feishu_settings 的 has_secret），明文只在桌面设置页用户点「显示/
    复制」时按需取回。LAN 放行等于让配对手机一次性拖走全部厂商 Key 与
    飞书 app_secret——手机端设置页的正常显示/回填不需要明文（走 has_*
    占位），只有「显示」「复制」按钮会落空，代价可接受。
    """
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
    """写「决定桌面后续执行什么」的持久配置必须 fail-closed 拒绝（本轮审计）。

    - save_hook：hook 信任门禁对「用户级」是默认放行的
      （core/hooks/trust.py:status_for，scope == "user" → TRUST_TRUSTED），
      而 scope 由客户端自选，所以写一次 ~/.ycode/hooks.json 就能让任意命令
      在此后每次 hook 事件上长期执行、且没有任何桌面确认——比已封的
      pty_terminal_write 更宽（一次一写 vs 长期生效）；
    - grant_hook_trust：同一通道的另一半，给项目级 pending hook 授信等于
      远程「确认安全检查」，与已封的 acknowledge_computer_safety 同类；
    - add_mcp_server：写盘 mcp.json 并立即 spawn（只校验命令名 ∈ npx/node/
      python，args 原样透传），与已封的 lsp_set_server_path 同边界且更直接。
    """
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
    """桌面专属的持久化布局与生命周期方法必须 fail-closed 拒绝（本轮审计）。

    黑名单此前存在同类不对称：ide_save_layout 已封、同为「覆盖桌面持久化
    布局」的 save_ui_layout 却放行；window_close 已封、真正中止全部活跃
    回合并销毁窗口的 shutdown 却放行。两者手机端都无能力需求
    （frontend-mobile 不调用；shutdown 的唯一调用方是 gui_main 的窗口销毁
    链路），与既有窗口控制分组同一划界。
    """
    bridge = _LeakProbeBridge()
    srv = LanServer(bridge=bridge)
    response = srv.dispatch_rpc(
        method, [{"groups": []}] if method == "save_ui_layout" else []
    )
    assert response["error"]["code"] == "method_not_allowed"
    assert bridge.calls == []


# 原 test_lan_allows_lsp_read_and_sync_methods（LSP 只读/同步类放行断言）已删除：
# 2026-10-17 LSP 子系统随 IDE 一并移除后，lsp_server_status / lsp_request /
# lsp_notify / lsp_sync_workspace 在 gui_bridge 侧已不存在，该用例靠自建
# LspBridge 假桩维持「放行」断言，恒绿且零信息量。封禁侧仍由
# test_lan_forbids_pty_write_kill_and_lsp_set_server_path 覆盖。


class _EscalationProbeBridge:
    """记录调用：验证封禁发生在到达桥之前（桥桩一旦被调用，危害已经发生）。"""

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
    """自动化任务链必须 fail-closed 拒绝（本轮审计 Important）。

    手机端原本可经 save_automation_task(permission_mode="full") →
    set_automation_task_enabled(True) 或 run_automation_task_now(task_id)
    拿到一次 full 档的桌面回合：automation 任务自带 permission_mode
    （core/automation/models.py 允许 "full"），而 run_automation_task_now
    内部硬编码 turn_source="local"（gui_bridge.py 的
    _dispatch_automation_run），连 resolve_computer_capability 的远程禁令
    都不继承。只强制 turn_source 不足以堵住（任务自带的 full 档仍生效），
    所以三个入口一起封。只读快照保持放行（见下一条用例）。
    """
    bridge = _EscalationProbeBridge()
    srv = LanServer(bridge=bridge)
    response = srv.dispatch_rpc(method, args)
    assert response["error"]["code"] == "method_not_allowed"
    assert bridge.calls == []


def test_lan_forbids_toggle_mcp_server():
    """MCP 启停必须 fail-closed 拒绝：与已封的 add_mcp_server 同通道。

    「启用」半边能把桌面用户主动停用的 MCP 服务远程拉起（拉起即 spawn 该
    服务配置的命令）。此前以「移动端在用」为由保留，经核实 frontend-mobile
    对 toggle_mcp_server 零引用，该理由不成立。
    """
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
    """无 session 的 set_permission_mode 必须拒绝：它会改写并落盘全局档位。

    gui_bridge.py 的 set_permission_mode 在 target_session 为空时调
    app_context.set_permission_mode 并 save_config_value("permission_mode", ...)
    ——手机端可据此把桌面**长期**置成 full（免确认执行文件写入与命令）。
    手机端只需要会话级切换（frontend-mobile 恒带 currentSession）。
    空白串也必须拒（否则 {"session_name": "  "} 会绕过守卫落到全局分支）。
    """
    bridge = _PermissionModeProbeBridge()
    srv = LanServer(bridge=bridge)
    response = srv.dispatch_rpc("set_permission_mode", ["full"] + session_args)
    assert response["error"]["code"] == "method_not_allowed"
    assert bridge.calls == []


def test_lan_allows_session_scoped_permission_mode_switch():
    """带 session 的会话级切换必须放行（手机端的档位选择器依赖它）。

    与上一条构成「封全局 / 留会话」的双侧锁定：只封不留会把手机端的
    权限档位选择器整条打断，而它是 frontend-mobile 真实在用的功能
    （frontend-mobile/src/stores/app.ts 调用时恒带 currentSession）。
    """
    bridge = _PermissionModeProbeBridge()
    srv = LanServer(bridge=bridge)
    # 位置通道
    result = srv.dispatch_rpc("set_permission_mode", ["full", "s1"])
    assert "error" not in result
    assert result["ok"] is True
    assert bridge.calls == [("full", "s1")]
    # kwargs 通道
    result = srv.dispatch_rpc("set_permission_mode", {"mode": "confirm", "session_name": "s2"})
    assert "error" not in result
    assert result["ok"] is True
    assert bridge.calls[-1] == ("confirm", "s2")


def test_lan_allows_adjacent_read_only_methods():
    """新封方法的相邻只读方法必须保持放行（评审 F5：双侧覆盖不能只做「封」）。

    上一轮的双侧断言只做到了 LSP 组，新封方法的相邻只读方法没有「仍放行」
    锁定——后人顺手多封不会被测出，而手机端的任务列表、运行历史、MCP 列表、
    布局读取与设置回填会随之静默失效。
    """
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
