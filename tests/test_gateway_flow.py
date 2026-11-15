"""网关透传、预览归属、失效踢线和设备心跳。"""

from __future__ import annotations

import json
import queue
import threading
import time

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
    downstream = queue.Queue()
    link = {"token": "worker-secret", "sent": [], "downstream": downstream}
    app.state.dial_worker = lambda account_id: link
    with client.websocket_connect("/ws/agent") as socket:
        socket.send_text(json.dumps({"jwt": token, "last_seq": None, "last_boot_id": None}))
        socket.send_text(json.dumps({"method": "send_user_message", "id": 1}))
        downstream.put(json.dumps({"type": "leak", "token": "worker-secret"}))
        downstream.put(json.dumps({"type": "hello", "data": {"boot_id": "boot"}}))
        heard = json.loads(socket.receive_text())
        deadline = time.time() + 2
        while time.time() < deadline and len(link["sent"]) < 2:
            time.sleep(0.01)
    assert heard == {"type": "hello", "data": {"boot_id": "boot"}}
    frame = link["sent"][0]
    assert frame["token"] == "worker-secret"
    assert frame["last_seq"] is None
    assert frame["last_boot_id"] is None
    assert json.loads(link["sent"][1])["method"] == "send_user_message"


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


def test_revoking_a_device_closes_its_desktop(tmp_path):
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
        assert json.loads(socket.receive_text())["type"] == "relay_registered"
        deleted = client.delete(
            f"/api/admin/devices/{created['id']}",
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert deleted.status_code == 200
        with pytest.raises(WebSocketDisconnect) as raised:
            socket.receive_text()
    assert raised.value.code == 4401


def test_phone_hears_the_desktop_come_online(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    admin = _login(client, "root")
    ada = client.post(
        "/api/admin/accounts",
        headers={"Authorization": f"Bearer {admin}"},
        json={"username": "ada", "password": "secret-secret"},
    ).json()["id"]
    ada_token = _login(client, "ada")
    created = client.post(
        f"/api/admin/devices?account_id={ada}",
        headers={"Authorization": f"Bearer {admin}"},
        json={"name": "家里的台式机"},
    ).json()
    with client.websocket_connect("/ws/devices") as phone:
        phone.send_text(json.dumps({"jwt": ada_token}))
        assert json.loads(phone.receive_text())["type"] == "ready"
        with client.websocket_connect("/relay/device") as desktop:
            desktop.send_text(json.dumps({
                "type": "relay_register",
                "token": created["token"],
                "device_id": created["id"],
                "boot_id": "boot-1",
            }))
            assert json.loads(desktop.receive_text())["type"] == "relay_registered"
            online = json.loads(phone.receive_text())
    assert online["type"] == "device_online"
    assert online["data"]["device_id"] == created["id"]


def test_a_second_desktop_kicks_the_first(tmp_path):
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
    with client.websocket_connect("/relay/device") as first:
        first.send_text(json.dumps({
            "type": "relay_register",
            "token": created["token"],
            "device_id": created["id"],
            "boot_id": "boot-1",
        }))
        assert json.loads(first.receive_text())["type"] == "relay_registered"
        with client.websocket_connect("/relay/device") as second:
            second.send_text(json.dumps({
                "type": "relay_register",
                "token": created["token"],
                "device_id": created["id"],
                "boot_id": "boot-2",
            }))
            assert json.loads(second.receive_text())["type"] == "relay_registered"
            with pytest.raises(WebSocketDisconnect) as raised:
                first.receive_text()
    assert raised.value.code == 4401


def test_one_account_cannot_start_two_containers(tmp_path):
    from gateway.workers import WorkerManager

    app = _app(tmp_path)
    account = app.state.database.execute("SELECT id FROM accounts").fetchone()["id"]

    class Docker:
        def __init__(self):
            self.names = set()
            self.concurrent = 0
            self.max_concurrent = 0
            self.guard = threading.Lock()

        def exists(self, name):
            return name in self.names

        def create(self, spec):
            with self.guard:
                self.concurrent += 1
                self.max_concurrent = max(self.max_concurrent, self.concurrent)
            time.sleep(0.05)
            self.names.add(spec["name"])
            with self.guard:
                self.concurrent -= 1

        def start(self, _name):
            return None

    docker = Docker()
    manager = WorkerManager(docker, app.state.database, tmp_path, "image")
    health = lambda _url: {"ok": True, "boot_id": "boot"}
    threads = [
        threading.Thread(target=manager.ensure_running, args=(account, health))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert docker.max_concurrent == 1
    assert manager.volume_for(account) != manager.volume_for("someone-else")


def test_restore_uses_the_gateway_worker_token(tmp_path):
    from gateway.backup import reconcile_worker_token

    app = _app(tmp_path)
    account = app.state.database.execute("SELECT id FROM accounts").fetchone()["id"]
    app.state.database.execute(
        "INSERT INTO workers(account_id, container_name, worker_token, desired_state, updated_at) VALUES (?, ?, ?, 'running', 1)",
        (account, f"xingyao-worker-{account}", "from-db"),
    )
    app.state.database.commit()
    volume = tmp_path / "volume"
    volume.mkdir()
    (volume / "config.json").write_text(json.dumps({"remote_pair_token": "from-volume"}), encoding="utf-8")
    assert reconcile_worker_token(app.state.database, account, volume) == "from-db"
    saved = json.loads((volume / "config.json").read_text(encoding="utf-8"))
    assert saved["remote_pair_token"] == "from-db"


def test_idle_poll_waits_thirty_seconds_between_checks():
    from gateway.workers import IdleLoop, IdleSupervisor

    class Manager:
        def consider_idle(self, account_id, metrics, now):
            return True

    loop = IdleLoop(IdleSupervisor(Manager()), interval=30)
    assert loop.advance(0, ["ada"], lambda _account: {"active_turn": False, "last_activity_ts": 0}) == ["ada"]
    assert loop.advance(10, ["ada"], lambda _account: {}) == []
    assert loop.advance(30, ["ada"], lambda _account: {}) == ["ada"]


def test_admin_models_never_disclose_or_persist_plaintext_keys(tmp_path):
    from gateway.platform_models import PlatformModelStore

    class MemoryKeyring:
        def __init__(self):
            self.secrets = {}

        def set_password(self, service, account, secret):
            self.secrets[(service, account)] = secret

        def get_password(self, service, account):
            return self.secrets.get((service, account))

    keyring = MemoryKeyring()
    app = _app(tmp_path)
    app.state.model_store = PlatformModelStore(app.state.database, keyring, "ycode-test")
    client = TestClient(app)
    token = _login(client, "root")
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "providers": [{
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-super-secret",
            "model_id": "deepseek-chat",
            "model_name": "DeepSeek Chat",
            "litellm_model": "openai/deepseek-chat",
        }],
        "active_model_id": "deepseek-chat",
    }
    assert client.put("/api/admin/models", json=body, headers=headers).status_code == 200
    listed = client.get("/api/admin/models", headers=headers)
    assert listed.status_code == 200
    assert "sk-super-secret" not in listed.text
    assert listed.json()["providers"][0]["api_key_configured"] is True
    stored = app.state.database.execute(
        "SELECT providers_json FROM platform_models WHERE id = 1"
    ).fetchone()[0]
    assert "sk-super-secret" not in stored
    assert app.state.model_store.load()["providers"][0]["api_key"] == "sk-super-secret"


def test_platform_models_are_stamped_into_each_account_volume(tmp_path):
    from gateway.platform_models import load_platform_models, save_platform_models
    from gateway.workers import WorkerManager

    app = _app(tmp_path)
    account = app.state.database.execute("SELECT id FROM accounts").fetchone()["id"]
    save_platform_models(
        app.state.database,
        [{
            "id": "deepseek",
            "name": "深度求索",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-test",
            "models": [{"id": "ds-v4-pro", "name": "deepseek-v4-pro", "litellm_model": "openai/deepseek-v4-pro"}],
        }],
        "ds-v4-pro",
    )

    class Docker:
        def exists(self, _name):
            return False

        def create(self, _spec):
            return None

        def start(self, _name):
            return None

    manager = WorkerManager(
        Docker(),
        app.state.database,
        tmp_path,
        "image",
        platform_models=lambda: load_platform_models(app.state.database),
    )
    manager.ensure_token(account)
    saved = json.loads((manager.volume_for(account) / "config.json").read_text(encoding="utf-8"))
    assert saved["providers"][0]["api_key"] == "sk-test"
    assert saved["providers"][0]["base_url"] == "https://api.deepseek.com"
    assert saved["active_model_id"] == "ds-v4-pro"
    assert saved["permission_mode"] == "full"
    assert saved["remote_pair_token"]


def test_loopback_model_url_is_rewritten_for_the_worker(tmp_path):
    from gateway.platform_models import load_platform_models, save_platform_models
    from gateway.workers import WorkerManager, worker_base_url

    assert worker_base_url("http://127.0.0.1:18888/v1", "172.18.0.1") == "http://172.18.0.1:18888/v1"
    assert worker_base_url("https://api.deepseek.com/v1", "172.18.0.1") == "https://api.deepseek.com/v1"
    app = _app(tmp_path)
    account = app.state.database.execute("SELECT id FROM accounts").fetchone()["id"]
    save_platform_models(
        app.state.database,
        [{
            "id": "xingyao-api",
            "name": "星瑶API",
            "base_url": "http://127.0.0.1:18888/v1",
            "api_key": "sk-test",
            "models": [{"id": "deepseek-v4.1-flash-loomy", "name": "deepseek", "litellm_model": "deepseek-v4.1-flash-loomy"}],
        }],
        "deepseek-v4.1-flash-loomy",
    )
    manager = WorkerManager(
        object(),
        app.state.database,
        tmp_path,
        "image",
        platform_models=lambda: load_platform_models(app.state.database),
        host_gateway="172.18.0.1",
    )
    manager.ensure_token(account)
    saved = json.loads((manager.volume_for(account) / "config.json").read_text(encoding="utf-8"))
    assert saved["providers"][0]["base_url"] == "http://172.18.0.1:18888/v1"
    assert saved["providers"][0]["api_key"] == "sk-test"


def test_missing_worker_image_reports_lost_instead_of_raising(tmp_path):
    from gateway.workers import WorkerManager

    app = _app(tmp_path)
    account = app.state.database.execute("SELECT id FROM accounts").fetchone()["id"]

    class Docker:
        def exists(self, _name):
            return False

        def create(self, _spec):
            raise RuntimeError("No such image: xingyao-worker:local")

        def start(self, _name):
            raise AssertionError("镜像不存在时不应启动")

    manager = WorkerManager(Docker(), app.state.database, tmp_path, "xingyao-worker:local", host_gateway="")
    result = manager.ensure_running(account, health=lambda _url: {"ok": False})
    assert result["ok"] is False
    assert result["error"] == "worker_lost"


def test_phone_rpc_is_forwarded_to_the_desktop(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    admin = _login(client, "root")
    ada = client.post(
        "/api/admin/accounts",
        headers={"Authorization": f"Bearer {admin}"},
        json={"username": "ada", "password": "secret-secret"},
    ).json()["id"]
    ada_token = _login(client, "ada")
    created = client.post(
        f"/api/admin/devices?account_id={ada}",
        headers={"Authorization": f"Bearer {admin}"},
        json={"name": "家里的台式机"},
    ).json()
    with client.websocket_connect("/relay/client") as phone:
        phone.send_text(json.dumps({"jwt": ada_token, "device_id": created["id"]}))
        json.loads(phone.receive_text())
        with client.websocket_connect("/relay/device") as desktop:
            desktop.send_text(json.dumps({
                "type": "relay_register",
                "token": created["token"],
                "device_id": created["id"],
                "boot_id": "boot-1",
            }))
            assert json.loads(desktop.receive_text())["type"] == "relay_registered"
            phone.send_text(json.dumps({"method": "send_user_message", "args": {"text": "hi"}, "id": 7}))
            forwarded = json.loads(desktop.receive_text())
            assert forwarded["method"] == "send_user_message"
            # P4：网关只在传输层加 App 前缀（两个 App 各自的 id=7 不能串），回程再还原。
            assert isinstance(forwarded["id"], str)
            assert forwarded["id"] != 7
            assert forwarded["id"].endswith(":7")
            desktop.send_text(json.dumps({"id": forwarded["id"], "result": {"ok": True}}))
            reply = {}
            for _ in range(5):
                reply = json.loads(phone.receive_text())
                if "id" in reply:
                    break
    assert reply["id"] == 7


def test_usage_day_follows_shanghai_midnight():
    from datetime import datetime, timezone

    before = datetime(2026, 11, 14, 15, 30, tzinfo=timezone.utc).timestamp()
    after = datetime(2026, 11, 14, 16, 30, tzinfo=timezone.utc).timestamp()
    assert shanghai_day(before) == "2026-11-14"
    assert shanghai_day(after) == "2026-11-15"
    assert apply_usage
