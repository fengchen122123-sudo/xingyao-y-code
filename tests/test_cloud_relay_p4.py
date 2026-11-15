"""P4 云中转协议面：reset/online/status/replay/resync_required、镜像与队列溢出、
generation 踢线、App 名额回收、RPC id 隔离、relay 路径的 dispatch_rpc 黑名单。"""

from __future__ import annotations

import json

import pytest

from core.cloud_relay.client import MIRROR_LIMIT, OUTBOUND_LIMIT, CloudRelayClient
from core.cloud_relay.service import CloudRelayService
from gateway.auth import init_admin
from gateway.db import connect
from gateway.relay import MAX_APPS, RelayRegistry


def _device(tmp_path, db_name="gateway.db"):
    database = connect(tmp_path / db_name)
    init_admin(database, "root", "secret-secret")
    account_id = database.execute("SELECT id FROM accounts").fetchone()["id"]
    registry = RelayRegistry()
    device_id, token = registry.create_device(database, account_id, "家里的台式机")
    return database, registry, account_id, device_id, token


# ---------------------------------------------------------------- 协议面


def test_boot_change_sends_device_reset_and_first_connect_replays(tmp_path):
    database, registry, _account, device_id, token = _device(tmp_path)
    first = registry.register(database, device_id, token, "boot-a")
    assert first["reset"] is None
    assert first["online"]["data"]["connection_generation"] == 1
    assert first["online"]["data"]["oldest_seq"] is None

    registry.remember(device_id, 1, json.dumps({"type": "notice", "seq": 1}))
    # 首连 App：last_boot_id 为空只代表没见过帧，应当补窗口而不是要求整态。
    replay = registry.frames_for(device_id, None, None)
    assert replay["type"] == "replay"
    assert len(replay["frames"]) == 1

    second = registry.register(database, device_id, token, "boot-b")
    assert second["generation"] == first["generation"] + 1
    assert second["reset"] == {
        "type": "device_reset",
        "data": {"device_id": device_id, "boot_id": "boot-b"},
    }
    # boot 变了：旧水位必须作废。
    stale = registry.frames_for(device_id, 1, "boot-a")
    assert stale["type"] == "resync_required"
    assert stale["data"]["reason"] == "boot"


def test_mirror_overflow_requires_resync_with_overflow_reason_then_self_heals(tmp_path):
    database, registry, _account, device_id, token = _device(tmp_path)
    registry.register(database, device_id, token, "boot-a")
    for seq in range(1, MIRROR_LIMIT + 40):
        registry.remember(device_id, seq, json.dumps({"type": "notice", "seq": seq}))

    behind = registry.frames_for(device_id, 5, "boot-a")
    assert behind["type"] == "resync_required"
    # 不能把「镜像挤掉过帧」谎报成普通空洞。
    assert behind["data"]["reason"] == "overflow"

    # 整态自愈之后：清掉水位按镜像窗口重放，不再要求 resync。
    healed = registry.frames_for(device_id, None, "boot-a")
    assert healed["type"] == "replay"
    assert len(healed["frames"]) == MIRROR_LIMIT
    # 窗口内的 App 不受影响。
    caught_up = registry.frames_for(device_id, MIRROR_LIMIT + 39, "boot-a")
    assert caught_up == {"type": "replay", "frames": []}


def test_device_side_frame_loss_marks_queue_drop_once(tmp_path):
    database, registry, _account, device_id, token = _device(tmp_path)
    registry.register(database, device_id, token, "boot-a")
    registry.remember(device_id, 1, json.dumps({"type": "notice", "seq": 1}))
    # 桌面出站队列丢过帧：seq 从 1 直接跳到 5。
    registry.remember(device_id, 5, json.dumps({"type": "notice", "seq": 5}))
    assert registry.holes[device_id] == 1

    first = registry.frames_for(device_id, 1, "boot-a")
    assert first["type"] == "resync_required"
    assert first["data"]["reason"] == "queue_drop"
    # 报一次就清：否则每次重连都被永久判成丢帧，永远自愈不了。
    assert device_id not in registry.holes
    assert registry.frames_for(device_id, 1, "boot-a")["type"] == "replay"


def test_gateway_outbound_overflow_reports_queue_drop(tmp_path):
    database, registry, _account, device_id, token = _device(tmp_path)
    registry.register(database, device_id, token, "boot-a")
    lost = registry.enqueue(device_id)
    assert lost["type"] == "resync_required"
    assert lost["data"]["reason"] == "queue_drop"
    assert registry.frames_for(device_id, 0, "boot-a")["data"]["reason"] == "queue_drop"
    # 上报一次即清零：重连拿回正常 replay，不会被永久判成丢帧。
    registry.remember(device_id, 1, json.dumps({"type": "notice", "seq": 1}))
    assert registry.frames_for(device_id, 0, "boot-a")["type"] == "replay"


def test_sweep_marks_offline_after_45_seconds_and_device_status_is_pushed():
    from gateway.relay import DeviceSweeper

    registry = RelayRegistry()
    registry.devices["dev"] = {"account_id": "acc", "boot_id": "b", "generation": 1,
                               "connection": None, "online": True, "last_ping": 0.0}
    pushed = []
    sweeper = DeviceSweeper(registry, lambda account, device, frame: pushed.append((account, device, frame)),
                            clock=lambda: 0.0)
    assert sweeper.run_once() == []
    sweeper.clock = lambda: 60.0
    events = sweeper.run_once()
    assert events == [{"type": "device_status", "data": {"device_id": "dev", "online": False}}]
    assert pushed[0][0] == "acc"
    # 已经离线的不重复推。
    assert sweeper.run_once() == []


# ---------------------------------------------------------------- generation 与名额


def test_revoke_and_rotate_kick_the_live_connection(tmp_path):
    database, registry, _account, device_id, token = _device(tmp_path)
    kicked = []
    registry.register(database, device_id, token, "boot-a")
    registry.devices[device_id]["connection"] = lambda: kicked.append("revoke")
    registry.attach_app(device_id, "app-1")
    registry.revoke(database, device_id)
    assert kicked == ["revoke"]
    assert device_id not in registry.devices
    # 吊销要连 App 名额一起释放，不能留下幽灵占位。
    assert registry.app_count(device_id) == 0
    assert registry.register(database, device_id, token, "boot-b")["ok"] is False

    fresh_id, fresh_token = registry.create_device(database, registry.devices.get("x", {}).get("account_id")
                                                   or database.execute("SELECT account_id FROM devices LIMIT 1").fetchone()["account_id"],
                                                   "第二台")
    registry.register(database, fresh_id, fresh_token, "boot-a")
    registry.devices[fresh_id]["connection"] = lambda: kicked.append("rotate")
    new_token = registry.rotate(database, fresh_id)
    assert new_token and new_token != fresh_token
    assert kicked == ["revoke", "rotate"]
    assert registry.register(database, fresh_id, fresh_token, "boot-a")["close"] == 4401
    assert registry.register(database, fresh_id, new_token, "boot-a")["ok"] is True


def test_app_slots_release_on_detach_and_same_app_does_not_double_count(tmp_path):
    _database, registry, _account, device_id, _token = _device(tmp_path)
    keys = []
    for index in range(MAX_APPS):
        attached = registry.attach_app(device_id, f"app-{index}")
        assert attached["ok"] is True
        keys.append(attached["key"])
    assert registry.attach_app(device_id, "overflow") == {"ok": False, "error": "too_many_apps"}
    assert registry.app_count(device_id) == MAX_APPS

    # 同一 App 重连：换连接键，不重复占名额。
    again = registry.attach_app(device_id, "app-0")
    assert again["ok"] is True
    assert again["key"] != keys[0]
    assert registry.app_count(device_id) == MAX_APPS

    # 断开/登出释放一个名额后，第 5 个 App 必须能连上，不能误报超限。
    assert registry.detach_app(device_id, again["key"]) is True
    assert registry.app_count(device_id) == MAX_APPS - 1
    assert registry.attach_app(device_id, "newcomer")["ok"] is True
    for key in keys[1:]:
        registry.detach_app(device_id, key)
    assert registry.app_count(device_id) == 1
    registry.detach_app(device_id, registry.app_keys(device_id)[0])
    assert device_id not in registry.apps


def test_two_apps_rpc_ids_do_not_cross_and_replies_restore_ids(tmp_path):
    _database, registry, _account, device_id, _token = _device(tmp_path)
    app_a = registry.attach_app(device_id, "app-a")["key"]
    app_b = registry.attach_app(device_id, "app-b")["key"]

    namespaced_a = registry.namespace_rpc(device_id, app_a, 1)
    namespaced_b = registry.namespace_rpc(device_id, app_b, 1)
    assert namespaced_a != namespaced_b

    # 设备侧回包按命名空间化后的 id 定向回原 App，并把 id 还原。
    assert registry.resolve_rpc(device_id, namespaced_b) == (app_b, 1)
    assert registry.resolve_rpc(device_id, namespaced_a) == (app_a, 1)
    # 一次性路由：已应答的不再残留。
    assert registry.resolve_rpc(device_id, namespaced_a) is None
    assert registry.resolve_rpc(device_id, "no-such-id") is None

    pushed = []
    registry.listen("acc", device_id, lambda frame: pushed.append(("status", frame)))
    registry.listen("acc", device_id, lambda frame: pushed.append(("a", frame)), app_key=app_a)
    registry.listen("acc", device_id, lambda frame: pushed.append(("b", frame)), app_key=app_b)
    route = registry.namespace_rpc(device_id, app_a, 7)
    key, original = registry.resolve_rpc(device_id, route)
    registry.publish("acc", device_id, {"id": original, "result": {"ok": True}}, app_key=key)
    assert [who for who, _frame in pushed] == ["a"]
    # 事件帧仍然广播给所有人（含设备状态页）。
    pushed.clear()
    registry.publish("acc", device_id, {"type": "notice", "data": {}, "seq": 3})
    assert [who for who, _frame in pushed] == ["status", "a", "b"]


# ---------------------------------------------------------------- 客户端


def test_client_reports_lost_frames_after_registration():
    sent = []
    client = CloudRelayClient(send=sent.append, boot_id="boot")

    class Socket:
        def __init__(self):
            self.sent = []
            self.incoming = [json.dumps({
                "type": "relay_registered",
                "data": {"device_id": "dev", "connection_generation": 3},
            })]

        def send(self, payload):
            self.sent.append(payload)

        def recv(self):
            if not self.incoming:
                raise ConnectionError("closed")
            return self.incoming.pop(0)

    for index in range(OUTBOUND_LIMIT + 3):
        client.submit("notice", {"n": index})
    assert client.dropped == 3
    assert client.lost >= 3

    socket = Socket()
    client.pump(socket, "device-token", "dev")
    kinds = [json.loads(item)["type"] for item in sent]
    assert json.loads(socket.sent[0])["type"] == "relay_register"
    # 队列丢过帧就不能装作补齐：注册成功后必须发 relay_lost 让 App 整态自愈。
    assert "relay_lost" in kinds
    assert client.generation == 3
    # 只上报一次，重连不会反复刷。
    assert client.report_loss() is False
    assert client.status()["device_id"] == "dev"


def test_service_status_exposes_connection_state(monkeypatch):
    monkeypatch.setattr("config._load", lambda managed=True: {
        "cloud_relay_enabled": True,
        "cloud_relay_url": "wss://gateway.invalid/relay/device",
        "cloud_relay_token": "device-token",
    })
    monkeypatch.setattr("core.credentials.unprotect_secret", lambda value: value)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.connected = True
            self.closed = False

        def status(self):
            return {"connected": True, "generation": 4, "boot_id": "b", "device_id": "dev",
                    "queued": 2, "dropped": 1, "lost": 5, "closed": False}

        def close(self):
            self.closed = True

    service = CloudRelayService(bridge=object(), client_factory=lambda url, token, bridge, on_state=None: FakeClient())
    assert service.start() is True
    status = service.status()
    assert status["state"] == "connected"
    assert status["generation"] == 4
    assert status["dropped"] == 1
    service.stop()
    assert service.status()["state"] == "stopped"


# ---------------------------------------------------------------- relay 路径的黑名单


@pytest.mark.parametrize("method", [
    "get_provider_api_key",   # 凭据读取
    "get_feishu_secret",
    "save_hook",              # hook 注入
    "grant_hook_trust",
    "shutdown",               # 桌面生命周期
    "pty_terminal_write",     # 终端写入
    "pty_terminal_kill",
])
def test_relay_dispatch_keeps_the_blacklist(monkeypatch, method):
    monkeypatch.setattr("config._load", lambda managed=True: {})

    class Bridge:
        def __getattr__(self, name):
            raise AssertionError(f"黑名单方法 {name} 被放行到桥上了")

    service = CloudRelayService(bridge=Bridge())
    denied = service.dispatch(method, {})
    assert denied["error"]["code"] == "method_not_allowed"


def test_relay_dispatch_also_denies_private_methods(monkeypatch):
    monkeypatch.setattr("config._load", lambda managed=True: {})

    class Bridge:
        def __getattr__(self, name):
            raise AssertionError(f"私有方法 {name} 被放行到桥上了")

    service = CloudRelayService(bridge=Bridge())
    assert service.dispatch("_queue_js_payload", {})["error"]["code"] == "method_not_allowed"
    assert service.dispatch(123, {})["error"]["code"] == "method_not_allowed"
