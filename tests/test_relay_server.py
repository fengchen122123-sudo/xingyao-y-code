"""relay_server 协议验收测试（TDD 红灯阶段）。

契约来源：docs/superpowers/specs/2026-11-17-minimal-relay-server-design.md
覆盖：db / RelayRegistry / remote JWT / 换票端点 / 设备 WS / App WS / 页面托管。
只依赖被测 API 契约，不依赖任何实现细节。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from relay_server import create_app
from relay_server.auth import issue_remote_token, read_remote_token
from relay_server.db import connect
from relay_server.registry import (
    MAX_APPS,
    MIRROR_LIMIT,
    OFFLINE_AFTER,
    OUTBOUND_LIMIT,
    RPC_ROUTE_LIMIT,
    DeviceSweeper,
    RelayRegistry,
)

SECRET = "unit-test-jwt-secret-0123456789abcdef"

# --- harness 兼容（starlette>=1.x）：WebSocketTestSession 必须先 __enter__ 才建 portal。
# --- 下方 device_ws/app_ws helper 在 with 之前就 send/receive，这里做自动进入＋幂等进入，
# --- 不改动任何用例断言与协议契约。
from starlette.testclient import WebSocketTestSession as _WSTS

_orig_enter = _WSTS.__enter__
_orig_send = _WSTS.send
_orig_receive = _WSTS.receive


def _enter_idempotent(self):
    if getattr(self, "exit_stack", None) is not None:
        return self
    return _orig_enter(self)


def _send_auto(self, message):
    if getattr(self, "portal", None) is None:
        _enter_idempotent(self)
    return _orig_send(self, message)


def _receive_auto(self, *args, **kwargs):
    if getattr(self, "portal", None) is None:
        _enter_idempotent(self)
    return _orig_receive(self, *args, **kwargs)


_WSTS.__enter__ = _enter_idempotent
_WSTS.send = _send_auto
_WSTS.receive = _receive_auto


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "relay.db")


@pytest.fixture()
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture()
def reg():
    return RelayRegistry()


@pytest.fixture()
def device(conn, reg):
    device_id, token = reg.create_device(conn, "desk")
    return device_id, token


@pytest.fixture()
def dirs(tmp_path):
    desktop = tmp_path / "desktop-dist"
    mobile = tmp_path / "mobile-dist"
    for d in (desktop, mobile):
        (d / "assets").mkdir(parents=True)
        (d / "index.html").write_text(
            "<html><head><title>t</title></head><body>app</body></html>",
            encoding="utf-8",
        )
        (d / "assets" / "app.js").write_text("console.log(1);", encoding="utf-8")
    shim = tmp_path / "mobile-shim.js"
    shim.write_text("/* relay shim */", encoding="utf-8")
    return SimpleNamespace(desktop=desktop, mobile=mobile, shim=shim)


@pytest.fixture()
def app(db_path, dirs):
    return create_app(
        db_path,
        SECRET,
        desktop_dir=str(dirs.desktop),
        mobile_dir=str(dirs.mobile),
        shim_path=str(dirs.shim),
    )


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- helpers


def raw(seq: int) -> str:
    return json.dumps({"type": "evt", "data": {"n": seq}, "seq": seq})


def frame_seq(f):
    if isinstance(f, str):
        f = json.loads(f)
    return f["seq"]


def recv_until(ws, predicate, limit=30):
    for _ in range(limit):
        frame = ws.receive_json()
        if predicate(frame):
            return frame
    raise AssertionError("期望的帧未在限制次数内收到")


@contextmanager
def device_ws(client, device_id, token, boot_id="boot-1"):
    with client.websocket_connect("/relay/device") as ws:
        ws.send_json(
            {
                "type": "relay_register",
                "device_id": device_id,
                "token": token,
                "boot_id": boot_id,
            }
        )
        hello = ws.receive_json()
        assert hello["type"] == "relay_registered"
        yield ws


@contextmanager
def app_ws(client, device_id, jwt_token, last_seq=None, last_boot_id=None):
    with client.websocket_connect(f"/relay/client?device_id={device_id}") as ws:
        auth = {"token": jwt_token, "device_id": device_id, "jwt": jwt_token}
        if last_seq is not None:
            auth["last_seq"] = last_seq
        if last_boot_id is not None:
            auth["last_boot_id"] = last_boot_id
        ws.send_json(auth)
        yield ws


def ticket(client, device_id, device_token):
    return client.post(
        "/api/relay/device-token",
        json={"device_id": device_id, "device_token": device_token},
    )


# ---------------------------------------------------------------- db


def test_db_connect_row_factory_and_schema(tmp_path):
    c = connect(str(tmp_path / "x.db"))
    try:
        assert c.row_factory is sqlite3.Row
        cols = {r["name"] for r in c.execute("PRAGMA table_info(devices)")}
        assert {"id", "name", "token_hash", "revoked", "created_at", "last_seen"} <= cols
    finally:
        c.close()


def test_protocol_constants():
    assert MAX_APPS == 4
    assert MIRROR_LIMIT == 128
    assert OUTBOUND_LIMIT == 500
    assert OFFLINE_AFTER == 45
    assert RPC_ROUTE_LIMIT == 512


# ---------------------------------------------------------------- registry


class TestRegistry:
    def test_create_device_stores_hash_only(self, conn, reg):
        device_id, token = reg.create_device(conn, "desk")
        assert device_id and token
        row = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        assert row["name"] == "desk"
        assert row["revoked"] == 0
        assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
        # 明文令牌绝不能落库
        assert all(token != str(v) for v in tuple(row))

    def test_register_success_frame(self, conn, reg, device):
        dev, token = device
        res = reg.register(conn, dev, token, "boot-1")
        assert res["ok"] is True
        assert res["generation"] == 1
        assert res["kicked"] is False
        assert res["reset"] is False
        assert res["online"] is True

    def test_register_generation_increments_and_kicks(self, conn, reg, device):
        dev, token = device
        r1 = reg.register(conn, dev, token, "b")
        r2 = reg.register(conn, dev, token, "b")
        assert r2["ok"] is True
        assert r2["generation"] > r1["generation"]
        assert r2["kicked"] is True
        assert r2["reset"] is False

    def test_register_wrong_token_4401(self, conn, reg, device):
        dev, _ = device
        res = reg.register(conn, dev, "wrong-token", "b")
        assert res["ok"] is False
        assert res["close"] == 4401

    def test_register_revoked_4401(self, conn, reg, device):
        dev, token = device
        reg.revoke(conn, dev)
        res = reg.register(conn, dev, token, "b")
        assert res["ok"] is False
        assert res["close"] == 4401

    def test_boot_change_resets_mirror(self, conn, reg, device):
        dev, token = device
        reg.register(conn, dev, token, "b1")
        reg.remember(dev, 1, raw(1))
        res = reg.register(conn, dev, token, "b2")
        assert res["ok"] is True
        assert res["reset"] is True
        plan = reg.frames_for(dev, 0, "b2")
        assert plan["type"] == "replay"
        assert plan["frames"] == []

    def test_remember_dedup(self, conn, reg, device):
        dev, token = device
        reg.register(conn, dev, token, "b")
        reg.remember(dev, 1, raw(1))
        reg.remember(dev, 1, raw(1))
        plan = reg.frames_for(dev, 0, "b")
        assert plan["type"] == "replay"
        assert len(plan["frames"]) == 1

    def test_frames_for_first_connect_empty_replay(self, conn, reg, device):
        dev, token = device
        reg.register(conn, dev, token, "b")
        plan = reg.frames_for(dev, 0, None)
        assert plan == {"type": "replay", "frames": []}

    def test_frames_for_replay_window(self, conn, reg, device):
        dev, token = device
        reg.register(conn, dev, token, "b")
        for seq in (1, 2, 3):
            reg.remember(dev, seq, raw(seq))
        plan = reg.frames_for(dev, 1, "b")
        assert plan["type"] == "replay"
        assert [frame_seq(f) for f in plan["frames"]] == [2, 3]

    def test_frames_for_gap(self, conn, reg, device):
        dev, token = device
        reg.register(conn, dev, token, "b")
        reg.remember(dev, 1, raw(1))
        reg.remember(dev, 3, raw(3))
        plan = reg.frames_for(dev, 1, "b")
        assert plan["type"] == "resync_required"
        assert plan["data"]["device_id"] == dev
        assert plan["data"]["reason"] == "gap"

    def test_frames_for_overflow(self, conn, reg, device):
        dev, token = device
        reg.register(conn, dev, token, "b")
        for seq in range(1, MIRROR_LIMIT + 3):
            reg.remember(dev, seq, raw(seq))
        plan = reg.frames_for(dev, 1, "b")
        assert plan["type"] == "resync_required"
        assert plan["data"]["reason"] == "overflow"

    def test_frames_for_boot_mismatch(self, conn, reg, device):
        dev, token = device
        reg.register(conn, dev, token, "b2")
        reg.remember(dev, 1, raw(1))
        plan = reg.frames_for(dev, 1, "b1")
        assert plan["type"] == "resync_required"
        assert plan["data"]["reason"] == "boot"

    def test_attach_app_same_id_no_double_count(self, reg, device):
        dev, _ = device
        a = reg.attach_app(dev, "jti-1")
        assert a["ok"] is True
        b = reg.attach_app(dev, "jti-1")
        assert b["ok"] is True
        assert reg.app_count(dev) == 1
        reg.detach_app(dev, a["key"])
        assert reg.app_count(dev) == 0

    def test_attach_app_limit_and_release(self, reg, device):
        dev, _ = device
        keys = []
        for i in range(MAX_APPS):
            r = reg.attach_app(dev, f"jti-{i}")
            assert r["ok"] is True
            keys.append(r["key"])
        full = reg.attach_app(dev, "jti-extra")
        assert full["ok"] is False
        assert full["error"] == "too_many_apps"
        reg.detach_app(dev, keys[0])
        assert reg.attach_app(dev, "jti-extra")["ok"] is True

    def test_namespace_rpc_roundtrip_one_shot(self, reg, device):
        dev, _ = device
        att = reg.attach_app(dev, "jti-1")
        ns = reg.namespace_rpc(dev, att["key"], 7)
        assert ns == f'{att["key"]}:7'
        assert reg.resolve_rpc(dev, ns) == (att["key"], 7)
        # 一次性：回程路由用完即焚
        assert reg.resolve_rpc(dev, ns) is None

    def test_namespace_rpc_route_eviction(self, reg, device):
        dev, _ = device
        att = reg.attach_app(dev, "jti-1")
        first = reg.namespace_rpc(dev, att["key"], 0)
        for i in range(1, RPC_ROUTE_LIMIT + 1):
            reg.namespace_rpc(dev, att["key"], i)
        assert reg.resolve_rpc(dev, first) is None
        latest = f'{att["key"]}:{RPC_ROUTE_LIMIT}'
        assert reg.resolve_rpc(dev, latest) == (att["key"], RPC_ROUTE_LIMIT)

    def test_listen_publish_broadcast_and_targeted(self, reg, device):
        dev, _ = device
        got_a, got_b = [], []
        reg.listen(dev, got_a.append, app_key="A")
        reg.listen(dev, got_b.append, app_key="B")
        reg.publish(dev, {"type": "evt"})
        assert got_a == [{"type": "evt"}]
        assert got_b == [{"type": "evt"}]
        reg.publish(dev, {"type": "rpc"}, app_key="A")
        assert len(got_a) == 2
        assert len(got_b) == 1

    def test_sweep_offline_after_45s(self, conn, reg, device):
        dev, token = device
        reg.register(conn, dev, token, "b")
        events = []
        reg.listen(dev, events.append)
        reg.sweep(time.time() + 10)
        assert not [e for e in events if e.get("type") == "device_status"]
        reg.sweep(time.time() + OFFLINE_AFTER + 1)
        offline = [e for e in events if e.get("type") == "device_status"]
        assert offline
        assert offline[-1]["data"]["device_id"] == dev
        assert offline[-1]["data"]["online"] is False

    def test_touch_keeps_online(self, conn, reg, device):
        dev, token = device
        reg.register(conn, dev, token, "b")
        events = []
        reg.listen(dev, events.append)
        reg.touch(dev)
        reg.sweep(time.time() + 10)
        assert not events

    def test_mark_offline_broadcasts_status(self, conn, reg, device):
        dev, token = device
        reg.register(conn, dev, token, "b")
        events = []
        reg.listen(dev, events.append)
        reg.mark_offline(dev)
        assert events[-1]["type"] == "device_status"
        assert events[-1]["data"]["device_id"] == dev
        assert events[-1]["data"]["online"] is False

    def test_rotate_replaces_token(self, conn, reg, device):
        dev, token = device
        reg.register(conn, dev, token, "b")
        result = reg.rotate(conn, dev)
        new_token = result[1] if isinstance(result, tuple) else result
        assert new_token and new_token != token
        assert reg.register(conn, dev, token, "b")["ok"] is False
        assert reg.register(conn, dev, new_token, "b")["ok"] is True

    def test_revoke_blocks_register(self, conn, reg, device):
        dev, token = device
        reg.revoke(conn, dev)
        row = conn.execute("SELECT revoked FROM devices WHERE id=?", (dev,)).fetchone()
        assert row["revoked"] == 1
        assert reg.register(conn, dev, token, "b")["ok"] is False

    def test_disconnect_then_reregister_online(self, conn, reg, device):
        dev, token = device
        reg.register(conn, dev, token, "b")
        reg.disconnect(dev)
        res = reg.register(conn, dev, token, "b")
        assert res["ok"] is True
        assert res["kicked"] is False
        assert res["online"] is True

    def test_device_sweeper_construct(self, reg):
        sweeper = DeviceSweeper(reg, reg.publish, interval=5, clock=time.time)
        assert sweeper is not None


# ---------------------------------------------------------------- auth


class TestAuth:
    def test_issue_read_roundtrip(self, conn, device):
        dev, _ = device
        payload = issue_remote_token(SECRET, dev)
        assert payload["token"]
        assert payload["expires_at"] > time.time()
        claims = read_remote_token(conn, payload["token"], SECRET, dev)
        assert claims is not None
        assert claims.device_id == dev
        assert claims.jti
        decoded = pyjwt.decode(payload["token"], SECRET, algorithms=["HS256"])
        assert decoded["typ"] == "remote"
        assert decoded["device_id"] == dev
        assert decoded["jti"] == claims.jti
        assert decoded["exp"] - decoded["iat"] == 23 * 3600
        assert payload["expires_at"] == decoded["exp"]

    def test_wrong_device_id_none(self, conn, device):
        dev, _ = device
        payload = issue_remote_token(SECRET, dev)
        assert read_remote_token(conn, payload["token"], SECRET, "other-device") is None

    def test_expired_none(self, conn, device):
        dev, _ = device
        t0 = int(time.time())
        payload = issue_remote_token(SECRET, dev, now=t0)
        assert read_remote_token(conn, payload["token"], SECRET, dev, now=t0 + 24 * 3600) is None

    def test_tampered_none(self, conn, device):
        dev, _ = device
        token = issue_remote_token(SECRET, dev)["token"]
        header, body, sig = token.split(".")
        tampered = f"{header}.{body[:-2]}xx.{sig}"
        assert read_remote_token(conn, tampered, SECRET, dev) is None

    def test_wrong_secret_none(self, conn, device):
        dev, _ = device
        token = issue_remote_token(SECRET, dev)["token"]
        assert read_remote_token(conn, token, "another-secret-0123456789abcdef0123", dev) is None

    def test_revoked_device_none(self, conn, reg, device):
        dev, _ = device
        token = issue_remote_token(SECRET, dev)["token"]
        reg.revoke(conn, dev)
        assert read_remote_token(conn, token, SECRET, dev) is None


# ---------------------------------------------------------------- 换票端点


class TestTicketEndpoint:
    def test_success_and_verifiable(self, client, conn, device):
        dev, token = device
        resp = ticket(client, dev, token)
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"]
        assert body["expires_at"] > time.time()
        assert read_remote_token(conn, body["token"], SECRET, dev) is not None

    def test_wrong_token_401(self, client, device):
        dev, _ = device
        assert ticket(client, dev, "nope").status_code == 401

    def test_revoked_401(self, client, conn, reg, device):
        dev, token = device
        reg.revoke(conn, dev)
        assert ticket(client, dev, token).status_code == 401

    def test_rate_limit_seventh_429(self, client, device):
        dev, token = device
        for _ in range(6):
            assert ticket(client, dev, token).status_code == 200
        assert ticket(client, dev, token).status_code == 429


# ---------------------------------------------------------------- 设备 WS


class TestDeviceWS:
    def test_register_handshake(self, client, device):
        dev, token = device
        with client.websocket_connect("/relay/device") as ws:
            ws.send_json(
                {"type": "relay_register", "device_id": dev, "token": token, "boot_id": "b1"}
            )
            hello = ws.receive_json()
            assert hello["type"] == "relay_registered"
            assert hello["data"]["device_id"] == dev
            assert hello["data"]["name"] == "desk"
            assert hello["data"]["connection_generation"] >= 1

    def test_wrong_token_close_4401(self, client, device):
        dev, _ = device
        with client.websocket_connect("/relay/device") as ws:
            ws.send_json(
                {"type": "relay_register", "device_id": dev, "token": "bad", "boot_id": "b"}
            )
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4401

    def test_revoked_close_4401(self, client, conn, reg, device):
        dev, token = device
        reg.revoke(conn, dev)
        with client.websocket_connect("/relay/device") as ws:
            ws.send_json(
                {"type": "relay_register", "device_id": dev, "token": token, "boot_id": "b"}
            )
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4401

    def test_ping_pong(self, client, device):
        dev, token = device
        with device_ws(client, dev, token) as ws:
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}

    def test_seq_frame_broadcast_to_app(self, client, device):
        dev, token = device
        jwt_token = issue_remote_token(SECRET, dev)["token"]
        with app_ws(client, dev, jwt_token) as app:
            assert app.receive_json()["type"] == "replay"
            with device_ws(client, dev, token) as dws:
                dws.send_json({"type": "e2e_event", "data": {"x": 1}, "seq": 1})
                got = recv_until(app, lambda f: f.get("type") == "e2e_event")
                assert got["data"] == {"x": 1}
                assert got["seq"] == 1

    def test_boot_change_reset_then_online(self, client, device):
        dev, token = device
        jwt_token = issue_remote_token(SECRET, dev)["token"]
        with app_ws(client, dev, jwt_token) as app:
            assert app.receive_json()["type"] == "replay"
            with device_ws(client, dev, token, boot_id="b1"):
                recv_until(app, lambda f: f.get("type") == "device_online")
                with device_ws(client, dev, token, boot_id="b2"):
                    recv_until(app, lambda f: f.get("type") == "device_reset")
                    online = recv_until(app, lambda f: f.get("type") == "device_online")
                    data = online["data"]
                    assert data["device_id"] == dev
                    assert data["boot_id"] == "b2"
                    assert "connection_generation" in data
                    assert "oldest_seq" in data
                    assert "newest_seq" in data

    def test_dual_app_rpc_ids_not_crossed(self, client, device):
        dev, token = device
        jwt_a = issue_remote_token(SECRET, dev)["token"]
        jwt_b = issue_remote_token(SECRET, dev)["token"]
        with app_ws(client, dev, jwt_a) as app_a, app_ws(client, dev, jwt_b) as app_b:
            assert app_a.receive_json()["type"] == "replay"
            assert app_b.receive_json()["type"] == "replay"
            with device_ws(client, dev, token) as dws:
                app_a.send_json({"method": "echo", "args": ["A"], "id": 1})
                app_b.send_json({"method": "echo", "args": ["B"], "id": 1})
                for _ in range(2):
                    req = recv_until(dws, lambda f: "method" in f)
                    # id 必须被前缀化，不能原样透传
                    assert req["id"] != 1
                    assert ":" in str(req["id"])
                    dws.send_json({"id": req["id"], "result": {"echo": req["args"]}})
                resp_a = recv_until(app_a, lambda f: "id" in f)
                resp_b = recv_until(app_b, lambda f: "id" in f)
                assert resp_a["id"] == 1
                assert resp_a["result"] == {"echo": ["A"]}
                assert resp_b["id"] == 1
                assert resp_b["result"] == {"echo": ["B"]}

    def test_dual_app_rpc_stress(self, client, device):
        """两 App 各流水线 20 轮 RPC：id/result 不串，且一帧都不能丢。

        收发同时压满，专门制造 asyncio.wait(FIRST_COMPLETED) 里 receive 与
        from_phone/incoming 同批完成的竞态；实现若 continue 丢弃同批另一个
        结果，本用例会挂起（faulthandler 超时）或断言失败。
        """
        dev, token = device
        jwt_a = issue_remote_token(SECRET, dev)["token"]
        jwt_b = issue_remote_token(SECRET, dev)["token"]
        rounds = 20
        with app_ws(client, dev, jwt_a) as app_a, app_ws(client, dev, jwt_b) as app_b:
            assert app_a.receive_json()["type"] == "replay"
            assert app_b.receive_json()["type"] == "replay"
            with device_ws(client, dev, token) as dws:
                # 两 App 连发 20 轮请求，把设备侧 receive/from_phone 管道压满
                for i in range(1, rounds + 1):
                    app_a.send_json({"method": "echo", "args": ["A", i], "id": i})
                    app_b.send_json({"method": "echo", "args": ["B", i], "id": i})
                seen = set()
                for _ in range(2 * rounds):
                    req = recv_until(dws, lambda f: "method" in f)
                    # id 必须被前缀化且全局唯一，不能原样透传/重复
                    assert ":" in str(req["id"])
                    assert req["id"] not in seen
                    seen.add(req["id"])
                    tag, num = req["args"]
                    assert tag in ("A", "B") and 1 <= num <= rounds
                    # 立刻应答，让回程与后续请求在设备循环里同批就绪
                    dws.send_json({"id": req["id"], "result": {"echo": req["args"]}})
                for app, tag in ((app_a, "A"), (app_b, "B")):
                    got = {}
                    for _ in range(rounds):
                        resp = recv_until(app, lambda f: "id" in f)
                        assert isinstance(resp["id"], int)
                        assert resp["id"] not in got
                        got[resp["id"]] = resp
                        # 回程必须还原自己的 id，result 不能串到另一个 App
                        assert resp["result"] == {"echo": [tag, resp["id"]]}
                    assert set(got) == set(range(1, rounds + 1))

    def test_relay_lost_resync_queue_drop(self, client, device):
        dev, token = device
        jwt_token = issue_remote_token(SECRET, dev)["token"]
        with app_ws(client, dev, jwt_token) as app:
            assert app.receive_json()["type"] == "replay"
            with device_ws(client, dev, token, boot_id="b1") as dws:
                dws.send_json(
                    {
                        "type": "relay_lost",
                        "data": {"device_id": dev, "boot_id": "b1", "dropped": 2},
                    }
                )
                got = recv_until(app, lambda f: f.get("type") == "resync_required")
                assert got["data"]["device_id"] == dev
                assert got["data"]["reason"] == "queue_drop"

    def test_second_connection_kicks_first(self, client, device):
        dev, token = device
        with device_ws(client, dev, token) as first:
            with device_ws(client, dev, token):
                with pytest.raises(WebSocketDisconnect) as exc:
                    first.receive_json()
                assert exc.value.code == 4401

    def test_disconnect_broadcasts_offline(self, client, device):
        dev, token = device
        jwt_token = issue_remote_token(SECRET, dev)["token"]
        with app_ws(client, dev, jwt_token) as app:
            assert app.receive_json()["type"] == "replay"
            with device_ws(client, dev, token):
                recv_until(app, lambda f: f.get("type") == "device_online")
            got = recv_until(app, lambda f: f.get("type") == "device_status")
            assert got["data"]["device_id"] == dev
            assert got["data"]["online"] is False


# ---------------------------------------------------------------- App WS


class TestAppWS:
    def test_bad_jwt_close_4401(self, client, device):
        dev, _ = device
        with client.websocket_connect(f"/relay/client?device_id={dev}") as ws:
            ws.send_json({"token": "bad", "device_id": dev, "jwt": "bad"})
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4401

    def test_first_connect_replay_all(self, client, device):
        dev, token = device
        with device_ws(client, dev, token) as dws:
            dws.send_json({"type": "evt", "data": {"n": 1}, "seq": 1})
            dws.send_json({"type": "evt", "data": {"n": 2}, "seq": 2})
            # ping/pong 做处理屏障：确保上面的 seq 帧已被 remember
            dws.send_json({"type": "ping"})
            assert dws.receive_json() == {"type": "pong"}
            jwt_token = issue_remote_token(SECRET, dev)["token"]
            with app_ws(client, dev, jwt_token) as app:
                plan = app.receive_json()
                assert plan["type"] == "replay"
                assert [frame_seq(f) for f in plan["frames"]] == [1, 2]

    def test_replay_with_last_seq_window(self, client, device):
        dev, token = device
        with device_ws(client, dev, token) as dws:
            dws.send_json({"type": "evt", "data": {"n": 1}, "seq": 1})
            dws.send_json({"type": "evt", "data": {"n": 2}, "seq": 2})
            dws.send_json({"type": "ping"})
            assert dws.receive_json() == {"type": "pong"}
            jwt_token = issue_remote_token(SECRET, dev)["token"]
            with app_ws(client, dev, jwt_token, last_seq=1, last_boot_id="boot-1") as app:
                plan = app.receive_json()
                assert plan["type"] == "replay"
                assert [frame_seq(f) for f in plan["frames"]] == [2]

    def test_offline_device_rpc_immediate_error(self, client, device):
        dev, _ = device
        jwt_token = issue_remote_token(SECRET, dev)["token"]
        with app_ws(client, dev, jwt_token) as app:
            assert app.receive_json()["type"] == "replay"
            app.send_json({"method": "echo", "args": [], "id": 9})
            resp = recv_until(app, lambda f: "id" in f)
            assert resp["id"] == 9
            assert resp.get("error")

    def test_same_jti_reconnect_no_slot_leak(self, client, device):
        dev, _ = device
        jwt_token = issue_remote_token(SECRET, dev)["token"]
        for _ in range(MAX_APPS + 2):
            with app_ws(client, dev, jwt_token) as app:
                assert app.receive_json()["type"] == "replay"

    def test_four_apps_then_close_4401(self, client, device):
        dev, _ = device
        with ExitStack() as stack:
            for _ in range(MAX_APPS):
                jwt_token = issue_remote_token(SECRET, dev)["token"]
                ws = stack.enter_context(app_ws(client, dev, jwt_token))
                assert ws.receive_json()["type"] == "replay"
            extra = issue_remote_token(SECRET, dev)["token"]
            with client.websocket_connect(f"/relay/client?device_id={dev}") as ws:
                ws.send_json({"token": extra, "device_id": dev, "jwt": extra})
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 4401


# ---------------------------------------------------------------- 页面托管


class TestPages:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_desktop_page_injection(self, client, device):
        dev, _ = device
        resp = client.get(f"/desktop/{dev}")
        assert resp.status_code == 200
        html = resp.text
        inject = f'window.__ycRelayDevice="{dev}"'
        assert inject in html
        assert '<script src="/desktop-shim.js"></script>' in html
        assert html.index(inject) < html.index("</head>")

    def test_mobile_page_injection(self, client, device):
        dev, _ = device
        resp = client.get(f"/m/{dev}")
        assert resp.status_code == 200
        html = resp.text
        inject = f'window.__ycRelayDevice="{dev}"'
        assert inject in html
        assert '<script src="/m/mobile-shim.js"></script>' in html
        assert html.index(inject) < html.index("</head>")

    def test_shim_route_priority_over_device_id(self, client, dirs):
        # /m/mobile-shim.js 必须先于 /m/{device_id} 注册：
        # 用设备号 "mobile-shim.js" 请求，应返回 shim 内容而不是注入页面。
        shim_text = dirs.shim.read_text(encoding="utf-8")
        r1 = client.get("/desktop-shim.js")
        assert r1.status_code == 200
        assert r1.text == shim_text
        r2 = client.get("/m/mobile-shim.js")
        assert r2.status_code == 200
        assert r2.text == shim_text

    def test_assets_served(self, client):
        # 桌面 dist 以 --base=/desktop-assets/ 构建：资源 URL 带 assets/ 段（整目录 mount）
        assert client.get("/desktop-assets/assets/app.js").status_code == 200
        assert client.get("/m/assets/app.js").status_code == 200

    def test_missing_dist_404(self, db_path, tmp_path):
        app = create_app(
            db_path,
            SECRET,
            desktop_dir=str(tmp_path / "no-desktop"),
            mobile_dir=str(tmp_path / "no-mobile"),
            shim_path=None,
        )
        with TestClient(app) as c:
            assert c.get("/desktop/any-device").status_code == 404
            assert c.get("/m/any-device").status_code == 404
