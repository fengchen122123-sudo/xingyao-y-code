"""core.embedded_browser 的纯函数与控制器的 headless 测试。

不 import clr / WinForms：纯函数全覆盖，控制器在 get_form 返回 None 时
所有方法必须优雅失败而不抛异常。
"""

from types import SimpleNamespace

import pytest

from core import embedded_browser as eb


# ------------------------------------------------------------------ 纯函数

@pytest.mark.parametrize(
    ("rect", "scale", "expected"),
    [
        ({"x": 10, "y": 20, "width": 300, "height": 200}, 1.0, (10, 20, 300, 200)),
        ({"x": 4, "y": 8, "width": 40, "height": 80}, 1.25, (5, 10, 50, 100)),
        ({"x": 10, "y": 20, "width": 300, "height": 200}, 2.0, (20, 40, 600, 400)),
        # 负值截 0（面板可能短暂上报负偏移）
        ({"x": -5, "y": -3, "width": -40, "height": -80}, 1.0, (0, 0, 0, 0)),
        ({"x": -5, "y": 10, "width": 40, "height": 80}, 2.0, (0, 20, 80, 160)),
        # 缺键按 0 处理
        ({"x": 7}, 1.0, (7, 0, 0, 0)),
    ],
)
def test_css_rect_to_physical(rect, scale, expected):
    assert eb.css_rect_to_physical(rect, scale) == expected


def test_css_rect_to_physical_handles_bad_scale_and_values():
    # scale 非法 → 退回 1.0；数值非法 → 0
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
        # 显式危险 scheme 一律拒绝
        ("javascript:alert(1)", ""),
        ("data:text/html,<h1>x</h1>", ""),
        ("file:///C:/x.txt", ""),
        ("mailto:a@b.com", ""),
        ("about:config", ""),
        # 搜索词 / 含空格
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


# ------------------------------------------------------------------ 控制器

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
    # get_state 即使未打开也返回结构化结果，不抛
    state = ctrl.get_state()
    assert isinstance(state, dict)
    assert state["ok"] is True
    assert state["state"]["visible"] is False
    # shutdown 在未创建时是安全 no-op
    assert ctrl.shutdown() == {"ok": True}


def test_controller_navigate_rejects_invalid_url():
    ctrl = _controller()
    result = ctrl.navigate("javascript:alert(1)")
    assert result == {"ok": False, "error": "invalid_url"}


# ------------------------------------------- B1：内核未就绪的 pending 补导航

def test_navigate_before_open_queues_pending_url():
    """navigate 先于 open 到达（pywebview 各 api 调用独立派发线程，顺序无
    保证）时不能丢：缓存 pending 目标并返回 ok，等初始化完成补导航。"""
    ctrl = _controller()
    result = ctrl.navigate("https://example.com")
    assert result["ok"] is True
    assert ctrl._pending_url == "https://example.com"
    # 乐观 URL 进快照，前端地址栏与后端状态一致
    assert ctrl.get_state()["state"]["url"] == "https://example.com"


class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _FakeSettings:
    IsStatusBarEnabled = True


class _FakeCore:
    def __init__(self):
        self.navigated = []
        self.Settings = _FakeSettings()
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
    """Invoke 直接同步执行回调（headless 下 _make_delegate 退回原可调用）。"""

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
    """完整时序：open 创建控件（内核异步初始化中）→ navigate 缓存 →
    初始化完成回调补导航。"""
    ctrl, core = _fake_ready_controller()
    result = ctrl.navigate("https://example.com")  # _ready 仍为 False
    assert result["ok"] is True
    assert core.navigated == []
    ctrl._on_init_completed(ctrl._webview, SimpleNamespace(IsSuccess=True))
    assert core.navigated == ["https://example.com"]


class _ZoomProbe:
    """记录 ZoomFactor 赋值次数的桩：验证同值幂等（不重复触碰控件）。"""

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
    ctrl._apply_zoom(0.5)  # 幂等：心跳重复上报同值不得反复触碰控件
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


# ---------------- 星瑶自动化准备（prepare_automation） ----------------


def test_prepare_automation_creates_hidden_control_with_cdp_args():
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: _FakeForm()
    ctrl._webview = None
    calls: dict = {}

    def _fake_create(form, additional_browser_args="", cdp_port=None):
        calls["args"] = additional_browser_args
        calls["port"] = cdp_port
        ctrl._webview = SimpleNamespace()
        ctrl._cdp_port = cdp_port

    ctrl._create_control = _fake_create
    result = ctrl.prepare_automation()
    assert result["ok"] is True
    assert result["recreated"] is False
    assert result["endpoint"] == f"http://127.0.0.1:{calls['port']}"
    assert calls["args"].startswith("--remote-debugging-port=")
    assert calls["args"] == f"--remote-debugging-port={calls['port']}"


def test_prepare_automation_is_idempotent_when_port_present():
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: _FakeForm()
    ctrl._webview = SimpleNamespace()
    ctrl._cdp_port = 9321
    created: list = []
    ctrl._create_control = lambda *args, **kwargs: created.append(args)
    result = ctrl.prepare_automation()
    assert result == {"ok": True, "endpoint": "http://127.0.0.1:9321", "recreated": False}
    assert created == []  # 幂等：不重复创建/重建控件


def test_prepare_automation_recreates_control_without_port():
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: _FakeForm()
    disposed: list = []
    ctrl._webview = SimpleNamespace(Dispose=lambda: disposed.append(1))
    ctrl._cdp_port = None
    ctrl._ready = True
    created: dict = {}

    def _fake_create(form, additional_browser_args="", cdp_port=None):
        created["port"] = cdp_port
        ctrl._webview = SimpleNamespace()
        ctrl._cdp_port = cdp_port

    ctrl._create_control = _fake_create
    result = ctrl.prepare_automation()
    assert result["ok"] is True
    assert result["recreated"] is True
    assert disposed == [1]
    assert ctrl._ready is False  # 重建后内核重新初始化
    assert created["port"] is not None
    assert result["endpoint"] == f"http://127.0.0.1:{created['port']}"


def test_prepare_automation_requires_form():
    ctrl, _core = _fake_ready_controller()
    ctrl._webview = None
    ctrl._get_form = lambda: None
    result = ctrl.prepare_automation()
    assert result == {"ok": False, "error": "form_unavailable"}


# ---------------- 页面自身发起的顶层导航 scheme 兜底 ----------------


class _NavArgs:
    """NavigationStartingEventArgs 桩：只有 Uri 与可写的 Cancel。"""

    def __init__(self, uri):
        self.Uri = uri
        self.Cancel = False


@pytest.mark.parametrize(
    ("uri", "cancelled"),
    [
        # 点击链接 / location.href / meta refresh 绕过后端 normalize_url 的入口
        ("javascript:alert(1)", True),
        ("file:///C:/Windows/win.ini", True),
        ("data:text/html,<h1>x</h1>", True),
        ("about:config", True),
        # 白名单：http/https 与既有的 about:blank
        ("https://example.com/page", False),
        ("http://example.com/page", False),
        ("about:blank", False),
    ],
)
def test_navigation_starting_enforces_scheme_whitelist(uri, cancelled):
    ctrl = _controller()
    args = _NavArgs(uri)

    ctrl._on_navigation_starting(None, args)

    assert args.Cancel is cancelled


def test_navigation_starting_without_uri_does_not_miskill():
    """Uri 取不到（旧版内核 / args 为 None）时不得误杀导航。"""
    ctrl = _controller()

    args = SimpleNamespace(Cancel=False)
    ctrl._on_navigation_starting(None, args)
    assert args.Cancel is False

    ctrl._on_navigation_starting(None, None)  # 不抛


def test_is_navigation_allowed_matches_normalize_url():
    assert eb.is_navigation_allowed("https://example.com") is True
    assert eb.is_navigation_allowed("about:blank") is True
    assert eb.is_navigation_allowed(None) is True  # 取不到 -> 不误杀
    assert eb.is_navigation_allowed("") is True
    assert eb.is_navigation_allowed("javascript:alert(1)") is False
    assert eb.is_navigation_allowed("file:///C:/x.txt") is False


# ---------------- shutdown 释放契约 ----------------


def test_shutdown_disposes_control_and_clears_cdp_port():
    """shutdown 必须一并清 _cdp_port：否则下一次 prepare_automation 会
    误认"控件仍带调试端口"而返回已失效的 endpoint。"""
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


def test_browser_mixin_releases_cached_controller_on_shutdown(monkeypatch):
    """gui_browser_mixin：控制器释放后必须清掉桥层缓存引用，后续脑跳调用与
    "从未打开"语义一致（set_bounds -> not_open），不再打到已 Dispose 的控件。

    注：gui_browser_mixin 共用 core.embedded_browser 的释放契约，测试放这里
    与控制器释放契约同处一文件。
    """
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

    ctrl.shutdown()  # ApiBridge.shutdown 的调用形态

    assert getattr(bridge, "_embedded_browser", None) is None
    rect = {"x": 0, "y": 0, "width": 100, "height": 100}
    assert bridge.browser_pane_set_bounds(rect) == {"ok": False, "error": "not_open"}
    assert bridge.browser_pane_get_state() == {"ok": True, "state": None}
    assert bridge.browser_pane_hide() == {"ok": True}
