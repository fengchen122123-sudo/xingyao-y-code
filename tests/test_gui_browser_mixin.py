from pathlib import Path
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

    def open(self, rect, url="", zoom=1.0):
        self.calls.append(("open", rect, url, zoom))
        return self.result

    def set_bounds(self, rect, zoom=1.0):
        self.calls.append(("set_bounds", rect, zoom))
        return self.result

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return self.result

    def set_session_cookies(self, cookies, domain="tokenrhythm.studio"):
        self.calls.append(("set_session_cookies", cookies, domain))
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
    assert stub.calls == [("open", VALID_RECT, "https://example.com", 1.0)]


def test_open_and_set_bounds_pass_zoom_through(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    d = Dummy()
    stub = StubController()
    d._embedded_browser = stub

    d.browser_pane_open(VALID_RECT, "", 0.35)
    d.browser_pane_set_bounds(VALID_RECT, 0.35)

    assert stub.calls == [
        ("open", VALID_RECT, "", 0.35),
        ("set_bounds", VALID_RECT, 0.35),
    ]


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


TOKENRHYTHM_URL = "https://tokenrhythm.studio/account"
FAKE_COOKIES = [
    {"name": "sid", "value": "secret-value", "domain": "tokenrhythm.studio", "path": "/"}
]


def _patch_export_session_cookies(monkeypatch, result):
    import core.tokenrhythm_auth as ta

    calls = []

    def _fake_export():
        calls.append(True)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(ta, "export_session_cookies", _fake_export)
    return calls


def test_navigate_injects_tokenrhythm_session_cookies(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    export_calls = _patch_export_session_cookies(monkeypatch, FAKE_COOKIES)
    d = Dummy()
    stub = StubController()
    d._embedded_browser = stub

    result = d.browser_pane_navigate(TOKENRHYTHM_URL)

    assert result["ok"] is True
    assert export_calls == [True]
    assert stub.calls == [
        ("set_session_cookies", FAKE_COOKIES, "tokenrhythm.studio"),
        ("navigate", TOKENRHYTHM_URL),
    ]


def test_open_injects_tokenrhythm_session_cookies(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    _patch_export_session_cookies(monkeypatch, FAKE_COOKIES)
    d = Dummy()
    stub = StubController()
    d._embedded_browser = stub

    result = d.browser_pane_open(VALID_RECT, TOKENRHYTHM_URL)

    assert result["ok"] is True
    assert stub.calls == [
        ("set_session_cookies", FAKE_COOKIES, "tokenrhythm.studio"),
        ("open", VALID_RECT, TOKENRHYTHM_URL, 1.0),
    ]


def test_subdomain_url_also_injects(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    _patch_export_session_cookies(monkeypatch, FAKE_COOKIES)
    d = Dummy()
    stub = StubController()
    d._embedded_browser = stub

    d.browser_pane_navigate("https://console.tokenrhythm.studio/login")

    assert ("set_session_cookies", FAKE_COOKIES, "tokenrhythm.studio") in stub.calls


def test_non_tokenrhythm_url_skips_injection(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    export_calls = _patch_export_session_cookies(monkeypatch, FAKE_COOKIES)
    d = Dummy()
    stub = StubController()
    d._embedded_browser = stub

    assert d.browser_pane_navigate("https://example.com")["ok"] is True

    assert export_calls == []
    assert stub.calls == [("navigate", "https://example.com")]


def test_export_failure_does_not_break_navigation(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    _patch_export_session_cookies(monkeypatch, RuntimeError("token file unreadable"))
    d = Dummy()
    stub = StubController()
    d._embedded_browser = stub

    result = d.browser_pane_navigate(TOKENRHYTHM_URL)

    assert result["ok"] is True
    assert stub.calls == [("navigate", TOKENRHYTHM_URL)]


def test_empty_cookie_list_skips_injection(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    _patch_export_session_cookies(monkeypatch, [])
    d = Dummy()
    stub = StubController()
    d._embedded_browser = stub

    assert d.browser_pane_navigate(TOKENRHYTHM_URL)["ok"] is True

    assert stub.calls == [("navigate", TOKENRHYTHM_URL)]


def test_controller_lazy_singleton_wires_on_state(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    d = Dummy()

    ctrl = d._browser_controller()

    assert ctrl is not None
    assert d._browser_controller() is ctrl
    ctrl._on_state({"url": "https://x.com"})
    assert d.events == [
        ("embedded_browser", {"url": "https://x.com", "session_name": ""})
    ]


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


def test_automation_endpoint_emits_session_name(monkeypatch):
    from core.computer.models import ComputerControlError

    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")

    class AutoController:
        def __init__(self):
            self.prepared = 0

        def prepare_automation(self):
            self.prepared += 1
            return {"ok": True, "endpoint": "http://127.0.0.1:9222"}

    d = Dummy()
    stub = AutoController()
    d._embedded_browser = stub
    monkeypatch.setattr(
        "core.computer.settings.get_project_browser_viewport",
        lambda _workdir: (1280, 800),
    )
    monkeypatch.setattr("core.workspace_context.get_workdir", lambda: "D:/proj")

    endpoint = d.embedded_browser_automation_endpoint("sess-A")

    assert endpoint == "http://127.0.0.1:9222"
    assert stub.prepared == 1
    assert d.events == [
        (
            "embedded_browser_automation",
            {"width": 1280, "height": 800, "session_name": "sess-A"},
        )
    ]

    d2 = Dummy()
    try:
        d2.embedded_browser_automation_endpoint("sess-B")
    except ComputerControlError as exc:
        assert exc.code == "computer_embedded_unavailable"
    else:
        raise AssertionError("expected ComputerControlError")
    assert d2.events == []


def test_write_preview_page_writes_file(tmp_path, monkeypatch):
    import core.web_preview as wp

    monkeypatch.setattr(wp, "get_preview_dir", lambda: tmp_path)
    d = Dummy()

    result = d.write_preview_page("<html>rendered</html>", r"D:\proj\a.md")

    assert result["ok"] is True
    assert Path(result["path"]).read_text(encoding="utf-8") == "<html>rendered</html>"


def test_write_preview_page_error_is_structured():
    d = Dummy()

    result = d.write_preview_page("   ")

    assert result["ok"] is False
    assert result["error"]


def test_browser_pane_forwards_surface_generation_when_provided(monkeypatch):
    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")

    class GenController(StubController):
        def open(self, rect, url="", zoom=1.0, surface_generation=None):
            self.calls.append(("open", rect, url, zoom, surface_generation))
            return {"ok": True, "ack": True, "surface_generation": surface_generation}

        def set_bounds(self, rect, zoom=1.0, surface_generation=None):
            self.calls.append(("set_bounds", rect, zoom, surface_generation))
            return {"ok": True, "ack": True, "surface_generation": surface_generation}

        def hide(self, surface_generation=None):
            self.calls.append(("hide", surface_generation))
            return {"ok": True, "ack": True, "surface_generation": surface_generation}

    d = Dummy()
    ctrl = GenController()
    d._embedded_browser = ctrl

    r1 = d.browser_pane_open(VALID_RECT, "https://example.com", 1.0, "", surface_generation=7)
    r2 = d.browser_pane_set_bounds(VALID_RECT, 1.0, "", surface_generation=7)
    r3 = d.browser_pane_hide("", surface_generation=8)

    assert r1["surface_generation"] == 7
    assert r2["surface_generation"] == 7
    assert r3["surface_generation"] == 8
    assert ctrl.calls == [
        ("open", VALID_RECT, "https://example.com", 1.0, 7),
        ("set_bounds", VALID_RECT, 1.0, 7),
        ("hide", 8),
    ]


def test_automation_endpoint_prompts_confirmation_via_permission_broker_for_non_cdp(monkeypatch):
    from core.computer.models import ComputerControlError
    from core.permission_broker import PermissionBroker

    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    monkeypatch.setattr("core.computer.settings.get_project_browser_viewport", lambda _w: (1280, 800))
    monkeypatch.setattr("core.workspace_context.get_workdir", lambda: "D:/proj")

    class NonCdpController:
        def __init__(self):
            self.confirmed = []

        def prepare_automation(self, *, confirm_recreate=None):
            ok = bool(confirm_recreate and confirm_recreate("https://example.com/user-page"))
            self.confirmed.append(ok)
            if not ok:
                return {"ok": False, "error": "recreate_confirmation_required", "msg": "需要用户确认刷新接管"}
            return {"ok": True, "endpoint": "http://127.0.0.1:9444", "recreated": True}

    prompts = []
    broker = PermissionBroker(
        activate_prompt=lambda req: (prompts.append(req), broker.resolve_by_key(req.request_id, "deny")),
        dismiss_prompt=lambda: None,
    )
    monkeypatch.setattr("core.permission_broker.permission_broker", broker)

    d = Dummy()
    d._embedded_browser = NonCdpController()
    try:
        d.embedded_browser_automation_endpoint("sess-A")
    except ComputerControlError as exc:
        assert exc.code == "computer_embedded_recreate_required"
    else:
        raise AssertionError("expected ComputerControlError when recreate declined")
    assert len(prompts) == 1
    assert prompts[0].function_name == "embedded_browser_recreate"
    assert d._embedded_browser.confirmed == [False]

    broker2 = PermissionBroker(
        activate_prompt=lambda req: broker2.resolve_by_key(req.request_id, "allow_once"),
        dismiss_prompt=lambda: None,
    )
    monkeypatch.setattr("core.permission_broker.permission_broker", broker2)
    endpoint = d.embedded_browser_automation_endpoint("sess-A")
    assert endpoint == "http://127.0.0.1:9444"
    assert d._embedded_browser.confirmed == [False, True]