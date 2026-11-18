"""云中转：默认关闭、来源固定为 lan、序号缓冲和设备代际。"""

from __future__ import annotations

import json

from core.cloud_relay.client import OUTBOUND_LIMIT, CloudRelayClient
from core.cloud_relay.service import CloudRelayService


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


def test_online_events_are_sent_and_reconnect_does_not_duplicate_replay():
    sent = []
    client = CloudRelayClient(send=sent.append, boot_id="boot")
    client.submit("notice", {"message": "first"})
    client.flush()
    assert len(sent) == 1

    client.submit("notice", {"message": "while-online"})
    client.flush()
    assert len(sent) == 2

    client.handle_registered({"data": {"connection_generation": 2}})
    client.flush()
    assert len(sent) == 3
    assert client.connected is True
    assert client.outbound.qsize() == 0


def test_ephemeral_events_do_not_inflate_lost_or_pollute_buffer():
    sent = []
    client = CloudRelayClient(send=sent.append, boot_id="boot")
    client.handle_registered({"data": {"connection_generation": 1}})
    for i in range(300):
        client.submit("token_delta", {"text": f"t-{i}"})
        client.submit("subagent_progress", {"step": i})
    client.submit("notice", {"message": "keep-me"})
    client.flush()

    assert client.lost == 0
    assert client.dropped == 0
    assert len(client.buffer.items) == 1
    last = json.loads(sent[-1])
    assert last["type"] == "notice"
    assert last["seq"] == 1
    first = json.loads(sent[0])
    assert "seq" not in first


def test_desktop_only_events_skipped_and_send_failure_is_safe():
    client = CloudRelayClient(send=lambda _p: (_ for _ in ()).throw(ConnectionResetError("gone")), boot_id="boot")
    client.connected = True
    client.submit("window_maximize_state", {"maximized": True})
    assert client.outbound.qsize() == 0

    # 发送失败不能把异常抛回事件生产者，且应主动置为断线
    client.submit("notice", {"message": "important"})
    assert client.connected is False
    # 该帧尚未成功送达，未进入 _replayed_seqs，重连后可由 handle_registered 补发
    delivered = []
    client._send = delivered.append
    client.handle_registered({"data": {"connection_generation": 2}})
    assert any("important" in item for item in delivered)


def test_pump_executes_rpcs_concurrently():
    import threading
    import time

    started_slow = threading.Event()
    release_slow = threading.Event()

    class Socket:
        def __init__(self):
            self.sent = []
            self.lock = threading.Lock()
            self.incoming = [
                json.dumps({"type": "relay_registered", "data": {"connection_generation": 1}}),
                json.dumps({"method": "slow", "args": [], "id": "a:1"}),
                json.dumps({"method": "fast", "args": [], "id": "b:1"}),
            ]
            self.done = threading.Event()

        def send(self, payload):
            with self.lock:
                self.sent.append(payload)
                parsed = json.loads(payload)
                if parsed.get("id") == "b:1":
                    release_slow.set()
                if sum(1 for item in self.sent if '"id"' in item) >= 2:
                    self.done.set()

        def recv(self):
            if self.incoming:
                return self.incoming.pop(0)
            self.done.wait(timeout=3)
            raise ConnectionError("closed")

    def on_rpc(method, _args):
        if method == "slow":
            started_slow.set()
            assert release_slow.wait(timeout=2), "快 RPC 被慢 RPC 队头阻塞了"
            return {"ok": "slow"}
        return {"ok": "fast"}

    socket = Socket()
    client = CloudRelayClient(send=socket.send, boot_id="boot")
    client.on_rpc = on_rpc
    client.pump(socket, "tok", "dev")
    replies = [json.loads(p) for p in socket.sent if '"id"' in p]
    assert [r["id"] for r in replies] == ["b:1", "a:1"]


def test_relay_inlines_local_images_in_reply_and_event(tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    file_url = "file:///" + str(img).replace("\\", "/")

    sent = []
    client = CloudRelayClient(send=sent.append, boot_id="boot")
    client.submit("message_final", {"content": f"![p]({file_url})"})
    client.flush()
    assert "data:image/png;base64," in sent[-1]
