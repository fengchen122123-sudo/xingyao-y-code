import logging
from types import SimpleNamespace
import time

import pytest

from core import embedded_browser as eb


@pytest.mark.parametrize(
    ("rect", "scale", "expected"),
    [
        ({"x": 10, "y": 20, "width": 300, "height": 200}, 1.0, (10, 20, 300, 200)),
        ({"x": 4, "y": 8, "width": 40, "height": 80}, 1.25, (5, 10, 50, 100)),
        ({"x": 10, "y": 20, "width": 300, "height": 200}, 2.0, (20, 40, 600, 400)),
        ({"x": -5, "y": -3, "width": -40, "height": -80}, 1.0, (0, 0, 0, 0)),
        ({"x": -5, "y": 10, "width": 40, "height": 80}, 2.0, (0, 20, 80, 160)),
        ({"x": 7}, 1.0, (7, 0, 0, 0)),
    ],
)
def test_css_rect_to_physical(rect, scale, expected):
    assert eb.css_rect_to_physical(rect, scale) == expected


def test_css_rect_to_physical_handles_bad_scale_and_values():
    assert eb.css_rect_to_physical({"x": "abc", "y": 5}, None) == (0, 5, 0, 0)
    assert eb.css_rect_to_physical({"x": 3}, "bad") == (3, 0, 0, 0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("   ", ""),
        ("about:blank", "about:blank"),
        ("  about:blank  ", "about:blank"),
        ("https://example.com/a", "https://example.com/a"),
        ("http://example.com", "http://example.com"),
        ("//example.com/x", "https://example.com/x"),
        ("example.com", "https://example.com"),
        ("example.com:8080/path?q=1", "https://example.com:8080/path?q=1"),
        ("localhost:3000", "https://localhost:3000"),
        ("localhost", "https://localhost"),
        ("192.168.1.1", "https://192.168.1.1"),
        ("192.168.1.1:8080", "https://192.168.1.1:8080"),
        ("javascript:alert(1)", ""),
        ("data:text/html,<h1>x</h1>", ""),
        ("mailto:a@b.com", ""),
        ("about:config", ""),
        ("file:///C:/x.txt", "file:///C:/x.txt"),
        ("file:///D:/proj/a.html", "file:///D:/proj/a.html"),
        ("C:\\proj\\a.html", "file:///C:/proj/a.html"),
        ("D:/proj/index.html", "file:///D:/proj/index.html"),
        ("\\\\srv\\share\\a.html", "file://srv/share/a.html"),
        ("file:///D:/My Docs/a b.html", "file:///D:/My%20Docs/a%20b.html"),
        ("file:///D:/文档/a.html", "file:///D:/%E6%96%87%E6%A1%A3/a.html"),
        ("file:relative.txt", ""),
        ("file://relative.txt", ""),
        ("file:///D:/a/../b.html", ""),
        ("../a.html", ""),
        ("hello world", ""),
        ("just-a-word", ""),
        ("foo.123", ""),
    ],
)
def test_normalize_url(raw, expected):
    assert eb.normalize_url(raw) == expected


def test_normalize_url_rejects_non_string():
    assert eb.normalize_url(None) == ""
    assert eb.normalize_url(123) == ""


def test_is_supported_matches_platform(monkeypatch):
    monkeypatch.setattr(eb.sys, "platform", "win32")
    assert eb.is_supported() is True
    monkeypatch.setattr(eb.sys, "platform", "linux")
    assert eb.is_supported() is False


def _controller():
    return eb.EmbeddedBrowserController(on_state=lambda _state: None, get_form=lambda: None)


def test_controller_open_without_form_fails_gracefully(monkeypatch):
    monkeypatch.setattr(eb.sys, "platform", "win32")
    ctrl = _controller()
    result = ctrl.open({"x": 0, "y": 0, "width": 100, "height": 100}, "https://example.com")
    assert result["ok"] is False
    assert result["error"] == "form_unavailable"


def test_controller_open_unsupported_platform(monkeypatch):
    monkeypatch.setattr(eb.sys, "platform", "linux")
    ctrl = _controller()
    result = ctrl.open({"x": 0, "y": 0, "width": 10, "height": 10})
    assert result == {"ok": False, "error": "unsupported_platform"}


def test_controller_methods_never_raise_without_form():
    ctrl = _controller()
    rect = {"x": 0, "y": 0, "width": 100, "height": 100}
    for result in (
        ctrl.set_bounds(rect),
        ctrl.back(),
        ctrl.forward(),
        ctrl.reload(),
        ctrl.stop(),
        ctrl.hide(),
    ):
        assert isinstance(result, dict)
        assert result["ok"] is False
    state = ctrl.get_state()
    assert isinstance(state, dict)
    assert state["ok"] is True
    assert state["state"]["visible"] is False
    assert ctrl.shutdown() == {"ok": True}


def test_controller_navigate_rejects_invalid_url():
    ctrl = _controller()
    result = ctrl.navigate("javascript:alert(1)")
    assert result == {"ok": False, "error": "invalid_url"}


def test_navigate_before_open_queues_pending_url():
    ctrl = _controller()
    result = ctrl.navigate("https://example.com")
    assert result["ok"] is True
    assert ctrl._pending_url == "https://example.com"
    assert ctrl.get_state()["state"]["url"] == "https://example.com"


class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _FakeSettings:
    IsStatusBarEnabled = True


class _FakeCookieManager:

    def __init__(self):
        self.created = []
        self.added = []

    def CreateCookie(self, name, value, domain, path):
        cookie = SimpleNamespace(
            Name=name,
            Value=value,
            Domain=domain,
            Path=path,
            IsHttpOnly=False,
            IsSecure=False,
        )
        self.created.append(cookie)
        return cookie

    def AddOrUpdateCookie(self, cookie):
        self.added.append(cookie)


class _FakeCore:
    def __init__(self):
        self.navigated = []
        self.Settings = _FakeSettings()
        self.CookieManager = _FakeCookieManager()
        self.Source = ""
        self.DocumentTitle = ""
        self.CanGoBack = False
        self.CanGoForward = False
        self.SourceChanged = _FakeEvent()
        self.DocumentTitleChanged = _FakeEvent()
        self.NavigationStarting = _FakeEvent()
        self.NavigationCompleted = _FakeEvent()
        self.NewWindowRequested = _FakeEvent()

    def Navigate(self, url):
        self.navigated.append(url)


class _FakeHandle:
    def ToInt32(self):
        return 1


class _FakeForm:

    def __init__(self):
        self.Handle = _FakeHandle()
        self.invoked = 0

    def Invoke(self, delegate):
        self.invoked += 1
        delegate()


def _fake_ready_controller():
    ctrl = eb.EmbeddedBrowserController(on_state=lambda _state: None, get_form=lambda: None)
    core = _FakeCore()
    ctrl._webview = SimpleNamespace(CoreWebView2=core, Visible=False)
    ctrl._form = _FakeForm()
    return ctrl, core


def test_pending_url_flushed_on_init_completed():
    ctrl, core = _fake_ready_controller()
    ctrl._pending_url = "https://example.com"
    ctrl._on_init_completed(ctrl._webview, SimpleNamespace(IsSuccess=True))
    assert ctrl._ready is True
    assert core.navigated == ["https://example.com"]
    assert ctrl._pending_url == ""


def test_navigate_when_ready_goes_straight_to_core():
    ctrl, core = _fake_ready_controller()
    ctrl._ready = True
    result = ctrl.navigate("example.com")
    assert result["ok"] is True
    assert core.navigated == ["https://example.com"]
    assert ctrl._url == "https://example.com"


def test_navigate_queues_when_core_not_ready_then_flushes():
    ctrl, core = _fake_ready_controller()
    result = ctrl.navigate("https://example.com")
    assert result["ok"] is True
    assert core.navigated == []
    ctrl._on_init_completed(ctrl._webview, SimpleNamespace(IsSuccess=True))
    assert core.navigated == ["https://example.com"]


class _ZoomProbe:

    def __init__(self):
        self.zoom_sets = 0
        self._zoom = None

    @property
    def ZoomFactor(self):
        return self._zoom

    @ZoomFactor.setter
    def ZoomFactor(self, value):
        self.zoom_sets += 1
        self._zoom = value


def test_apply_zoom_clamps_to_webview_range():
    ctrl, _core = _fake_ready_controller()
    probe = _ZoomProbe()
    ctrl._webview = probe

    ctrl._apply_zoom(0.01)
    assert probe.ZoomFactor == 0.25
    ctrl._apply_zoom(99)
    assert probe.ZoomFactor == 8.0


def test_apply_zoom_skips_unchanged_value():
    ctrl, _core = _fake_ready_controller()
    probe = _ZoomProbe()
    ctrl._webview = probe

    ctrl._apply_zoom(0.5)
    assert probe.zoom_sets == 1
    ctrl._apply_zoom(0.5)
    ctrl._apply_zoom(0.7)
    ctrl._apply_zoom(0.7)
    assert probe.ZoomFactor == 0.7
    assert probe.zoom_sets == 2


def test_apply_zoom_tolerates_bad_input():
    ctrl, _core = _fake_ready_controller()
    probe = _ZoomProbe()
    ctrl._webview = probe

    for bad in (None, "abc", float("nan")):
        ctrl._apply_zoom(bad)
    assert probe.zoom_sets == 0
    assert probe.ZoomFactor is None


def test_automation_profile_dir_reflects_shared_profile():
    assert eb.profile_dir().name == "browser_profile"
    assert eb.automation_profile_dir().name == "browser_profile_cdp"


def test_prepare_automation_creates_hidden_control_with_cdp_args():
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: _FakeForm()
    ctrl._webview = None
    ctrl._cdp_probe = lambda port, timeout: (True, "ready")
    calls: dict = {}

    def _fake_create(form, additional_browser_args="", cdp_port=None, user_data_folder=None):
        calls["args"] = additional_browser_args
        calls["port"] = cdp_port
        calls["folder"] = eb.profile_dir() if user_data_folder is None else user_data_folder
        ctrl._webview = SimpleNamespace()
        ctrl._cdp_port = cdp_port

    ctrl._create_control = _fake_create
    result = ctrl.prepare_automation()
    assert result["ok"] is True
    assert result["recreated"] is False
    assert result["endpoint"] == f"http://127.0.0.1:{calls['port']}"
    assert calls["args"].startswith("--remote-debugging-port=")
    assert calls["args"] == f"--remote-debugging-port={calls['port']}"
    assert calls["folder"] == eb.profile_dir()


def test_prepare_automation_is_idempotent_when_port_present():
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: _FakeForm()
    ctrl._webview = SimpleNamespace()
    ctrl._cdp_port = 9321
    probed: list = []
    ctrl._cdp_probe = lambda port, timeout: (probed.append(port), (True, "ready"))[1]
    created: list = []
    ctrl._create_control = lambda *args, **kwargs: created.append(args)
    result = ctrl.prepare_automation()
    assert result == {"ok": True, "endpoint": "http://127.0.0.1:9321", "recreated": False}
    assert created == []
    assert probed == [9321]


def test_prepare_automation_rebuilds_when_existing_port_is_dead():
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: _FakeForm()
    disposed: list = []
    ctrl._webview = SimpleNamespace(Dispose=lambda: disposed.append(1))
    ctrl._cdp_port = 9321
    probes: list = []

    def _probe(port, timeout):
        probes.append(port)
        if port == 9321:
            return False, "URLError: connection refused"
        return True, "ready"

    ctrl._cdp_probe = _probe
    created: dict = {}

    def _fake_create(form, additional_browser_args="", cdp_port=None, user_data_folder=None):
        created["port"] = cdp_port
        created["args"] = additional_browser_args
        ctrl._webview = SimpleNamespace()
        ctrl._cdp_port = cdp_port

    ctrl._create_control = _fake_create
    result = ctrl.prepare_automation()
    assert result["ok"] is True
    assert result["recreated"] is True
    assert disposed == [1]
    assert created["port"] is not None and created["port"] != 9321
    assert created["args"] == f"--remote-debugging-port={created['port']}"
    assert result["endpoint"] == f"http://127.0.0.1:{created['port']}"
    assert probes == [9321, created["port"]]


def test_prepare_automation_requires_confirmation_for_open_non_cdp_page():
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: _FakeForm()
    ctrl._cdp_probe = lambda port, timeout: (True, "ready")
    disposed: list = []
    existing_view = SimpleNamespace(Dispose=lambda: disposed.append(1))
    ctrl._webview = existing_view
    ctrl._cdp_port = None
    ctrl._ready = True
    ctrl._url = "https://example.com/form"
    created: list = []
    ctrl._create_control = lambda *args, **kwargs: created.append(1)

    denied = ctrl.prepare_automation()
    assert denied["ok"] is False
    assert denied["error"] == "recreate_confirmation_required"
    assert "刷新" in denied["msg"] or "重新加载" in denied["msg"]
    assert disposed == []
    assert created == []
    assert ctrl._webview is existing_view
    assert ctrl._cdp_port is None

    declined = ctrl.prepare_automation(confirm_recreate=lambda url: False)
    assert declined["ok"] is False
    assert declined["error"] == "recreate_confirmation_required"
    assert disposed == []
    assert created == []
    assert ctrl._webview is existing_view


def test_prepare_automation_recreates_only_after_explicit_confirmation():
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: _FakeForm()
    ctrl._cdp_probe = lambda port, timeout: (True, "ready")
    disposed: list = []
    ctrl._webview = SimpleNamespace(Dispose=lambda: disposed.append(1))
    ctrl._cdp_port = None
    ctrl._ready = True
    ctrl._url = "https://example.com/editor"
    seen_urls: list[str] = []
    created: dict = {}

    def _fake_create(form, additional_browser_args="", cdp_port=None, user_data_folder=None):
        created["port"] = cdp_port
        ctrl._webview = SimpleNamespace()
        ctrl._cdp_port = cdp_port

    ctrl._create_control = _fake_create
    result = ctrl.prepare_automation(
        confirm_recreate=lambda url: (seen_urls.append(url), True)[1]
    )
    assert result["ok"] is True
    assert result["recreated"] is True
    assert seen_urls == ["https://example.com/editor"]
    assert disposed == [1]
    assert ctrl._ready is False
    assert ctrl._pending_url == "https://example.com/editor"
    assert created["port"] is not None
    assert result["endpoint"] == f"http://127.0.0.1:{created['port']}"


def test_prepare_automation_reports_real_error_when_cdp_never_ready():
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: _FakeForm()
    ctrl._webview = None
    ctrl._cdp_probe = lambda port, timeout: (False, "URLError: connection refused")
    disposed: list = []

    def _fake_create(form, additional_browser_args="", cdp_port=None, user_data_folder=None):
        ctrl._webview = SimpleNamespace(Dispose=lambda: disposed.append(1))
        ctrl._cdp_port = cdp_port

    ctrl._create_control = _fake_create
    ctrl._init_error = "(0x8007139F): 组或资源的状态不是执行请求操作的正确状态。"
    result = ctrl.prepare_automation()
    assert result["ok"] is False
    assert "CDP 端点不可用" in result["error"]
    assert "0x8007139F" in result["error"]
    assert disposed == [1, 1]
    assert ctrl._webview is None
    assert ctrl._cdp_port is None


def test_init_failure_is_recorded_for_error_reporting():
    ctrl, _core = _fake_ready_controller()
    ctrl._on_init_completed(
        ctrl._webview,
        SimpleNamespace(IsSuccess=False, InitializationException="boom (0x8007139F)"),
    )
    assert "0x8007139F" in ctrl._init_error
    assert ctrl._ready is False
    ctrl2, _core2 = _fake_ready_controller()
    ctrl2._init_error = "old"
    ctrl2._on_init_completed(ctrl2._webview, SimpleNamespace(IsSuccess=True))
    assert ctrl2._init_error == ""


def test_init_failure_recycles_control_so_next_open_can_recreate():
    ctrl, _core = _fake_ready_controller()
    ctrl._on_init_completed(
        ctrl._webview,
        SimpleNamespace(IsSuccess=False, InitializationException="boom (0x8007139F)"),
    )
    assert ctrl._webview is None
    assert ctrl._ready is False
    assert ctrl._visible is False
    assert ctrl._pending_url == ""


def test_set_bounds_restores_visibility_of_recreated_control():
    ctrl, _core = _fake_ready_controller()
    ctrl._webview.Visible = False
    ctrl._visible = False
    ctrl._apply_bounds = lambda *_args, **_kwargs: None

    result = ctrl.set_bounds({"x": 10, "y": 20, "width": 300, "height": 200})

    assert result["ok"] is True
    assert ctrl._webview.Visible is True
    assert ctrl._visible is True


def test_probe_cdp_endpoint_gives_up_within_timeout():
    started = time.monotonic()
    ok, detail = eb.probe_cdp_endpoint(1, timeout=0.3)
    assert ok is False
    assert detail
    assert time.monotonic() - started < 5.0


def test_prepare_automation_requires_form():
    ctrl, _core = _fake_ready_controller()
    ctrl._webview = None
    ctrl._get_form = lambda: None
    result = ctrl.prepare_automation()
    assert result == {"ok": False, "error": "form_unavailable"}


class _NavArgs:

    def __init__(self, uri):
        self.Uri = uri
        self.Cancel = False


@pytest.mark.parametrize(
    ("uri", "cancelled"),
    [
        ("javascript:alert(1)", True),
        ("data:text/html,<h1>x</h1>", True),
        ("about:config", True),
        ("file:relative.txt", True),
        ("https://example.com/page", False),
        ("http://example.com/page", False),
        ("about:blank", False),
        ("file:///C:/Windows/win.ini", False),
        ("file:///D:/proj/a.html", False),
    ],
)
def test_navigation_starting_enforces_scheme_whitelist(uri, cancelled):
    ctrl = _controller()
    args = _NavArgs(uri)

    ctrl._on_navigation_starting(None, args)

    assert args.Cancel is cancelled


def test_navigation_starting_without_uri_does_not_miskill():
    ctrl = _controller()

    args = SimpleNamespace(Cancel=False)
    ctrl._on_navigation_starting(None, args)
    assert args.Cancel is False

    ctrl._on_navigation_starting(None, None)


def test_is_navigation_allowed_matches_normalize_url():
    assert eb.is_navigation_allowed("https://example.com") is True
    assert eb.is_navigation_allowed("about:blank") is True
    assert eb.is_navigation_allowed(None) is True
    assert eb.is_navigation_allowed("") is True
    assert eb.is_navigation_allowed("javascript:alert(1)") is False
    assert eb.is_navigation_allowed("file:///C:/x.txt") is True
    assert eb.is_navigation_allowed("file:relative.txt") is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("file:///C:/x.txt", "file:///C:/x.txt"),
        ("file://C:/x.txt", "file:///C:/x.txt"),
        ("C:\\proj\\a.html", "file:///C:/proj/a.html"),
        ("C:/proj/a.html", "file:///C:/proj/a.html"),
        ("\\\\srv\\share\\a.html", "file://srv/share/a.html"),
        ("  file:///C:/x.txt  ", "file:///C:/x.txt"),
    ],
)
def test_normalize_file_url_forms(raw, expected):
    assert eb.normalize_file_url(raw) == expected


def test_normalize_file_url_rejects_unsafe_inputs():
    assert eb.normalize_file_url(None) == ""
    assert eb.normalize_file_url(123) == ""
    assert eb.normalize_file_url("") == ""
    assert eb.normalize_file_url("file:") == ""
    assert eb.normalize_file_url("file:C:/x.txt") == ""
    assert eb.normalize_file_url("relative/a.html") == ""
    assert eb.normalize_file_url("file:///C:/a/../b.html") == ""


def test_path_to_file_uri_rejects_relative_and_root_only():
    assert eb.path_to_file_uri("./a.html") == ""
    assert eb.path_to_file_uri("") == ""
    assert eb.path_to_file_uri("C:") == ""


def test_shutdown_disposes_control_and_clears_cdp_port():
    ctrl, core = _fake_ready_controller()
    disposed = []
    ctrl._webview = SimpleNamespace(
        CoreWebView2=core, Dispose=lambda: disposed.append(1)
    )
    ctrl._cdp_port = 9321

    assert ctrl.shutdown() == {"ok": True}

    assert disposed == [1]
    assert ctrl._cdp_port is None
    assert ctrl._webview is None


def test_shutdown_clears_cdp_port_without_control():
    ctrl = _controller()
    ctrl._cdp_port = 9321

    assert ctrl.shutdown() == {"ok": True}

    assert ctrl._cdp_port is None


def test_discard_control_and_shutdown_wait_browser_process_exit_outside_invoke():
    ctrl, core = _fake_ready_controller()
    core.BrowserProcessId = 4242
    order = []
    form = _FakeForm()

    def _invoke(_f, cb):
        order.append("invoke_enter")
        cb()
        order.append("invoke_exit")
        return True

    ctrl._invoke = _invoke
    ctrl._wait_process_exit = lambda pid: order.append(("wait", pid))
    ctrl._webview = SimpleNamespace(
        CoreWebView2=core,
        Dispose=lambda: order.append("dispose"),
    )

    ctrl._discard_control(form)
    assert order == ["invoke_enter", "dispose", "invoke_exit", ("wait", 4242)]

    order.clear()
    core.BrowserProcessId = 5353
    ctrl._form = form
    ctrl._webview = SimpleNamespace(
        CoreWebView2=core,
        Dispose=lambda: order.append("dispose"),
    )
    assert ctrl.shutdown() == {"ok": True}
    assert order == ["invoke_enter", "dispose", "invoke_exit", ("wait", 5353)]


def test_prepare_automation_falls_back_to_automation_profile_on_0x8007139f_after_recreate():
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: _FakeForm()
    disposed = []
    ctrl._webview = SimpleNamespace(Dispose=lambda: disposed.append(1))
    ctrl._cdp_port = None
    ctrl._url = "https://example.com"
    seen_folders = []

    def _fake_create(form, additional_browser_args="", cdp_port=None, user_data_folder=None):
        folder = eb.profile_dir() if user_data_folder is None else user_data_folder
        seen_folders.append(folder)
        ctrl._webview = SimpleNamespace(Dispose=lambda: disposed.append(1))
        ctrl._cdp_port = cdp_port
        ctrl._init_error = "(0x8007139F): 组或资源的状态不是执行请求操作的正确状态。" if len(seen_folders) == 1 else ""

    def _probe(port, timeout):
        return (len(seen_folders) >= 2, "ready" if len(seen_folders) >= 2 else "timed out")

    ctrl._create_control = _fake_create
    ctrl._cdp_probe = _probe

    result = ctrl.prepare_automation(confirm_recreate=lambda _u: True)
    assert result["ok"] is True
    assert result["recreated"] is True
    assert seen_folders == [eb.profile_dir(), eb.automation_profile_dir()]
    assert len(disposed) == 2


def test_browser_mixin_releases_cached_controller_on_shutdown(monkeypatch):
    import gui_browser_mixin

    class _Bridge(gui_browser_mixin.BrowserBridgeMixin):
        def __init__(self):
            self._window = SimpleNamespace(uid="master")

        def emit_event(self, *_args):
            pass

    monkeypatch.setattr(gui_browser_mixin.sys, "platform", "win32")
    bridge = _Bridge()
    ctrl = bridge._browser_controller()
    assert ctrl is not None
    assert bridge._embedded_browser is ctrl

    ctrl.shutdown()

    assert getattr(bridge, "_embedded_browser", None) is None
    rect = {"x": 0, "y": 0, "width": 100, "height": 100}
    assert bridge.browser_pane_set_bounds(rect) == {"ok": False, "error": "not_open"}
    assert bridge.browser_pane_get_state() == {"ok": True, "state": None}
    assert bridge.browser_pane_hide() == {"ok": True}


def test_set_session_cookies_waits_for_core_ready_then_replays():
    ctrl, core = _fake_ready_controller()
    assert ctrl._ready is False

    ctrl.set_session_cookies([{"name": "sid", "value": "secret-1"}])

    assert ctrl._session_cookies[0]["name"] == "sid"
    assert ctrl._session_cookie_domain == "tokenrhythm.studio"
    assert core.CookieManager.added == []

    ctrl._on_init_completed(ctrl._webview, SimpleNamespace(IsSuccess=True))

    assert ctrl._ready is True
    assert [c.Name for c in core.CookieManager.added] == ["sid"]
    cookie = core.CookieManager.added[0]
    assert (cookie.Domain, cookie.Path) == ("tokenrhythm.studio", "/")


def test_set_session_cookies_without_control_is_safe():
    ctrl = _controller()
    ctrl.set_session_cookies([{"name": "sid", "value": "v"}])

    assert ctrl._webview is None
    assert [item["name"] for item in ctrl._session_cookies] == ["sid"]


def test_set_session_cookies_writes_every_valid_cookie():
    ctrl, core = _fake_ready_controller()
    ctrl._ready = True

    ctrl.set_session_cookies(
        [
            {"name": "sid", "value": "v1"},
            {"name": "", "value": "v2"},
            {"name": "no_value"},
            {"value": "v3"},
            {
                "name": "keep",
                "value": "v4",
                "domain": "app.example.com",
                "path": "/app",
                "http_only": True,
                "secure": True,
            },
        ]
    )

    manager = core.CookieManager
    assert [(c.Name, c.Value) for c in manager.created] == [
        ("sid", "v1"),
        ("keep", "v4"),
    ]
    assert manager.added == manager.created
    first, second = manager.created
    assert (first.Domain, first.Path) == ("tokenrhythm.studio", "/")
    assert (second.Domain, second.Path) == ("app.example.com", "/app")
    assert (first.IsHttpOnly, first.IsSecure) == (False, False)
    assert (second.IsHttpOnly, second.IsSecure) == (True, True)


def test_tr_csrf_cookie_is_visible_to_page_js():
    ctrl, core = _fake_ready_controller()
    ctrl._ready = True

    ctrl.set_session_cookies(
        [
            {"name": "tr_session", "value": "s", "http_only": True},
            {"name": "tr_csrf", "value": "c", "http_only": True},
        ]
    )

    session, csrf = core.CookieManager.created
    assert session.IsHttpOnly is True
    assert csrf.IsHttpOnly is False


def test_set_session_cookies_tolerates_bad_items():
    ctrl, _core = _fake_ready_controller()
    ctrl._ready = True

    ctrl.set_session_cookies([None, "sid=v", {"name": None, "value": None}])

    assert ctrl._session_cookies == []


def test_navigate_replays_session_cookies_before_navigating():
    ctrl, core = _fake_ready_controller()
    ctrl._ready = True
    ctrl.set_session_cookies([{"name": "sid", "value": "v1"}])
    assert len(core.CookieManager.added) == 1

    core.CookieManager.created.clear()
    core.CookieManager.added.clear()
    result = ctrl.navigate("example.com")

    assert result["ok"] is True
    assert core.navigated == ["https://example.com"]
    assert [c.Name for c in core.CookieManager.added] == ["sid"]


def test_session_cookie_value_never_logged(caplog):
    caplog.set_level(logging.DEBUG, logger=eb.logger.name)
    secret = "SESSION-SECRET-DO-NOT-LOG"
    ctrl, core = _fake_ready_controller()
    ctrl._ready = True

    def _boom(name, value, domain, path):
        raise RuntimeError("CreateCookie failed")

    core.CookieManager.CreateCookie = _boom
    ctrl.set_session_cookies([{"name": "sid", "value": secret}])

    assert "写入会话 cookie 失败" in caplog.text
    assert secret not in caplog.text  # 但 value 绝不入日志


class _FakeRect:

    def __init__(self, x, y, w, h):
        self.X, self.Y = int(x), int(y)
        self.Width, self.Height = int(w), int(h)

    def as_tuple(self):
        return (self.X, self.Y, self.Width, self.Height)


def _rect_factory(x, y, w, h):
    return _FakeRect(x, y, w, h)


class _NudgeForm:

    def __init__(self):
        self.Handle = _FakeHandle()
        self.queue = []
        self.begin_invoke_calls = 0

    def BeginInvoke(self, delegate):
        self.begin_invoke_calls += 1
        self.queue.append(delegate)

    def run_next(self):
        assert self.queue, "没有待驱动的 BeginInvoke 投递"
        self.queue.pop(0)()


class _BoundsProbe:

    def __init__(self):
        self.writes = []
        self._bounds = None
        self.Visible = False
        self.CoreWebView2 = None

    @property
    def Bounds(self):
        return self._bounds

    @Bounds.setter
    def Bounds(self, rect):
        self._bounds = rect
        self.writes.append(rect.as_tuple())


class _FakeMessageArgs:

    def __init__(self, text=None, json_value=None):
        self._text = text
        self._json = json_value

    def TryGetWebMessageAsString(self):
        if self._text is None:
            raise RuntimeError("消息不是字符串形态")
        return self._text

    @property
    def WebMessageAsJson(self):
        if self._json is None:
            raise RuntimeError("JSON 形态不可用")
        return self._json


def _nudge_controller(monkeypatch=None, bounds=(10, 20, 300, 200)):
    ctrl = eb.EmbeddedBrowserController(on_state=lambda _state: None, get_form=lambda: None)
    form = _NudgeForm()
    probe = _BoundsProbe()
    ctrl._form = form
    ctrl._webview = probe
    ctrl._ready = True
    ctrl._visible = True
    ctrl._loading = False
    ctrl._bounds = bounds
    ctrl._last_nudge = 0.0
    if monkeypatch is not None:
        monkeypatch.setattr(ctrl, "_make_rectangle", _rect_factory, raising=False)
    return ctrl, form, probe


def test_scroll_nudge_script_carries_ms_threshold():
    expected = str(max(1, int(eb._SCROLL_NUDGE_MIN_INTERVAL * 1000)))
    assert "__YCODE_NUDGE_MS__" not in eb._SCROLL_NUDGE_SCRIPT
    assert "now - last < " + expected + ")" in eb._SCROLL_NUDGE_SCRIPT


def test_non_nudge_web_message_does_not_trigger_nudge(monkeypatch):
    ctrl, form, _probe = _nudge_controller()
    calls = []
    monkeypatch.setattr(ctrl, "_nudge_after_scroll", lambda: calls.append(1))

    ctrl._on_web_message_received(None, _FakeMessageArgs(text="hello"))
    ctrl._on_web_message_received(None, _FakeMessageArgs(json_value='{"type":"hello"}'))
    ctrl._on_web_message_received(None, _FakeMessageArgs(text=""))

    assert calls == []
    assert form.begin_invoke_calls == 0


def test_scroll_nudge_web_message_triggers_nudge(monkeypatch):
    ctrl, _form, _probe = _nudge_controller()
    calls = []
    monkeypatch.setattr(ctrl, "_nudge_after_scroll", lambda: calls.append(1))

    ctrl._on_web_message_received(None, _FakeMessageArgs(text="scroll-nudge"))

    assert calls == [1]


def test_scroll_nudge_json_fallback_triggers_nudge(monkeypatch):
    ctrl, _form, _probe = _nudge_controller()
    calls = []
    monkeypatch.setattr(ctrl, "_nudge_after_scroll", lambda: calls.append(1))

    ctrl._on_web_message_received(None, _FakeMessageArgs(json_value='"scroll-nudge"'))

    assert calls == [1]


def test_scroll_nudge_unreadable_args_ignored(monkeypatch):
    ctrl, _form, _probe = _nudge_controller()
    calls = []
    monkeypatch.setattr(ctrl, "_nudge_after_scroll", lambda: calls.append(1))

    ctrl._on_web_message_received(None, _FakeMessageArgs())

    assert calls == []


def test_nudge_throttled_within_window():
    ctrl, form, probe = _nudge_controller()
    ctrl._last_nudge = time.monotonic()

    ctrl._nudge_after_scroll()

    assert form.begin_invoke_calls == 0
    assert form.queue == []
    assert probe.writes == []

    ctrl._last_nudge = time.monotonic() - eb._SCROLL_NUDGE_MIN_INTERVAL - 0.01
    ctrl._nudge_after_scroll()

    assert form.begin_invoke_calls == 1
    assert len(form.queue) == 1
    assert ctrl._last_nudge > 0.0

    ctrl._nudge_after_scroll()
    assert form.begin_invoke_calls == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("_ready", False),
        ("_webview", None),
        ("_visible", False),
        ("_loading", True),
        ("_bounds", None),
    ],
)
def test_nudge_guard_blocks_dispatch(field, value):
    ctrl, form, probe = _nudge_controller()
    setattr(ctrl, field, value)

    ctrl._nudge_after_scroll()

    assert form.begin_invoke_calls == 0
    assert form.queue == []
    assert probe.writes == []
    assert ctrl._last_nudge == 0.0


def test_nudge_bumps_height_by_one_then_restores(monkeypatch):
    ctrl, form, probe = _nudge_controller(monkeypatch, bounds=(10, 20, 300, 200))

    ctrl._nudge_after_scroll()

    assert len(form.queue) == 1
    assert probe.writes == []
    assert form.queue[0] is not None

    form.run_next()
    assert probe.writes == [(10, 20, 300, 201)]
    assert ctrl._bounds == (10, 20, 300, 200)
    assert len(form.queue) == 1

    form.run_next()
    assert probe.writes == [(10, 20, 300, 201), (10, 20, 300, 200)]
    assert form.queue == []


def test_nudge_restore_follows_latest_bounds(monkeypatch):
    ctrl, form, probe = _nudge_controller(monkeypatch, bounds=(10, 20, 300, 200))

    ctrl._nudge_after_scroll()
    form.run_next()

    ctrl._bounds = (50, 60, 400, 500)
    form.run_next()

    assert probe.writes == [(10, 20, 300, 201), (50, 60, 400, 500)]


def test_nudge_swallows_dispatch_failure():
    ctrl, form, probe = _nudge_controller()

    def _boom(delegate):
        raise RuntimeError("BeginInvoke failed")

    form.BeginInvoke = _boom

    ctrl._nudge_after_scroll()

    assert probe.writes == []
    assert form.queue == []


def test_nudge_skips_zero_size_bounds_inside_callback(monkeypatch):
    ctrl, form, probe = _nudge_controller(monkeypatch, bounds=(10, 20, 0, 0))

    ctrl._nudge_after_scroll()

    assert form.begin_invoke_calls == 1
    form.run_next()
    assert probe.writes == []
    assert form.queue == []


def test_surface_generation_rejects_stale_bounds_after_hide_and_requires_ready_ack():
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: ctrl._form
    ctrl._ready = False
    ctrl._apply_bounds = lambda *_args, **_kwargs: None
    ctrl._webview.Bounds = None

    opened = ctrl.open({"x": 0, "y": 0, "width": 400, "height": 500}, surface_generation=2)
    assert opened["ok"] is True
    assert opened["ack"] is True
    assert opened["surface_generation"] == 2
    assert opened["phase"] == "opening"
    assert opened["state"]["phase"] == "opening"

    ctrl._on_init_completed(ctrl._webview, SimpleNamespace(IsSuccess=True))
    assert ctrl._snapshot()["phase"] == "ready"

    hidden = ctrl.hide(surface_generation=3)
    assert hidden["ok"] is True
    assert hidden["ack"] is True
    assert hidden["surface_generation"] == 3
    assert ctrl._webview.Visible is False
    assert ctrl._snapshot()["phase"] == "hidden"

    stale = ctrl.set_bounds({"x": 0, "y": 0, "width": 500, "height": 600}, surface_generation=2)
    assert stale["ok"] is False
    assert stale["stale"] is True
    assert stale["error"] == "stale_surface_generation"
    assert ctrl._webview.Visible is False
    assert ctrl._snapshot()["surface_generation"] == 3

    bounds_after_hide = ctrl.set_bounds({"x": 0, "y": 0, "width": 500, "height": 600}, surface_generation=3)
    assert bounds_after_hide["ok"] is True
    assert ctrl._webview.Visible is False
