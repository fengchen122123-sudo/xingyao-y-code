import json
from types import SimpleNamespace

import gui_browser_mixin
import gui_bridge

from gui_browser_mixin import BrowserBridgeMixin


VALID_RECT = {"x": 0, "y": 0, "width": 400, "height": 600}


class Dummy(BrowserBridgeMixin):
    def __init__(self, current_session="A"):
        self._window = SimpleNamespace(uid="master")
        self._current_session = current_session
        self.events = []

    def emit_event(self, event_type, data):
        self.events.append((event_type, data))


class StubController:
    def __init__(self):
        self.calls = []

    def open(self, rect, url="", zoom=1.0):
        self.calls.append(("open", rect, url, zoom))
        return {"ok": True}

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return {"ok": True}

    def hide(self):
        self.calls.append(("hide",))
        return {"ok": True}

    def set_bounds(self, rect, zoom=1.0):
        self.calls.append(("set_bounds", rect, zoom))
        return {"ok": True}


def _dummy(monkeypatch, current="A"):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    d = Dummy(current)
    d._embedded_browser = StubController()
    return d


def test_background_open_is_deferred_and_does_not_touch_controller(monkeypatch):
    d = _dummy(monkeypatch, "A")

    result = d.browser_pane_open(VALID_RECT, "https://example.com", 1.0, "B")

    assert result == {"ok": True, "deferred": True, "session_name": "B"}
    assert d._embedded_browser.calls == []
    assert d.events == [
        (
            "embedded_browser_assigned",
            {"session_name": "B", "url": "https://example.com"},
        )
    ]
    assert d._browser_pane_owner() == ""


def test_foreground_open_executes_and_claims_ownership(monkeypatch):
    d = _dummy(monkeypatch, "A")

    result = d.browser_pane_open(VALID_RECT, "https://example.com", 1.0, "A")

    assert result == {"ok": True}
    assert d._embedded_browser.calls == [
        ("open", VALID_RECT, "https://example.com", 1.0)
    ]
    assert d._browser_pane_owner() == "A"


def test_activate_session_replays_pending_url(monkeypatch):
    d = _dummy(monkeypatch, "A")
    d.browser_pane_open(VALID_RECT, "https://example.com", 1.0, "B")
    d.events.clear()

    result = d.browser_pane_set_active_session("B")

    assert result == {"ok": True, "session_name": "B", "activated": True}
    assert d.events == [
        (
            "embedded_browser_activation",
            {"session_name": "B", "url": "https://example.com"},
        )
    ]
    assert d._browser_pane_owner() == "B"
    assert d.browser_pane_set_active_session("B")["activated"] is False


def test_background_ops_report_not_visible(monkeypatch):
    d = _dummy(monkeypatch, "A")

    assert d.browser_pane_hide("B") == {"ok": False, "error": "not_visible"}
    assert d.browser_pane_reload("B") == {"ok": False, "error": "not_visible"}
    assert d._embedded_browser.calls == []


def test_background_navigate_only_updates_intent(monkeypatch):
    d = _dummy(monkeypatch, "A")

    result = d.browser_pane_navigate("https://example.com/next", "B")

    assert result == {"ok": True, "deferred": True, "session_name": "B"}
    assert d._embedded_browser.calls == []
    assert d._browser_pane_pending()["B"] == {"url": "https://example.com/next"}


def test_assign_records_intent_without_controller(monkeypatch):
    d = _dummy(monkeypatch, "A")

    result = d.browser_pane_assign("https://example.com", "A")

    assert result == {"ok": True, "deferred": True, "session_name": "A"}
    assert d._embedded_browser.calls == []
    assert d.events[0][0] == "embedded_browser_assigned"


def test_state_event_carries_owner_session(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    d = Dummy("A")
    captured = {}

    class FakeController:
        def __init__(self, on_state=None, get_form=None):
            captured["on_state"] = on_state

        def shutdown(self):
            return {"ok": True}

    monkeypatch.setitem(
        __import__("sys").modules,
        "core.embedded_browser",
        SimpleNamespace(EmbeddedBrowserController=FakeController),
    )

    ctrl = d._browser_controller()
    assert ctrl is not None
    d._browser_pane_owner_session = "A"
    captured["on_state"]({"url": "https://example.com", "title": "t"})

    assert d.events[-1] == (
        "embedded_browser",
        {"url": "https://example.com", "title": "t", "session_name": "A"},
    )


def test_gui_context_forwards_browser_bridge_methods():

    class FakeBridge:
        def browser_pane_open(self, rect, url="", zoom=1.0, session_name=""):
            return {"ok": True, "args": (rect, url, zoom, session_name)}

        def browser_pane_assign(self, url="", session_name=""):
            return {"ok": True, "args": (url, session_name)}

    ctx = gui_bridge._GuiContext(FakeBridge())

    assert ctx.browser_pane_open(VALID_RECT, "u", 1.0, "B")["args"][3] == "B"
    assert ctx.browser_pane_assign("u", "B")["args"] == ("u", "B")
    try:
        ctx._window
    except AttributeError:
        pass
    else:  # pragma: no cover - 防御断言
        raise AssertionError("_GuiContext 不应暴露非白名单属性")


def test_tool_open_without_rect_uses_assign(monkeypatch):
    from core import app_context
    from core import tools_admin_interaction as tools

    calls = []

    class FakeCtx:
        current_session = "B"

        def browser_pane_assign(self, url, session_name=""):
            calls.append((url, session_name))
            return {"ok": True, "deferred": True, "session_name": session_name}

    monkeypatch.setattr(app_context, "get", lambda: FakeCtx())
    monkeypatch.setattr(app_context, "get_thread_session", lambda: "B")

    result = json.loads(tools.browser_pane("open", url="https://example.com"))

    assert result["ok"] is True
    assert result["deferred"] is True
    assert calls == [("https://example.com", "B")]


def test_tool_open_with_rect_passes_session(monkeypatch):
    from core import app_context
    from core import tools_admin_interaction as tools

    calls = []

    class FakeCtx:
        current_session = "B"

        def browser_pane_open(self, rect, url="", zoom=1.0, session_name=""):
            calls.append((rect, url, zoom, session_name))
            return {"ok": True}

    monkeypatch.setattr(app_context, "get", lambda: FakeCtx())
    monkeypatch.setattr(app_context, "get_thread_session", lambda: "B")

    result = json.loads(
        tools.browser_pane("open", url="https://example.com", rect=VALID_RECT)
    )

    assert result["ok"] is True
    assert calls == [(VALID_RECT, "https://example.com", 1.0, "B")]


def test_tool_to_gui_context_to_mixin_end_to_end(monkeypatch):
    from core import app_context
    from core import tools_admin_interaction as tools

    class Bridge(BrowserBridgeMixin):
        def __init__(self):
            self._current_session = "A"
            self.events = []
            self._embedded_browser = StubController()

        def emit_event(self, event_type, data):
            self.events.append((event_type, data))

    bridge = Bridge()
    ctx = gui_bridge._GuiContext(bridge)
    monkeypatch.setattr(app_context, "get", lambda: ctx)
    monkeypatch.setattr(app_context, "get_thread_session", lambda: "B")

    result = json.loads(tools.browser_pane("open", url="https://example.com"))

    assert result["ok"] is True
    assert result["deferred"] is True
    assert bridge._embedded_browser.calls == []
    assert bridge.events == [
        (
            "embedded_browser_assigned",
            {"session_name": "B", "url": "https://example.com"},
        )
    ]


def test_tool_not_visible_error_is_chinese(monkeypatch):
    from core import app_context
    from core import tools_admin_interaction as tools

    class FakeCtx:
        current_session = "B"

        def browser_pane_back(self, session_name=""):
            return {"ok": False, "error": "not_visible"}

    monkeypatch.setattr(app_context, "get", lambda: FakeCtx())
    monkeypatch.setattr(app_context, "get_thread_session", lambda: "B")

    result = json.loads(tools.browser_pane("back"))

    assert result["ok"] is False
    assert "不在前台" in result["error"]


def test_set_active_session_does_not_clobber_already_visible_target_surface(monkeypatch):
    d = _dummy(monkeypatch, "B")
    d._embedded_browser._visible = True
    d._browser_pane_owner_session = "B"
    d._browser_pane_mark_content("B")

    result = d.browser_pane_set_active_session("B")

    assert result == {"ok": True, "session_name": "B", "activated": False}
    assert d._embedded_browser.calls == []

    d._browser_pane_owner_session = "A"
    d._browser_pane_mark_content("A")
    result2 = d.browser_pane_set_active_session("B")

    assert result2 == {"ok": True, "session_name": "B", "activated": False}
    assert d._embedded_browser.calls == [("hide",)]

