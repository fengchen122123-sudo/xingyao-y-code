"""core.embedded_browser 的纯函数与控制器的 headless 测试。

不 import clr / WinForms：纯函数全覆盖，控制器在 get_form 返回 None 时
所有方法必须优雅失败而不抛异常。
"""

import logging
from types import SimpleNamespace
import time

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
        ("mailto:a@b.com", ""),
        ("about:config", ""),
        # 本地文件放行：file:// URL 与盘符 / UNC 绝对路径都规范成 file:// URI
        ("file:///C:/x.txt", "file:///C:/x.txt"),
        ("file:///D:/proj/a.html", "file:///D:/proj/a.html"),
        ("C:\\proj\\a.html", "file:///C:/proj/a.html"),
        ("D:/proj/index.html", "file:///D:/proj/index.html"),
        ("\\\\srv\\share\\a.html", "file://srv/share/a.html"),
        # 空格 / 中文按百分号编码（本地路径带空格与中文是常态）
        ("file:///D:/My Docs/a b.html", "file:///D:/My%20Docs/a%20b.html"),
        ("file:///D:/文档/a.html", "file:///D:/%E6%96%87%E6%A1%A3/a.html"),
        # file: 的非标准写法 / 相对路径 / 含 ".." 的路径一律拒绝
        ("file:relative.txt", ""),
        ("file://relative.txt", ""),
        ("file:///D:/a/../b.html", ""),
        ("../a.html", ""),
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


class _FakeCookieManager:
    """CookieManager 桩：记录 CreateCookie / AddOrUpdateCookie 的调用。"""

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


def test_automation_profile_dir_reflects_shared_profile():
    """两套控件已合并：面板控件从创建起就带 CDP 调试端口，自动化接管的就是用户
    看到的那个浏览器（共享登录态），两者共用 profile_dir()，不再有第二个
    WebView2 环境，也就不存在同目录二次带参建环境（0x8007139F）的约束。"""
    assert eb.profile_dir().name == "browser_profile"
    # automation_profile_dir 仅为兼容旧引用的历史符号，已不用于创建任何控件
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
        # 未传目录即等价于 _create_control 的缺省 profile_dir()（两套控件共用）
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
    assert created == []  # 幂等：端口可用时不重复创建/重建控件
    assert probed == [9321]  # 但必须真实探测，不能凭"控件带端口"就返回


def test_prepare_automation_rebuilds_when_existing_port_is_dead():
    """控件已带端口但 probe 失败（面板控件创建路径从不 probe，端口可能已死）：
    必须丢弃该控件、带新端口重建，重建后 probe 通过才把 endpoint 交给上层。"""
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: _FakeForm()
    disposed: list = []
    ctrl._webview = SimpleNamespace(Dispose=lambda: disposed.append(1))
    ctrl._cdp_port = 9321
    probes: list = []

    def _probe(port, timeout):
        probes.append(port)
        if port == 9321:  # 旧端口已死
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
    assert disposed == [1]  # 死端点控件被丢弃，不留在主窗体上
    assert created["port"] is not None and created["port"] != 9321
    assert created["args"] == f"--remote-debugging-port={created['port']}"
    assert result["endpoint"] == f"http://127.0.0.1:{created['port']}"
    assert probes == [9321, created["port"]]  # 旧端口一次 + 重建端口一次


def test_prepare_automation_recreates_control_without_port():
    ctrl, _core = _fake_ready_controller()
    ctrl._get_form = lambda: _FakeForm()
    ctrl._cdp_probe = lambda port, timeout: (True, "ready")
    disposed: list = []
    ctrl._webview = SimpleNamespace(Dispose=lambda: disposed.append(1))
    ctrl._cdp_port = None
    ctrl._ready = True
    created: dict = {}

    def _fake_create(form, additional_browser_args="", cdp_port=None, user_data_folder=None):
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


def test_prepare_automation_reports_real_error_when_cdp_never_ready():
    """控件建了但 CDP 端口起不来（如 WebView2 初始化失败 0x8007139F）：
    必须丢弃控件并返回真实错误，绝不返回连不上的死端点——否则上层会拿着
    它去建 backend，最终只表现为"启动超时"，真因被掩盖。重建一次仍失败时
    同样丢弃并报错（两次都探不到就认输）。"""
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
    assert disposed == [1, 1]  # 首次创建 + 重建各丢弃一次死控件，都不留在主窗体上
    assert ctrl._webview is None
    assert ctrl._cdp_port is None  # 清端口：下次准备必须重新创建


def test_init_failure_is_recorded_for_error_reporting():
    ctrl, _core = _fake_ready_controller()
    ctrl._on_init_completed(
        ctrl._webview,
        SimpleNamespace(IsSuccess=False, InitializationException="boom (0x8007139F)"),
    )
    assert "0x8007139F" in ctrl._init_error
    assert ctrl._ready is False
    ctrl._on_init_completed(ctrl._webview, SimpleNamespace(IsSuccess=True))
    assert ctrl._init_error == ""


def test_probe_cdp_endpoint_gives_up_within_timeout():
    """探测必须有界：连不上的端口要在超时后返回失败，绝不无限等。"""
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
        ("data:text/html,<h1>x</h1>", True),
        ("about:config", True),
        ("file:relative.txt", True),
        # 白名单：http/https、about:blank，以及本地文件（本地 html 页内互链、
        # meta refresh 到另一个本地页都按系统浏览器行为放行）
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
    # 本地文件已放行（右栏内嵌浏览器渲染本地 html）；非绝对路径写法仍拒绝
    assert eb.is_navigation_allowed("file:///C:/x.txt") is True
    assert eb.is_navigation_allowed("file:relative.txt") is False


# ---------------- 本地文件 URI 规整 ----------------


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
    assert eb.normalize_file_url("file:C:/x.txt") == ""  # 非 file:// 标准写法
    assert eb.normalize_file_url("relative/a.html") == ""  # 相对路径无基准目录
    assert eb.normalize_file_url("file:///C:/a/../b.html") == ""  # 含 .. 视为异常输入


def test_path_to_file_uri_rejects_relative_and_root_only():
    assert eb.path_to_file_uri("./a.html") == ""
    assert eb.path_to_file_uri("") == ""
    assert eb.path_to_file_uri("C:") == ""  # 只有盘符、无文件名


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


# ---------------- 会话 cookie 注入（内嵌控件没有自己的登录态） ----------------

def test_set_session_cookies_waits_for_core_ready_then_replays():
    """内核异步初始化：就绪前只记录不外写，_on_init_completed 必须补写。

    只在 set_session_cookies 调用时写一次会永久丢掉登录态（控件此刻内核还没起）。
    """
    ctrl, core = _fake_ready_controller()
    assert ctrl._ready is False

    ctrl.set_session_cookies([{"name": "sid", "value": "secret-1"}])  # 不得抛

    assert ctrl._session_cookies[0]["name"] == "sid"
    assert ctrl._session_cookie_domain == "tokenrhythm.studio"
    assert core.CookieManager.added == []  # 内核未就绪，此时不写

    ctrl._on_init_completed(ctrl._webview, SimpleNamespace(IsSuccess=True))

    assert ctrl._ready is True
    assert [c.Name for c in core.CookieManager.added] == ["sid"]
    cookie = core.CookieManager.added[0]
    assert (cookie.Domain, cookie.Path) == ("tokenrhythm.studio", "/")


def test_set_session_cookies_without_control_is_safe():
    """控件尚未创建（_webview=None）时只记录，不抛异常，等初始化回放。"""
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
            {"name": "", "value": "v2"},  # 缺 name
            {"name": "no_value"},  # 缺 value
            {"value": "v3"},  # 缺 name
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
    assert manager.added == manager.created  # 每个 CreateCookie 都 AddOrUpdate 一次
    first, second = manager.created
    assert (first.Domain, first.Path) == ("tokenrhythm.studio", "/")
    assert (second.Domain, second.Path) == ("app.example.com", "/app")
    assert (first.IsHttpOnly, first.IsSecure) == (False, False)
    assert (second.IsHttpOnly, second.IsSecure) == (True, True)


def test_tr_csrf_cookie_is_visible_to_page_js():
    """tr_csrf 必须对页面 JS 可见。

    官方前端的请求包装从 document.cookie 读 tr_csrf 并回填 X-CSRF-Token；
    若按 httpOnly 注入，所有非 GET 请求都会被服务端判 CSRF_INVALID，
    充值页的「查看报价」就是这样失败的。会话凭据 tr_session 仍保持 httpOnly。
    """
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
    """每次导航前重放：页面把 cookie 清掉后下一次导航也能补回登录态。"""
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
    """cookie 的 value 是登录凭据：写入失败走 debug 日志，但 value 不得出现。"""
    caplog.set_level(logging.DEBUG, logger=eb.logger.name)
    secret = "SESSION-SECRET-DO-NOT-LOG"
    ctrl, core = _fake_ready_controller()
    ctrl._ready = True

    def _boom(name, value, domain, path):
        raise RuntimeError("CreateCookie failed")

    core.CookieManager.CreateCookie = _boom
    ctrl.set_session_cookies([{"name": "sid", "value": secret}])

    assert "写入会话 cookie 失败" in caplog.text  # 失败路径确实记了日志
    assert secret not in caplog.text  # 但 value 绝不入日志
