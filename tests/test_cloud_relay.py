"""云中转：默认关闭、来源固定为 lan、序号缓冲和设备代际。"""

from __future__ import annotations

import json

from core.cloud_relay.client import OUTBOUND_LIMIT, CloudRelayClient
from core.cloud_relay.service import CloudRelayService
from gateway.db import connect
from gateway.relay import MAX_APPS, RelayRegistry


def test_disabled_relay_does_not_start(monkeypatch):
    monkeypatch.setattr("config._load", lambda managed=True: {})
    service = CloudRelayService(bridge=object())
    assert service.start() is False
    assert service.status()["state"] == "disabled"
    service.on_event("notice", {"message": "x"})


def test_dispatch_strips_client_turn_source_and_blocks_private_methods(monkeypatch):
    monkeypatch.setattr("config._load", lambda managed=True: {})

    class Bridge:
        def send_user_message(self, text="", **kwargs):
            return {"text": text, "turn_source": kwargs.get("turn_source")}

        def window_close(self):
            return {"closed": True}

    service = CloudRelayService(bridge=Bridge())
    sent = service.dispatch("send_user_message", {"text": "hi", "turn_source": "local"})
    assert sent["turn_source"] == "lan"
    denied = service.dispatch("window_close", {})
    assert denied["error"]["code"] == "method_not_allowed"


def test_client_registers_on_an_open_socket_and_flushes_the_window():
    from core.cloud_relay.client import CloudRelayClient

    class Socket:
        def __init__(self):
            self.sent = []
            self.incoming = [json.dumps({
                "type": "relay_registered",
                "data": {"device_id": "dev", "name": "家里的台式机", "connection_generation": 2},
            })]

        def send(self, payload):
            self.sent.append(payload)

        def recv(self):
            if not self.incoming:
                raise ConnectionError("closed")
            return self.incoming.pop(0)

    delivered = []
    client = CloudRelayClient(send=delivered.append, boot_id="boot")
    client.submit("notice", {"message": "queued"})
    socket = Socket()
    client.pump(socket, "device-token", "dev")
    first = json.loads(socket.sent[0])
    assert first["type"] == "relay_register"
    assert first["token"] == "device-token"
    assert first["boot_id"] == "boot"
    assert client.generation == 2
    assert any("queued" in item for item in delivered)
    assert client.connected is False


def test_outbound_queue_does_not_block_when_full():
    sent = []
    client = CloudRelayClient(send=sent.append, boot_id="abc")
    frame = client.register_frame("token", "device")
    assert frame["boot_id"] == "abc"
    assert "token" in frame
    for index in range(OUTBOUND_LIMIT + 3):
        client.submit("notice", {"n": index})
    assert client.dropped == 3
    assert client.outbound.qsize() == OUTBOUND_LIMIT


def test_new_registration_kicks_the_previous_connection(tmp_path):
    database = connect(tmp_path / "gateway.db")
    registry = RelayRegistry()
    from gateway.auth import init_admin

    init_admin(database, "root", "secret-secret")
    account_id = database.execute("SELECT id FROM accounts").fetchone()["id"]
    device_id, token = registry.create_device(database, account_id, "家里的台式机")
    first = registry.register(database, device_id, token, "boot-a")
    second = registry.register(database, device_id, token, "boot-b")
    assert second["generation"] == first["generation"] + 1
    assert second["kicked"] is not None
    assert second["reset"]["type"] == "device_reset"
    assert second["online"]["data"]["oldest_seq"] is None
    stolen = registry.register(database, device_id, "wrong", "boot-b")
    assert stolen["close"] == 4401
    for index in range(MAX_APPS):
        assert registry.attach_app(device_id, f"app-{index}")["ok"] is True
    assert registry.attach_app(device_id, "overflow")["ok"] is False
    registry.remember(device_id, 1, json.dumps({"type": "notice", "seq": 1}))
    replay = registry.frames_for(device_id, 0, "boot-b")
    assert replay["type"] == "replay"
    assert len(replay["frames"]) == 1
    gap = registry.frames_for(device_id, 0, "boot-old")
    assert gap["type"] == "resync_required"
