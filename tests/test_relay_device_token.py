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
    data = {
        config.CLOUD_RELAY_URL_KEY: "wss://example.com/xy/bot/relay/device",
        config.CLOUD_RELAY_TOKEN_KEY: "plain-device-token",
    }
    monkeypatch.setattr(config, "_load", lambda: data)
    # 持久化写盘换成内存副本，测试不碰真实 config.json
    monkeypatch.setattr(config, "save_config_value", lambda key, value: data.__setitem__(key, value) is None)
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
        lambda mobile, reason=None, force_new=False: {
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
        bridge,
        "_cloud_relay_pair_url",
        lambda mobile, reason=None, force_new=False: None,
    )
    monkeypatch.setattr(bridge, "_ensure_lan_server", lambda: None)
    result = bridge.get_remote_bind_qr(mobile=True)
    assert result["ok"] is False
    assert "启动失败" in result["msg"]


def test_force_new_bypasses_ticket_cache(relay_config, monkeypatch):
    """用户点「刷新」必须真去网关换新票，不能被本地换票缓存吞掉。"""
    monkeypatch.setattr(portal, "mobile_page_available", lambda base, device_id: True)
    tokens = iter(["jwt-old", "jwt-new"])

    def _ticket(base, device_id, token):
        return {"token": next(tokens), "expires_at": int(time.time()) + 3600}

    monkeypatch.setattr(portal, "request_ticket", _ticket)
    bridge = _FakeBridge(_FakeService(True, "dev-1"))
    first = portal.build_pair_url(bridge, mobile=True)
    cached = portal.build_pair_url(bridge, mobile=True)
    refreshed = portal.build_pair_url(bridge, mobile=True, force_new=True)
    assert first["token"] == cached["token"] == "jwt-old"
    assert refreshed["token"] == "jwt-new"


def test_force_new_keeps_cached_link_when_exchange_fails(relay_config, monkeypatch):
    """换票被网关限流拒绝时，刷新保留仍有效的旧链接并给出提示，不能直接失败。"""
    monkeypatch.setattr(portal, "mobile_page_available", lambda base, device_id: True)
    bridge = _FakeBridge(_FakeService(True, "dev-1"))
    original = portal.build_pair_url(bridge, mobile=True)
    assert original is not None

    monkeypatch.setattr(portal, "request_ticket", lambda base, device_id, token: None)
    refreshed = portal.build_pair_url(bridge, mobile=True, force_new=True)
    assert refreshed is not None
    assert refreshed["token"] == original["token"]
    assert refreshed.get("refresh_notice")


def test_persisted_ticket_survives_restart(relay_config, monkeypatch):
    """桌面重启后要能读回上次的票：否则自动换票会把手机里正在用的链接踢下线。"""
    monkeypatch.setattr(portal, "mobile_page_available", lambda base, device_id: True)
    bridge = _FakeBridge(_FakeService(True, "dev-1"))
    first = portal.build_pair_url(bridge, mobile=True)
    assert first["token"] == "remote-jwt"

    portal._TICKET_CACHE.clear()  # 模拟进程重启：内存缓存丢失
    calls = []

    def _explode(base, device_id, token):
        calls.append(device_id)
        return {"token": "should-not-be-called", "expires_at": int(time.time()) + 3600}

    monkeypatch.setattr(portal, "request_ticket", _explode)
    again = portal.build_pair_url(bridge, mobile=True)
    assert calls == []
    assert again["token"] == "remote-jwt"


def test_force_new_replaces_persisted_ticket_and_warns(relay_config, monkeypatch):
    """显式刷新要换新票、覆盖持久化缓存，并明确告知旧链接已失效。"""
    monkeypatch.setattr(portal, "mobile_page_available", lambda base, device_id: True)
    tokens = iter(["jwt-old", "jwt-new"])
    monkeypatch.setattr(
        portal,
        "request_ticket",
        lambda base, device_id, token: {
            "token": next(tokens),
            "expires_at": int(time.time()) + 3600,
        },
    )
    bridge = _FakeBridge(_FakeService(True, "dev-1"))
    first = portal.build_pair_url(bridge, mobile=True)
    refreshed = portal.build_pair_url(bridge, mobile=True, force_new=True)
    assert first["token"] == "jwt-old"
    assert refreshed["token"] == "jwt-new"
    assert "失效" in refreshed["refresh_notice"]

    portal._TICKET_CACHE.clear()
    monkeypatch.setattr(
        portal,
        "request_ticket",
        lambda *a: {"token": "no-more", "expires_at": int(time.time()) + 3600},
    )
    restored = portal.build_pair_url(bridge, mobile=True)
    assert restored["token"] == "jwt-new"


def test_bind_qr_passes_force_new_to_relay(monkeypatch):
    """刷新按钮的 force_new 必须一路传到云中转换票，并把刷新提示透传给前端。"""
    import gui_bridge

    bridge = gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)
    seen = {}

    def _pair_url(mobile, reason=None, force_new=False):
        seen["force_new"] = force_new
        return {
            "url": "https://example.com/xy/bot/m/dev-1?token=jwt",
            "token": "jwt",
            "expires_in": 3600,
            "refresh_notice": "刷新过于频繁",
        }

    monkeypatch.setattr(bridge, "_cloud_relay_pair_url", _pair_url)
    result = bridge.get_remote_bind_qr(force_new=True, mobile=True)
    assert seen["force_new"] is True
    assert result["refresh_notice"] == "刷新过于频繁"
