from __future__ import annotations

import json

import pytest

from core.cloud_relay.client import OUTBOUND_LIMIT, CloudRelayClient
from core.cloud_relay.service import CloudRelayService


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
    assert "relay_lost" in kinds
    assert client.generation == 3
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


@pytest.mark.parametrize("method", [
    "get_provider_api_key",
    "get_feishu_secret",
    "save_hook",
    "grant_hook_trust",
    "shutdown",
    "pty_terminal_write",
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


def test_background_factory_sets_large_max_size_skips_dead_flush_and_reuses_rpc_host(monkeypatch):
    import sys
    import types
    import config
    from core.cloud_relay.service import MAX_WS_FRAME_BYTES, _background_factory

    monkeypatch.setattr(config, "_load", lambda managed=True: {config.CLOUD_RELAY_DEVICE_ID_KEY: "dev-1"})
    connect_calls = []
    lan_inits = []

    class FakeLanServer:
        def __init__(self, bridge=None):
            lan_inits.append(bridge)

        def dispatch_rpc(self, method, args, turn_source=None):
            if method == "custom_err":
                return {"ok": False, "error": {"code": "biz_err"}}
            if method == "forbidden":
                return {"error": {"code": "method_not_allowed"}}
            return {"ok": True, "method": method}

    import core.lan_server as lan_mod

    monkeypatch.setattr(lan_mod, "LanServer", FakeLanServer)

    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.incoming = [
                json.dumps({"type": "relay_registered", "data": {"connection_generation": 1}}),
                json.dumps({"method": "custom_err", "args": [], "id": "1:1"}),
                json.dumps({"method": "forbidden", "args": [], "id": "1:2"}),
            ]
            self.closed_flag = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed_flag = True

        def send(self, payload):
            if self.closed_flag:
                raise RuntimeError("sent on closed socket")
            self.sent.append(payload)

        def recv(self):
            if self.incoming:
                return self.incoming.pop(0)
            import time
            time.sleep(0.05)
            client.close()
            raise ConnectionError("eof")

        def close(self):
            self.closed_flag = True

    sock = FakeSocket()

    def fake_connect(url, **kwargs):
        connect_calls.append((url, kwargs))
        return sock

    fake_sync_client = types.ModuleType("websockets.sync.client")
    fake_sync_client.connect = fake_connect
    monkeypatch.setitem(sys.modules, "websockets.sync.client", fake_sync_client)

    states = []
    client = _background_factory("wss://example.invalid/relay/device", "tok", object(), on_state=states.append)
    import time
    deadline = time.time() + 3.0
    while not client.closed and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.05)

    assert connect_calls and connect_calls[0][1]["max_size"] == MAX_WS_FRAME_BYTES >= 70 * 1024 * 1024
    assert len(lan_inits) == 1
    assert "error" not in states
    replies = {json.loads(p)["id"]: json.loads(p) for p in sock.sent if '"id"' in p}
    assert replies["1:1"]["result"] == {"ok": False, "error": {"code": "biz_err"}}
    assert replies["1:2"]["error"] == "method not allowed"


def test_configure_cloud_relay_runtime_status_and_hot_reload(monkeypatch, tmp_path):
    import os
    import config
    import gui_bridge
    from core.cloud_relay import service as svc_mod

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(config, "YCODE_DIR", str(tmp_path))

    def fake_load(managed=True):
        return json.loads(cfg_path.read_text(encoding="utf-8"))

    def fake_save(key, value):
        cur = fake_load()
        cur[key] = value
        cfg_path.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(config, "_load", fake_load)
    monkeypatch.setattr(config, "save_config_value", fake_save)
    monkeypatch.setattr("core.credentials.protect_secret", lambda v: f"enc:{v}")
    monkeypatch.setattr("core.credentials.unprotect_secret", lambda v: v[4:] if v.startswith("enc:") else v)

    bad = svc_mod.configure_cloud_relay("https://not-ws/relay/device", "dev-1", "tok-1")
    assert bad["ok"] is False

    res = svc_mod.configure_cloud_relay("wss://example.com/relay/device", "dev-1", "secret-tok")
    assert res == {
        "ok": True,
        "enabled": True,
        "url": "wss://example.com/relay/device",
        "device_id": "dev-1",
        "has_token": True,
    }
    saved = fake_load()
    assert saved[config.CLOUD_RELAY_TOKEN_KEY] == "enc:secret-tok"

    # 运行态快照持久化与脱敏读取
    started_clients = []

    class FakeClient:
        def __init__(self, url, token, bridge, on_state):
            self.connected = True
            self.closed = False
            self._url = url
            self._token = token
            on_state("connected")

        def status(self):
            return {"connected": True, "generation": 1, "device_id": "dev-1"}

        def close(self):
            self.closed = True

    monkeypatch.setattr(svc_mod, "_background_factory", lambda u, t, b, cb=None: (started_clients.append((u, t)), FakeClient(u, t, b, cb))[1])

    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)
    import threading
    bridge._cloud_relay_service = None
    bridge._cloud_relay_lock = threading.Lock()
    bridge._cloud_relay_watcher_active = True
    bridge._cloud_relay_cfg_mtime_ns = 1
    bridge._cloud_relay_cfg_sig = (False, "", "", "")

    bridge._sync_cloud_relay_from_disk()
    assert len(started_clients) == 1
    assert started_clients[0] == ("wss://example.com/relay/device", "secret-tok")
    assert bridge._cloud_relay_service is not None

    bridge._sync_cloud_relay_from_disk()
    assert len(started_clients) == 1

    st = svc_mod.read_runtime_status()
    assert st["ok"] is True
    assert st["settings"]["state"] == "connected"
    assert st["settings"]["connected"] is True
    assert "secret-tok" not in json.dumps(st)

    svc_mod.configure_cloud_relay("wss://example.com/relay/device", "dev-1", enabled=False)
    os.utime(cfg_path, ns=(os.stat(cfg_path).st_atime_ns, os.stat(cfg_path).st_mtime_ns + 1_000_000))
    bridge._sync_cloud_relay_from_disk()
    assert bridge._cloud_relay_service is None
    assert svc_mod.read_runtime_status()["settings"]["state"] == "disabled"


def test_gui_main_cli_relay_configure_and_status(tmp_path):
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["YCODE_DATA_DIR"] = str(tmp_path)
    p1 = subprocess.run(
        [
            sys.executable,
            "gui_main.py",
            "--cli",
            "--ycode-relay-configure",
            "--url",
            "wss://example.com/relay/device",
            "--device-id",
            "dev-xyz",
            "--token",
            "plain-secret-123",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert p1.returncode == 0, p1.stderr
    out1 = json.loads(p1.stdout.strip().splitlines()[-1])
    assert out1["ok"] is True
    assert out1["device_id"] == "dev-xyz"
    assert out1["has_token"] is True
    assert "plain-secret-123" not in p1.stdout

    p2 = subprocess.run(
        [sys.executable, "gui_main.py", "--cli", "--ycode-relay-status"],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert p2.returncode == 0, p2.stderr
    out2 = json.loads(p2.stdout.strip().splitlines()[-1])
    assert out2["ok"] is True
    assert out2["settings"]["enabled"] is True
    assert out2["settings"]["device_id"] == "dev-xyz"
    assert "plain-secret-123" not in p2.stdout
