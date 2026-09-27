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
