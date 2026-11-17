"""P4 云中转桌面侧：客户端丢帧上报、连接状态暴露与 relay 路径的 dispatch_rpc 黑名单。"""

from __future__ import annotations

import json

import pytest

from core.cloud_relay.client import OUTBOUND_LIMIT, CloudRelayClient
from core.cloud_relay.service import CloudRelayService


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
