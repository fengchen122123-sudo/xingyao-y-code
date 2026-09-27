"""gu`i_browser_mixin.BrowserBridgeMixin 的 headless 测试。

用 Dummy(Mixin) 组合类 + stub 控制器，覆盖：非 win32 拒绝、rect 校验、
navigate 透传、控制器抛异常兜底、on_state→emit_event 接线。
"""

from types import SimpleNamespace

import gui_browser_mixin
from gui_browser_mixin import BrowserBridgeMixin


class Dummy(BrowserBridgeMixin):
    def __init__(self):
        self._window = SimpleNamespace(uid="master")
        self.events = []

    def emit_event(self, event_type, data):
        self.events.append((event_type, data))


class StubController:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": True, "state": {"url": "https://example.com"}}

    def open(self, rect, url=""):
        self.calls.append(("open", rect, url))
        return self.result

    def set_bounds(self, rect):
        self.calls.append(("set_bounds", rect))
        return self.result

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return self.result

    def back(self):
        self.calls.append(("back",))
        return self.result

    def forward(self):
        self.calls.append(("forward",))
        return self.result

    def reload(self):
        self.calls.append(("reload",))
        return self.result

    def stop(self):
        self.calls.append(("stop",))
        return self.result

    def hide(self):
        self.calls.append(("hide",))
        return self.result

    def get_state(self):
        self.calls.append(("get_state",))
        return self.result


VALID_RECT = {"x": 0, "y": 0, "width": 400, "height": 600}


def test_non_windows_reports_unsupported(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "linux")
    d = Dummy()
    d._embedded_browser = StubController()

    assert d.browser_pane_open(VALID_RECT) == {"ok": False, "error": "unsupported_platform"}
    assert d.browser_pane_navigate("https://x.com") == {
        "ok": False,
        "error": "unsupported_platform",
    }
    # S5：set_bounds 的平台守卫与其余桥方法一致（此前返回 not_open）
    assert d.browser_pane_set_bounds(VALID_RECT) == {
        "ok": False,
        "error": "unsupported_platform",
    }


def test_invalid_rect_is_rejected_before_controller_use():
    d = Dummy()
    stub = StubController()
    d._embedded_browser = stub

    assert d.browser_pane_open("not-a-dict") == {"ok": False, "error": "invalid_rect"}
    assert d.browser_pane_open({"x": 1, "y": 2}) == {"ok": False, "error": "invalid_rect"}
    assert d.browser_pane_set_bounds({"x": 1, "width": 2}) == {
        "ok": False,
        "error": "invalid_rect",
    }
    assert stub.calls == []


def test_open_passes_rect_and_url_to_controller(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    d = Dummy()
    stub = StubController()
    d._embedded_browser = stub

    result = d.browser_pane_open(VALID_RECT, "https://example.com")

    assert result["ok"] is True
    assert stub.calls == [("open", VALID_RECT, "https://example.com")]


def test_navigate_returns_controller_state(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    d = Dummy()
    stub = StubController({"ok": True, "state": {"url": "https://target.dev"}})
    d._embedded_browser = stub

    result = d.browser_pane_navigate("https://target.dev")

    assert result == {"ok": True, "state": {"url": "https://target.dev"}}
    assert stub.calls == [("navigate", "https://target.dev")]


def test_controller_exception_is_swallowed(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")

    class BoomController(StubController):
        def navigate(self, url):
            raise RuntimeError("boom")

    d = Dummy()
    d._embedded_browser = BoomController()

    assert d.browser_pane_navigate("https://x.com") == {"ok": False, "error": "boom"}


def test_controller_lazy_singleton_wires_on_state(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    d = Dummy()

    ctrl = d._browser_controller()

    assert ctrl is not None
    assert d._browser_controller() is ctrl  # 单例
    # on_state → emit_event('embedded_browser', ...)
    ctrl._on_state({"url": "https://x.com"})
    assert d.events == [("embedded_browser", {"url": "https://x.com"})]


def test_controller_creation_failure_reports_unsupported(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("no webview2")

    d = Dummy()
    monkeypatch.setattr(
        "core.embedded_browser.EmbeddedBrowserController",
        _boom,
    )

    assert d.browser_pane_navigate("https://x.com") == {
        "ok": False,
        "error": "unsupported_platform",
    }


def test_hide_and_get_state_without_controller_are_safe():
    d = Dummy()
    assert d.browser_pane_hide() == {"ok": True}
    assert d.browser_pane_get_state() == {"ok": True, "state": None}