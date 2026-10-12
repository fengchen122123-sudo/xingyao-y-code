"""plugin_loader.call_plugin_hook 与"删除插件时完整卸载"的单元测试。

隔离约定：refresh_plugins 的扫盘段通过打桩 plugin_dirs() 返回空列表隔离，
绝不触碰真实插件目录。
"""

import types

import core.plugin_loader as plugin_loader


class _FakeRegistry:
    def __init__(self):
        self.registered = {}
        self.unregistered = []

    def register(self, spec):
        self.registered[spec.name] = spec

    def unregister(self, name):
        self.unregistered.append(name)
        self.registered.pop(name, None)

    def all(self):
        return tuple(self.registered.values())


def _isolate(monkeypatch, infos=None, modules=None):
    """隔离插件加载器全局状态：空扫描目录 + 指定的 infos/modules。"""
    monkeypatch.setattr(plugin_loader, "plugin_dirs", lambda: [])
    fake_infos = list(infos or [])
    fake_modules = dict(modules or {})
    monkeypatch.setattr(plugin_loader, "_PLUGIN_INFOS", fake_infos)
    monkeypatch.setattr(plugin_loader, "_LOADED_MODULES", fake_modules)
    return fake_infos, fake_modules, _FakeRegistry()


def test_call_plugin_hook_requires_trust(monkeypatch):
    _isolate(monkeypatch, infos=[{"name": "demo", "status": "untrusted"}])
    result = plugin_loader.call_plugin_hook("demo", "plugin_settings", {"action": "render"})
    assert result["ok"] is False and result["needs"] == "trust"


def test_call_plugin_hook_disabled_needs_enable(monkeypatch):
    _isolate(monkeypatch, infos=[{"name": "demo", "status": "disabled"}])
    result = plugin_loader.call_plugin_hook("demo", "plugin_settings")
    assert result["ok"] is False and result["needs"] == "enable"


def test_call_plugin_hook_invokes_hook_and_passes_payload(monkeypatch):
    seen = {}

    def fake_hook(payload):
        seen.update(payload)
        return {"page": {"title": "demo"}}

    module = types.SimpleNamespace(plugin_settings=fake_hook)
    _isolate(monkeypatch, infos=[{"name": "demo", "status": "loaded"}], modules={"demo": module})
    result = plugin_loader.call_plugin_hook("demo", "plugin_settings", {"action": "render"})
    assert result == {"ok": True, "data": {"page": {"title": "demo"}}}
    assert seen == {"action": "render"}


def test_call_plugin_hook_missing_hook(monkeypatch):
    module = types.SimpleNamespace()
    _isolate(monkeypatch, infos=[{"name": "demo", "status": "loaded"}], modules={"demo": module})
    result = plugin_loader.call_plugin_hook("demo", "plugin_settings")
    assert result["ok"] is False and "未提供" in result["message"]


def test_call_plugin_hook_swallows_exceptions(monkeypatch):
    def boom(payload):
        raise RuntimeError("钩子崩了")

    module = types.SimpleNamespace(plugin_settings=boom)
    _isolate(monkeypatch, infos=[{"name": "demo", "status": "loaded"}], modules={"demo": module})
    result = plugin_loader.call_plugin_hook("demo", "plugin_settings")
    assert result["ok"] is False and "钩子崩了" in result["message"]


def test_call_plugin_hook_rejects_non_dict(monkeypatch):
    module = types.SimpleNamespace(plugin_settings=lambda payload: "not-a-dict")
    _isolate(monkeypatch, infos=[{"name": "demo", "status": "loaded"}], modules={"demo": module})
    result = plugin_loader.call_plugin_hook("demo", "plugin_settings")
    assert result["ok"] is False


def test_refresh_unloads_removed_plugin(monkeypatch, tmp_path):
    """插件目录被删除：记录移除、工具注销、cleanup 调用、模块引用清理。"""
    calls = []
    module = types.SimpleNamespace(cleanup=lambda: calls.append("cleanup"))
    infos, _, registry = _isolate(
        monkeypatch,
        infos=[{
            "name": "demo",
            "path": str(tmp_path / "demo-missing"),  # 目录不存在 → 触发删除分支
            "status": "loaded",
            "tools": ["demo_tool"],
        }],
        modules={"demo": module},
    )
    registry.registered["demo_tool"] = object()

    plugin_loader.refresh_plugins(registry)

    assert infos == []
    assert "demo" not in plugin_loader._LOADED_MODULES
    assert registry.unregistered == ["demo_tool"]
    assert calls == ["cleanup"]
