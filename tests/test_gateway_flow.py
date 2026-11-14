"""网关透传、预览归属、失效踢线和设备心跳。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway.app import create_app
from gateway.auth import create_account
from gateway.relay import RelayRegistry
from gateway.usage import apply_usage, shanghai_day


SECRET = "jwt-secret-jwt-secret-jwt-secret"


def _app(tmp_path):
    app = create_app(tmp_path / "gateway.db", SECRET)
    app.state.init_admin("root", "secret-secret")
    return app


def _login(client, username, password="secret-secret"):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["access_token"]


def test_agent_socket_sends_worker_token_only_upstream(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    token = _login(client, "root")
    link = {"token": "worker-secret", "sent": []}
    app.state.dial_worker = lambda account_id: link
    with client.websocket_connect("/ws/agent") as socket:
        socket.send_text(json.dumps({"jwt": token, "last_seq": None, "last_boot_id": None}))
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()
    frame = link["sent"][0]
    assert frame["token"] == "worker-secret"
    assert frame["last_seq"] is None
    assert frame["last_boot_id"] is None


def test_missing_worker_tells_the_browser_it_is_lost(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    token = _login(client, "root")
    with client.websocket_connect("/ws/agent") as socket:
        socket.send_text(json.dumps({"jwt": token}))
        starting = json.loads(socket.receive_text())
        lost = json.loads(socket.receive_text())
    assert starting["type"] == "worker_starting"
    assert lost == {"type": "worker_lost", "data": {"boot_id": None}}


def test_invalid_jwt_closes_without_a_worker(tmp_path):
    app = _app(tmp_path)
    called = []
    app.state.dial_worker = lambda account_id: called.append(account_id)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as raised:
        with client.websocket_connect("/ws/agent") as socket:
            socket.send_text(json.dumps({"jwt": "not-a-token"}))
            socket.receive_text()
    assert raised.value.code == 4401
    assert called == []


def test_preview_stays_on_the_callers_worker_and_hides_the_token(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    admin = _login(client, "root")
    created = client.post(
        "/api/admin/accounts",
        headers={"Authorization": f"Bearer {admin}"},
        json={"username": "ada", "password": "secret-secret"},
    )
    ada = created.json()["id"]
    app.state.database.execute(
        "INSERT INTO workers(account_id, container_name, worker_token, desired_state, updated_at) VALUES (?, ?, ?, 'running', 1)",
        (ada, f"xingyao-worker-{ada}", "ada-worker-token"),
    )
    app.state.database.commit()
    seen = {}

    def fetch(request):
        seen.update(request)
        return 200, b"png", "image/png"

    app.state.fetch_preview = fetch
    user = _login(client, "ada")
    response = client.get(
        "/computer-preview/preview-token/run/1.png",
        headers={"Authorization": f"Bearer {user}"},
    )
    assert response.status_code == 200
    assert response.content == b"png"
    assert ada in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer ada-worker-token"
    assert b"ada-worker-token" not in response.content


def test_one_account_cannot_see_another_devices(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    admin = _login(client, "root")
    ada = client.post(
        "/api/admin/accounts",
        headers={"Authorization": f"Bearer {admin}"},
        json={"username": "ada", "password": "secret-secret"},
    ).json()["id"]
    grace = client.post(
        "/api/admin/accounts",
        headers={"Authorization": f"Bearer {admin}"},
        json={"username": "grace", "password": "secret-secret"},
    ).json()["id"]
    created = client.post(
        f"/api/admin/devices?account_id={ada}",
        headers={"Authorization": f"Bearer {admin}"},
        json={"name": "家里的台式机"},
    )
    assert created.status_code == 200
    grace_token = _login(client, "grace")
    listed = client.get("/api/devices", headers={"Authorization": f"Bearer {grace_token}"})
    assert listed.json()["devices"] == []
    assert client.post(
        "/api/admin/accounts",
        headers={"Authorization": f"Bearer {grace_token}"},
        json={"username": "x", "password": "secret-secret"},
    ).status_code == 403
    assert grace


def test_disable_closes_open_sockets_and_stops_the_worker(tmp_path):
    app = _app(tmp_path)
    closed = []
    account = app.state.database.execute("SELECT id FROM accounts").fetchone()["id"]
    app.state.sockets.add(account, "jti-1", lambda: closed.append("jti-1"))
    app.state.workers = type("W", (), {"stop": lambda self, account_id: closed.append(account_id)})()
    client = TestClient(app)
    admin = _login(client, "root")
    response = client.patch(
        f"/api/admin/accounts/{account}",
        headers={"Authorization": f"Bearer {admin}"},
        json={"disabled": True},
    )
    assert response.status_code == 200
    assert "jti-1" in closed
    assert account in closed


def test_desktop_registers_and_another_account_cannot_attach(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    admin = _login(client, "root")
    ada = client.post(
        "/api/admin/accounts",
        headers={"Authorization": f"Bearer {admin}"},
        json={"username": "ada", "password": "secret-secret"},
    ).json()["id"]
    created = client.post(
        f"/api/admin/devices?account_id={ada}",
        headers={"Authorization": f"Bearer {admin}"},
        json={"name": "家里的台式机"},
    ).json()
    with client.websocket_connect("/relay/device") as socket:
        socket.send_text(json.dumps({
            "type": "relay_register",
            "token": created["token"],
            "device_id": created["id"],
            "boot_id": "boot-1",
        }))
        registered = json.loads(socket.receive_text())
        assert registered["type"] == "relay_registered"
        socket.send_text(json.dumps({"type": "ping"}))
        assert json.loads(socket.receive_text())["type"] == "pong"
    grace = client.post(
        "/api/admin/accounts",
        headers={"Authorization": f"Bearer {admin}"},
        json={"username": "grace", "password": "secret-secret"},
    )
    assert grace.status_code == 200
    grace_token = _login(client, "grace")
    with pytest.raises(WebSocketDisconnect) as raised:
        with client.websocket_connect("/relay/client") as socket:
            socket.send_text(json.dumps({"jwt": grace_token, "device_id": created["id"]}))
            socket.receive_text()
    assert raised.value.code == 4401


def test_idle_tick_stops_a_quiet_worker():
    from gateway.workers import IdleSupervisor

    stopped = []

    class Manager:
        def consider_idle(self, account_id, metrics, now):
            stopped.append((account_id, now - metrics["last_activity_ts"]))
            return True

    assert IdleSupervisor(Manager()).tick("ada", {"active_turn": False, "last_activity_ts": 0}, 1801)
    assert stopped == [("ada", 1801)]


def test_device_goes_offline_after_45_seconds_without_a_ping():
    registry = RelayRegistry()
    registry.devices["dev"] = {"online": True, "last_ping": 100, "account_id": "a"}
    assert registry.sweep(140) == []
    events = registry.sweep(146)
    assert events[0]["data"]["online"] is False


def test_usage_day_follows_shanghai_midnight():
    from datetime import datetime, timezone

    before = datetime(2026, 11, 14, 15, 30, tzinfo=timezone.utc).timestamp()
    after = datetime(2026, 11, 14, 16, 30, tzinfo=timezone.utc).timestamp()
    assert shanghai_day(before) == "2026-11-14"
    assert shanghai_day(after) == "2026-11-15"
    assert apply_usage
