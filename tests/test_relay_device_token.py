from __future__ import annotations

import json
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
        lambda base, device_id, token, ttl_seconds=0: {
            "token": "remote-jwt",
            "expires_at": int(time.time()) + 3600,
        },
    )


def test_pair_url_is_none_when_relay_is_offline(relay_config):
    assert portal.build_pair_url(_FakeBridge(_FakeService(False)), mobile=True) is None
    assert portal.build_pair_url(_FakeBridge(None), mobile=True) is None


def test_pair_url_prefers_mobile_page_when_deployed(relay_config, monkeypatch):
    """部署了独立移动端时优先给移动页面，并按票的真实剩余秒数回填 expires_in。

    ttl 显式传 86400：这正是 gui_bridge 上送 config.remote_token_ttl 的调用方式。
    新语义下缺省 ttl=0 表示「永久」，expires_in 会返回 0，不再给出 3600 级数字。
    """
    monkeypatch.setattr(portal, "mobile_page_available", lambda base, device_id: True)
    result = portal.build_pair_url(
        _FakeBridge(_FakeService(True, "dev-1")), mobile=True, ttl_seconds=86400
    )
    assert result is not None
    assert result["url"] == "https://example.com/xy/bot/m/dev-1?token=remote-jwt"
    assert result["token"] == "remote-jwt"
    assert 3500 <= result["expires_in"] <= 3600


def test_pair_url_reports_forever_when_ttl_is_zero(relay_config, monkeypatch):
    """ttl_seconds=0 表示永久有效：expires_in 必须是 0（前端据此显示「永久有效」）。"""
    monkeypatch.setattr(portal, "mobile_page_available", lambda base, device_id: True)
    result = portal.build_pair_url(
        _FakeBridge(_FakeService(True, "dev-1")), mobile=True, ttl_seconds=0
    )
    assert result is not None
    assert result["expires_in"] == 0
    assert result["url"] == "https://example.com/xy/bot/m/dev-1?token=remote-jwt"


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


# ---- 票据有效期可配置：客户端上送 ttl / 缓存比对 / 老缓存兼容 ----


def _install_http_stub(monkeypatch, sent, payload=None):
    """把 httpx.Client 换成内存桩，顺手抓下换票请求体。"""
    import httpx

    body = payload if payload is not None else {
        "token": "remote-jwt",
        "expires_at": int(time.time()) + 3600,
    }

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return body

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def post(self, url, **kwargs):
            sent.append({"url": url, "body": kwargs.get("json")})
            return _Response()

    monkeypatch.setattr(httpx, "Client", _Client)


@pytest.mark.parametrize("ttl", [0, 3600])
def test_request_ticket_sends_ttl_seconds(monkeypatch, ttl):
    """换票请求体必须带 ttl_seconds：网关据此签票（0=永久，正数=对应秒数）。"""
    sent = []
    _install_http_stub(monkeypatch, sent)
    payload = portal.request_ticket(
        "https://example.com/xy/bot", "dev-1", "plain-device-token", ttl_seconds=ttl
    )
    assert payload is not None
    assert sent[0]["url"] == "https://example.com/xy/bot/api/relay/device-token"
    assert sent[0]["body"] == {
        "device_id": "dev-1",
        "device_token": "plain-device-token",
        "ttl_seconds": ttl,
    }


def test_build_pair_url_refetches_only_when_ttl_changes(relay_config, monkeypatch):
    """缓存要连 ttl 一起比：设置没变就命中缓存不换票，改了有效期必须重签一张。"""
    monkeypatch.setattr(portal, "mobile_page_available", lambda base, device_id: True)
    seen = []

    def _ticket(base, device_id, token, ttl_seconds=0):
        seen.append(ttl_seconds)
        return {"token": f"jwt-{len(seen)}", "expires_at": int(time.time()) + 3600}

    monkeypatch.setattr(portal, "request_ticket", _ticket)
    bridge = _FakeBridge(_FakeService(True, "dev-1"))
    first = portal.build_pair_url(bridge, mobile=True, ttl_seconds=3600)
    cached = portal.build_pair_url(bridge, mobile=True, ttl_seconds=3600)
    changed = portal.build_pair_url(bridge, mobile=True, ttl_seconds=0)
    # 只有两次真实换票：3600 那次命中缓存，0 那次因 ttl 变了重签
    assert seen == [3600, 0]
    assert cached["token"] == first["token"]
    assert 3500 <= cached["expires_in"] <= 3600
    assert changed["token"] != first["token"]
    assert changed["expires_in"] == 0


def test_legacy_persisted_ticket_without_ttl_counts_as_zero(relay_config, monkeypatch):
    """老版本落盘的缓存没有 ttl_seconds 字段：按 0（永久）读回，不报错也不白换票。"""
    monkeypatch.setattr("core.credentials.unprotect_secret", lambda value: value)
    monkeypatch.setattr("core.credentials.protect_secret", lambda value: value)
    monkeypatch.setattr(portal, "mobile_page_available", lambda base, device_id: True)
    legacy = json.dumps(
        {
            "device_id": "dev-1",
            "token": "legacy-jwt",
            "expires_at": int(time.time()) + 3600,
        }
    )
    config.save_config_value(config.CLOUD_RELAY_REMOTE_TICKET_KEY, legacy)
    assert portal._persisted_ticket("dev-1")["ttl_seconds"] == 0

    calls = []

    def _explode(*args, **kwargs):
        calls.append(kwargs)
        return {"token": "should-not-be-called", "expires_at": int(time.time()) + 3600}

    monkeypatch.setattr(portal, "request_ticket", _explode)
    result = portal.build_pair_url(
        _FakeBridge(_FakeService(True, "dev-1")), mobile=True, ttl_seconds=0
    )
    assert result is not None
    assert result["token"] == "legacy-jwt"
    assert result["expires_in"] == 0
    assert calls == []
