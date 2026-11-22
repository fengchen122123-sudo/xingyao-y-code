from __future__ import annotations

import time

import pytest

import config
from core.cloud_relay import portal


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


def test_bind_qr_prefers_public_url_when_relay_connected(monkeypatch):
    import gui_bridge

    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)
    monkeypatch.setattr(
        bridge,
        "_cloud_relay_pair_url",
        lambda mobile, reason=None: {
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
    import gui_bridge

    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)
    monkeypatch.setattr(
        bridge, "_cloud_relay_pair_url", lambda mobile, reason=None: None
    )
    monkeypatch.setattr(bridge, "_ensure_lan_server", lambda: None)
    result = bridge.get_remote_bind_qr(mobile=True)
    assert result["ok"] is False
    assert "启动失败" in result["msg"]
