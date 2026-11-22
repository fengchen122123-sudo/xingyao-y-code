from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
import websockets.sync.client

from core.cloud_relay import portal
from core.cloud_relay.client import CloudRelayClient
from relay_server import create_app
from relay_server.db import connect
from relay_server.registry import RelayRegistry

SECRET = "e2e-jwt-secret-0123456789abcdef0123"
RECV_TIMEOUT = 10


def _wait(predicate, timeout=10.0, message="等待超时"):
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            raise AssertionError(message)
        time.sleep(0.02)


@pytest.fixture()
def relay_env(tmp_path):
    db_path = str(tmp_path / "relay.db")
    conn = connect(db_path)
    reg = RelayRegistry()
    device_id, device_token = reg.create_device(conn, "e2e-desk")
    conn.close()

    desktop = tmp_path / "desktop-dist"
    mobile = tmp_path / "mobile-dist"
    for d in (desktop, mobile):
        (d / "assets").mkdir(parents=True)
        (d / "index.html").write_text(
            "<html><head></head><body>e2e</body></html>", encoding="utf-8"
        )
    shim = tmp_path / "mobile-shim.js"
    shim.write_text("/* e2e shim */", encoding="utf-8")

    app = create_app(
        db_path,
        SECRET,
        desktop_dir=str(desktop),
        mobile_dir=str(mobile),
        shim_path=str(shim),
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True, name="e2e-uvicorn")
    thread.start()
    try:
        _wait(lambda: server.started, message="uvicorn 未能启动")
        port = server.servers[0].sockets[0].getsockname()[1]
        yield SimpleNamespace(
            base=f"http://127.0.0.1:{port}",
            port=port,
            device_id=device_id,
            device_token=device_token,
            db_path=db_path,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture()
def device_client(relay_env):
    conn = websockets.sync.client.connect(
        f"ws://127.0.0.1:{relay_env.port}/relay/device", open_timeout=RECV_TIMEOUT
    )
    client = CloudRelayClient(send=conn.send, boot_id="e2e-boot")
    client.on_rpc = lambda method, args: {"echo": method, "args": args}
    pump = threading.Thread(
        target=client.pump,
        args=(conn, relay_env.device_token, relay_env.device_id),
        daemon=True,
        name="e2e-device-pump",
    )
    pump.start()
    try:
        _wait(lambda: client.connected, message="设备客户端未能注册")
        yield SimpleNamespace(conn=conn, client=client, env=relay_env)
    finally:
        client.close()
        try:
            conn.close()
        except Exception:
            pass
        pump.join(timeout=5)


def _ticket(env):
    payload = portal.request_ticket(env.base, env.device_id, env.device_token)
    assert payload is not None, "换票失败"
    assert payload["token"]
    assert payload["expires_at"] > time.time()
    return payload["token"]


def _app_connect(env, jwt_token, extra=None):
    ws = websockets.sync.client.connect(
        f"ws://127.0.0.1:{env.port}/relay/client?device_id={env.device_id}",
        open_timeout=RECV_TIMEOUT,
    )
    auth = {"token": jwt_token, "device_id": env.device_id, "jwt": jwt_token}
    if extra:
        auth.update(extra)
    ws.send(json.dumps(auth))
    return ws


def _recv_json(ws):
    return json.loads(ws.recv(timeout=RECV_TIMEOUT))


def _recv_until(ws, predicate):
    deadline = time.time() + RECV_TIMEOUT
    while True:
        frame = _recv_json(ws)
        if predicate(frame):
            return frame
        assert time.time() < deadline, "期望的帧未在超时内收到"


def test_ticket_exchange_and_mobile_page(relay_env, device_client):
    env = relay_env
    jwt_token = _ticket(env)
    assert jwt_token
    resp = httpx.get(f"{env.base}/m/{env.device_id}", timeout=RECV_TIMEOUT)
    assert resp.status_code == 200
    assert f'window.__ycRelayDevice="{env.device_id}"' in resp.text


def test_app_rpc_roundtrip(relay_env, device_client):
    env = relay_env
    jwt_token = _ticket(env)
    with _app_connect(env, jwt_token) as app:
        first = _recv_json(app)
        assert first["type"] == "replay"
        app.send(json.dumps({"method": "echo", "args": [1], "id": 1}))
        resp = _recv_until(app, lambda f: "id" in f)
        assert resp["id"] == 1
        assert resp["result"] == {"echo": "echo", "args": [1]}


def test_event_broadcast_and_reconnect_replay(relay_env, device_client):
    env = relay_env
    client = device_client.client
    jwt_token = _ticket(env)
    with _app_connect(env, jwt_token) as app:
        assert _recv_json(app)["type"] == "replay"
        client.submit("e2e_event", {"x": 1})
        frame = _recv_until(app, lambda f: f.get("type") == "e2e_event")
        assert frame["data"] == {"x": 1}
        assert isinstance(frame["seq"], int)
        seq = frame["seq"]
    client.submit("e2e_event", {"x": 2})
    _wait(lambda: client.outbound.qsize() == 0, message="第二条事件未发往网关")
    time.sleep(0.05)
    with _app_connect(
        env, jwt_token, extra={"last_seq": seq, "last_boot_id": "e2e-boot"}
    ) as app2:
        plan = _recv_json(app2)
        assert plan["type"] == "replay"
        frames = [json.loads(f) if isinstance(f, str) else f for f in plan["frames"]]
        assert any(
            f.get("type") == "e2e_event" and f.get("data") == {"x": 2} and f.get("seq") > seq
            for f in frames
        )


def test_large_inbound_frame_over_1mb_succeeds(relay_env):
    from core.cloud_relay.service import MAX_WS_FRAME_BYTES

    env = relay_env
    conn = websockets.sync.client.connect(
        f"ws://127.0.0.1:{env.port}/relay/device",
        open_timeout=RECV_TIMEOUT,
        max_size=MAX_WS_FRAME_BYTES,
    )
    client = CloudRelayClient(send=conn.send, boot_id="large-boot")
    client.on_rpc = lambda method, args: {"method": method, "len": len(args[0])}
    pump = threading.Thread(
        target=client.pump,
        args=(conn, env.device_token, env.device_id),
        daemon=True,
        name="e2e-large-pump",
    )
    pump.start()
    try:
        _wait(lambda: client.connected, message="设备客户端未能注册")
        jwt_token = _ticket(env)
        payload = "X" * (1300 * 1024)
        with _app_connect(env, jwt_token) as app:
            assert _recv_json(app)["type"] == "replay"
            app.send(json.dumps({"method": "upload_echo", "args": [payload], "id": 77}))
            resp = _recv_until(app, lambda f: f.get("id") == 77)
            assert resp["result"] == {"method": "upload_echo", "len": len(payload)}
            assert client.connected is True
    finally:
        client.close()
        try:
            conn.close()
        except Exception:
            pass
        pump.join(timeout=5)


def test_device_reconnect_generation_increments(relay_env, device_client):
    env = relay_env
    client = device_client.client
    gen1 = client.generation
    assert gen1 >= 1
    device_client.conn.close()
    _wait(lambda: not client.connected, message="设备客户端未能感知断开")

    conn2 = websockets.sync.client.connect(
        f"ws://127.0.0.1:{env.port}/relay/device", open_timeout=RECV_TIMEOUT
    )
    client2 = CloudRelayClient(send=conn2.send, boot_id="e2e-boot")
    pump2 = threading.Thread(
        target=client2.pump,
        args=(conn2, env.device_token, env.device_id),
        daemon=True,
        name="e2e-device-pump-2",
    )
    pump2.start()
    try:
        _wait(lambda: client2.connected, message="设备重连未能注册")
        assert client2.generation > gen1
    finally:
        client2.close()
        try:
            conn2.close()
        except Exception:
            pass
        pump2.join(timeout=5)
