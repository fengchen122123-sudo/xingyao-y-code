"""GUI 插件信任入口（trust_plugin 桥接方法）的单元测试。

复用 test_gui_mcp.py 的 _bare_bridge 模式：ApiBridge.__new__ 绕过 __init__，
只设置测试所需属性；trust_plugin 内部为局部导入
（from core.plugin_loader import trust_plugin），因此打桩需 patch
core.plugin_loader 模块属性。
"""


import core.plugin_loader as plugin_loader
from gui_bridge import ApiBridge


def _bare_bridge(get_plugins=None):
    """不经 __init__ 构造裸实例；可选地给 get_plugins 打桩。"""
    bridge = ApiBridge.__new__(ApiBridge)
    if get_plugins is not None:
        bridge.get_plugins = lambda: get_plugins
    return bridge


def test_trust_plugin_success_returns_plugins_list(monkeypatch):
    """信任成功：ok=True、msg 透传底层消息、响应携带刷新后的 plugins 列表。"""
    monkeypatch.setattr(plugin_loader, "trust_plugin", lambda name: (True, "已信任插件 demo"))
    plugins = [{"name": "demo", "status": "loaded", "enabled": True}]
    bridge = _bare_bridge(get_plugins=plugins)

    res = bridge.trust_plugin("demo")

    assert res["ok"] is True
    assert res["msg"] == "已信任插件 demo"
    assert res["plugins"] == plugins


def test_trust_plugin_not_found_returns_ok_false(monkeypatch):
    """底层返回 (False, msg)：透传失败，不向上抛异常。"""
    monkeypatch.setattr(plugin_loader, "trust_plugin", lambda name: (False, "未找到插件 x"))
    bridge = _bare_bridge(get_plugins=[])

    res = bridge.trust_plugin("x")

    assert res["ok"] is False
    assert "未找到" in res["msg"]


def test_trust_plugin_swallows_exceptions(monkeypatch):
    """底层抛异常：桥接层捕获并返回 {"ok": False, "msg": 异常文本}。"""

    def boom(name):
        raise RuntimeError("信任流程崩溃")

    monkeypatch.setattr(plugin_loader, "trust_plugin", boom)
    bridge = _bare_bridge()

    res = bridge.trust_plugin("y")

    assert res["ok"] is False
    assert "崩溃" in res["msg"]
