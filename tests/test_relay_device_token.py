"""云中转换票：桌面用设备令牌换只读 remote JWT，供公网二维码扫码入站。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import config
from core.cloud_relay import portal
from gateway.app import create_app
from gateway.auth import read_remote_access

SECRET = "jwt-secret-jwt-secret-jwt-secret"


def _client(tmp_path):
    app = create_app(tmp_path / "gateway.db", SECRET)
    app.state.init_admin("root", "secret-secret")
    client = TestClient(app)
    account = app.state.database.execute("SELECT id FROM accounts WHERE username = 'root'").fetchone()
    device_id, device_token = app.state.relay.create_device(
        app.state.database, account["id"], "家里的台式机"
    )
    return app, client, device_id, device_token


def _ticket(client, device_id, device_token):
    return client.post(
        "/api/relay/device-token",
        json={"device_id": device_id, "device_token": device_token},
    )


def test_device_token_is_exchanged_for_readable_remote_jwt(tmp_path):
    app, client, device_id, device_token = _client(tmp_path)
    response = _ticket(client, device_id, device_token)
    assert response.status_code == 200
    payload = response.json()
    assert payload["token"]
    assert payload["expires_at"] > time.time()
    # 换来的票必须能被远端页校验器认，且只绑自己这台设备
    principal = read_remote_access(app.state.database, payload["token"], SECRET, device_id)
    assert principal is not None
    other = read_remote_access(app.state.database, payload["token"], SECRET, "another-device")
    assert other is None


def test_wrong_device_token_is_rejected(tmp_path):
    _app, client, device_id, _device_token = _client(tmp_path)
    assert _ticket(client, device_id, "not-the-token").status_code == 401
    assert _ticket(client, "missing-device", "whatever").status_code == 401


def test_revoked_device_is_rejected(tmp_path):
    app, client, device_id, device_token = _client(tmp_path)
    app.state.relay.revoke(app.state.database, device_id)
    assert _ticket(client, device_id, device_token).status_code == 401


def test_disabled_account_is_rejected(tmp_path):
    app, client, device_id, device_token = _client(tmp_path)
    app.state.database.execute("UPDATE accounts SET disabled = 1 WHERE username = 'root'")
    app.state.database.commit()
    assert _ticket(client, device_id, device_token).status_code == 403


def test_exchange_is_rate_limited(tmp_path):
    _app, client, device_id, device_token = _client(tmp_path)
    codes = [_ticket(client, device_id, device_token).status_code for _ in range(7)]
    assert codes[:6] == [200] * 6
    assert codes[6] == 429


def test_site_base_keeps_reverse_proxy_prefix():
    assert portal.site_base("wss://example.com/relay/device") == "https://example.com"
    assert (
        portal.site_base("wss://example.com/xy/bot/relay/device")
        == "https://example.com/xy/bot"
    )
    assert portal.site_base("ws://127.0.0.1:18080/relay/device") == "http://127.0.0.1:18080"
    assert portal.site_base("https://example.com") == ""


class _FakeService:
    def __init__(self, connected: bool, device_id: str = "dev-1"):
        self._connected = connected
        self._device_id = device_id

    def status(self) -> dict:
        return {"connected": self._connected, "device_id": self._device_id}


class _FakeBridge:
    def __init__(self, service):
        self._cloud_relay_service = service


@pytest.fixture
def relay_config(monkeypatch):
    monkeypatch.setattr(
        config,
        "_load",
        lambda: {
            config.CLOUD_RELAY_URL_KEY: "wss://example.com/xy/bot/relay/device",
            config.CLOUD_RELAY_TOKEN_KEY: "plain-device-token",
        },
    )
    monkeypatch.setattr(portal, "_TICKET_CACHE", {})
    monkeypatch.setattr(portal, "device_token", lambda: "plain-device-token")
    monkeypatch.setattr(
        portal,
        "request_ticket",
        lambda base, device_id, token: {
            "token": "remote-jwt",
            "expires_at": int(time.time()) + 3600,
        },
    )


def test_pair_url_is_none_when_relay_is_offline(relay_config):
    assert portal.build_pair_url(_FakeBridge(_FakeService(False)), mobile=True) is None
    assert portal.build_pair_url(_FakeBridge(None), mobile=True) is None


def test_pair_url_prefers_mobile_page_when_deployed(relay_config, monkeypatch):
    monkeypatch.setattr(portal, "mobile_page_available", lambda base, device_id: True)
    result = portal.build_pair_url(_FakeBridge(_FakeService(True, "dev-1")), mobile=True)
    assert result is not None
    assert result["url"] == "https://example.com/xy/bot/m/dev-1?token=remote-jwt"
    assert result["token"] == "remote-jwt"
    assert 3500 <= result["expires_in"] <= 3600


def test_pair_url_falls_back_to_desktop_page(relay_config, monkeypatch):
    monkeypatch.setattr(portal, "mobile_page_available", lambda base, device_id: False)
    result = portal.build_pair_url(_FakeBridge(_FakeService(True, "dev-1")), mobile=True)
    assert result["url"] == "https://example.com/xy/bot/desktop/dev-1?token=remote-jwt"
    desktop = portal.build_pair_url(_FakeBridge(_FakeService(True, "dev-1")), mobile=False)
    assert desktop["url"] == "https://example.com/xy/bot/desktop/dev-1?token=remote-jwt"


def test_pair_url_reuses_cached_ticket(relay_config, monkeypatch):
    monkeypatch.setattr(portal, "mobile_page_available", lambda base, device_id: True)
    calls = []

    def _counted(base, device_id, token):
        calls.append(device_id)
        return {"token": f"jwt-{len(calls)}", "expires_at": int(time.time()) + 3600}

    monkeypatch.setattr(portal, "request_ticket", _counted)
    bridge = _FakeBridge(_FakeService(True, "dev-1"))
    first = portal.build_pair_url(bridge, mobile=True)
    second = portal.build_pair_url(bridge, mobile=True)
    assert len(calls) == 1
    assert first["url"] == second["url"]


def test_pair_url_returns_none_when_exchange_fails(relay_config, monkeypatch):
    monkeypatch.setattr(portal, "request_ticket", lambda base, device_id, token: None)
    assert portal.build_pair_url(_FakeBridge(_FakeService(True, "dev-1")), mobile=True) is None


def _fake_mobile_dist(tmp_path):
    frontend = tmp_path / "frontend"
    mobile = frontend / "mobile-dist"
    (mobile / "assets").mkdir(parents=True)
    (mobile / "index.html").write_text(
        "<html><head></head><body>m</body></html>", encoding="utf-8"
    )
    (mobile / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (frontend / "mobile-shim.js").write_text("// shim", encoding="utf-8")
    return mobile


def test_mount_mobile_injects_device_and_serves_assets(tmp_path):
    from gateway.app import create_app, mount_mobile

    mobile = _fake_mobile_dist(tmp_path)
    app = create_app(tmp_path / "gw.db", SECRET)
    mount_mobile(app, mobile)
    client = TestClient(app)

    page = client.get("/m/dev-1")
    assert page.status_code == 200
    assert 'window.__ycRelayDevice="dev-1"' in page.text
    assert "/m/mobile-shim.js" in page.text
    # vite base 是 /m/，资源必须在这个前缀下取到，不能落到 web 通配路由
    assert client.get("/m/assets/app.js").text == "console.log(1)"
    # /m/{device_id} 不得把 shim 当设备号吃掉
    assert client.get("/m/mobile-shim.js").text == "// shim"


def test_mount_mobile_skips_when_package_is_absent(tmp_path):
    from gateway.app import create_app, mount_mobile

    app = create_app(tmp_path / "gw.db", SECRET)
    mount_mobile(app, tmp_path / "missing-mobile-dist")
    assert TestClient(app).get("/m/dev-1").status_code == 404


def test_bind_qr_prefers_public_url_when_relay_connected(monkeypatch):
    """云中转连上时二维码给公网地址，且自带二维码图，前端无需区分来源。"""
    import gui_bridge

    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)
    monkeypatch.setattr(
        bridge,
        "_cloud_relay_pair_url",
        lambda mobile: {
            "url": "https://example.com/xy/bot/m/dev-1?token=jwt",
            "token": "jwt",
            "expires_in": 3600,
        },
    )
    result = bridge.get_remote_bind_qr(mobile=True)
    assert result["ok"] is True
    assert result["url"] == "https://example.com/xy/bot/m/dev-1?token=jwt"
    assert result["qr"].startswith("data:image/png;base64,")
    assert result["expires_in"] == 3600


def test_bind_qr_falls_back_to_lan_when_relay_unavailable(monkeypatch):
    """公网入口拿不到时不能报错，要退回原来的局域网逻辑。"""
    import gui_bridge

    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)
    monkeypatch.setattr(bridge, "_cloud_relay_pair_url", lambda mobile: None)
    monkeypatch.setattr(bridge, "_ensure_lan_server", lambda: None)
    result = bridge.get_remote_bind_qr(mobile=True)
    assert result["ok"] is False
    assert "启动失败" in result["msg"]
