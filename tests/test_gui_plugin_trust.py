import core.plugin_loader as plugin_loader
from gui_bridge import ApiBridge


def _bare_bridge(get_plugins=None):
    bridge = ApiBridge.__new__(ApiBridge)
    if get_plugins is not None:
        bridge.get_plugins = lambda: get_plugins
    return bridge


def test_trust_plugin_success_returns_plugins_list(monkeypatch):
    monkeypatch.setattr(plugin_loader, "trust_plugin", lambda name: (True, "已信任插件 demo"))
    plugins = [{"name": "demo", "status": "loaded", "enabled": True}]
    bridge = _bare_bridge(get_plugins=plugins)

    res = bridge.trust_plugin("demo")

    assert res["ok"] is True
    assert res["msg"] == "已信任插件 demo"
    assert res["plugins"] == plugins


def test_trust_plugin_not_found_returns_ok_false(monkeypatch):
    monkeypatch.setattr(plugin_loader, "trust_plugin", lambda name: (False, "未找到插件 x"))
    bridge = _bare_bridge(get_plugins=[])

    res = bridge.trust_plugin("x")

    assert res["ok"] is False
    assert "未找到" in res["msg"]


def test_trust_plugin_swallows_exceptions(monkeypatch):

    def boom(name):
        raise RuntimeError("信任流程崩溃")

    monkeypatch.setattr(plugin_loader, "trust_plugin", boom)
    bridge = _bare_bridge()

    res = bridge.trust_plugin("y")

    assert res["ok"] is False
    assert "崩溃" in res["msg"]
