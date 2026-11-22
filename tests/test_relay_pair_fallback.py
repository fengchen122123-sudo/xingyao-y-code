from __future__ import annotations

import time

import pytest

import config
from core.cloud_relay import portal


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
    monkeypatch.setattr(portal, "mobile_page_available", lambda base, device_id: True)


def test_offline_relay_reports_reason(relay_config):
    reason: dict = {}
    result = portal.build_pair_url(
        _FakeBridge(_FakeService(False)), mobile=True, reason=reason
    )
    assert result is None
    assert reason["code"] == "offline"
    assert "局域网" in reason["message"]


def test_disabled_relay_reports_reason(relay_config):
    reason: dict = {}
    assert (
        portal.build_pair_url(_FakeBridge(None), mobile=True, reason=reason) is None
    )
    assert reason["code"] == "disabled"


def test_ticket_failure_reports_reason(relay_config, monkeypatch):
    monkeypatch.setattr(portal, "request_ticket", lambda base, device_id, token: None)
    reason: dict = {}
    result = portal.build_pair_url(
        _FakeBridge(_FakeService(True)), mobile=True, reason=reason
    )
    assert result is None
    assert reason["code"] == "ticket"


def test_success_leaves_reason_empty(relay_config):
    reason: dict = {}
    result = portal.build_pair_url(
        _FakeBridge(_FakeService(True)), mobile=True, reason=reason
    )
    assert result is not None
    assert result["url"] == "https://example.com/xy/bot/m/dev-1?token=remote-jwt"
    assert reason == {}


class _FakeLanServer:
    def get_bind_info(self, force_new: bool = False, path: str = "/"):
        return {
            "ok": True,
            "url": f"http://192.168.1.5:8765{path}?token=lan-token",
            "token": "lan-token",
            "qr": "data:image/png;base64,LAN",
            "expires_in": 600,
        }


def _bridge_with_offline_relay():
    import gui_bridge

    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)

    def _pair_url(mobile: bool, reason=None):
        if reason is not None:
            reason["code"] = "offline"
            reason["message"] = "云中转未连上，已使用局域网地址（手机需与电脑连同一 WiFi）"
        return None

    bridge._cloud_relay_pair_url = _pair_url
    bridge._ensure_lan_server = lambda: _FakeLanServer()
    return bridge


def test_bind_qr_surfaces_notice_when_relay_enabled(monkeypatch):
    bridge = _bridge_with_offline_relay()
    monkeypatch.setattr(config, "_load", lambda: {config.CLOUD_RELAY_ENABLED_KEY: True})
    result = bridge.get_remote_bind_qr(mobile=False)
    assert result["ok"] is True
    assert result["url"].startswith("http://192.168.1.5:8765")
    assert "云中转未连上" in result["relay_notice"]


def test_bind_qr_has_no_notice_when_relay_disabled(monkeypatch):
    bridge = _bridge_with_offline_relay()
    monkeypatch.setattr(config, "_load", lambda: {config.CLOUD_RELAY_ENABLED_KEY: False})
    result = bridge.get_remote_bind_qr(mobile=False)
    assert result["ok"] is True
    assert "relay_notice" not in result
