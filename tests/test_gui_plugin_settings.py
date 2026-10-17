"""gui_bridge 插件设置接口的单元测试（ApiBridge.__new__ 裸实例 + patch 模块属性）。"""

import pytest

import core.plugin_loader as plugin_loader
from gui_bridge import ApiBridge


def _bare_bridge():
    return ApiBridge.__new__(ApiBridge)


def test_get_plugin_settings_passes_through(monkeypatch):
    page = {"title": "demo", "sections": []}
    monkeypatch.setattr(
        plugin_loader, "call_plugin_hook",
        lambda name, hook, payload=None: {"ok": True, "data": {"page": page}},
    )
    res = _bare_bridge().get_plugin_settings("demo")
    assert res["ok"] is True and res["page"] == page


def test_get_plugin_settings_needs_guide(monkeypatch):
    monkeypatch.setattr(
        plugin_loader, "call_plugin_hook",
        lambda name, hook, payload=None: {"ok": False, "message": "未启用", "needs": "enable"},
    )
    res = _bare_bridge().get_plugin_settings("demo")
    assert res["ok"] is False and res["needs"] == "enable" and "未启用" in res["msg"]


def test_get_plugin_settings_invalid_page(monkeypatch):
    monkeypatch.setattr(
        plugin_loader, "call_plugin_hook",
        lambda name, hook, payload=None: {"ok": True, "data": {"page": "bad"}},
    )
    res = _bare_bridge().get_plugin_settings("demo")
    assert res["ok"] is False


def test_run_plugin_settings_action_envelope(monkeypatch):
    seen = {}

    def fake(name, hook, payload=None):
        seen.update({"name": name, "payload": payload})
        return {"ok": True, "data": {"ok": True, "message": "已切换",
                                     "page": {"title": "x", "sections": []}, "done": True}}

    monkeypatch.setattr(plugin_loader, "call_plugin_hook", fake)
    res = _bare_bridge().run_plugin_settings_action("demo", "switch_account", {"uid": "u1"})
    assert res["ok"] is True and res["done"] is True and res["msg"] == "已切换"
    assert seen["payload"] == {"action": "switch_account", "params": {"uid": "u1"}}


def test_run_plugin_settings_action_swallows_exceptions(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("炸了")

    monkeypatch.setattr(plugin_loader, "call_plugin_hook", boom)
    res = _bare_bridge().run_plugin_settings_action("demo", "x", {})
    assert res["ok"] is False and "炸了" in res["msg"]


def test_open_external_url_rejects_non_http(monkeypatch):
    res = _bare_bridge().open_external_url("file:///etc/passwd")
    assert res["ok"] is False


def test_open_external_url_calls_webbrowser(monkeypatch):
    import webbrowser

    opened = {}

    def fake_open(url):
        opened["url"] = url
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)
    res = _bare_bridge().open_external_url("https://example.com/auth")
    assert res["ok"] is True and opened["url"] == "https://example.com/auth"
