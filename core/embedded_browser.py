"""右侧面板内嵌浏览器：把原生 WebView2 WinForms 控件叠加到 pywebview 主窗体上。

为什么要这样实现：
- pywebview 自身的 webview 以 Dock=Fill + BringToFront 铺满整个主窗体，
  DOM 侧无法承载第三方浏览器内核。这里在其之上再叠加一个独立 WebView2
  控件，用前端上报的矩形（CSS 像素）做绝对定位，并每次 BringToFront
  压住主 webview，从而在右侧面板内形成"内嵌浏览器"（等价于 ZCode Electron
  的 <webview> 方案）。
- 顶层只 import 轻量模块，clr / WinForms / WebView2 全部惰性 import，
  保证 headless 环境（pytest、非 Windows）也能 import 本模块而不炸。
- 所有 WinForms / WebView2 操作都必须回到 GUI 线程执行：桥方法从 js_api
  线程进入，用 form.Invoke 同步 marshal 到 GUI 线程；事件回调本身就在
  GUI 线程，推送走 on_state 回调（线程安全入队）。
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

# 状态推送节流窗口：WebView2 导航过程会密集触发多个事件，150ms 足够顺滑
# 又不至于让每帧都生成并回传一次快照。
_EMIT_INTERVAL = 0.15

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_HOST_CHARS_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
# 命中这些显式 scheme 一律拒绝加载（前端 <a>/搜索框可能混入），
# 避免内嵌控件被 javascript:/data: 利用做越权或注入。
# file: 不在此列：本地 html 要在右栏内嵌浏览器里按网页渲染，改由
# normalize_file_url 单独规整（只放行盘符 / UNC 形式的绝对路径，见下）。
_REJECTED_SCHEMES_RE = re.compile(
    r"^(javascript|data|vbscript|blob|mailto|about|tel|chrome|edge|view-source):"
)
# 本地文件绝对路径形态（相对路径没有可靠基准目录，一律不放行）
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

# WebView2 ZoomFactor 允许范围：自由尺寸「适应窗口」用缩放把请求的逻辑视口
# （如 1280×800）渲染进面板矩形；超出范围的请求直接夹到边界。
_ZOOM_MIN = 0.25
_ZOOM_MAX = 8.0


def is_supported() -> bool:
    """内嵌浏览器仅 Windows（WebView2/WinForms）可用。"""
    return sys.platform == "win32"


def profile_dir() -> Path:
    """内嵌 WebView2 的 UserDataFolder（单一事实来源；computer.settings 的
    embedded 分支必须与此恒等）。支持 YCODE_DATA_DIR 覆盖。"""
    return _data_dir() / "browser_profile"


def pick_free_loopback_port() -> int:
    """随机空闲回环端口：CDP 调试端口只绑 127.0.0.1，端口随机化降低
    本机其它进程探测/连接的面。存在理论竞态（端口被抢），由 CDP 连接
    重试与明确报错兜底。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _data_dir() -> Path:
    """Y-CODE 数据目录（与主程序保持一致，支持 YCODE_DATA_DIR 覆盖）。"""
    return Path(os.environ.get("YCODE_DATA_DIR") or (Path.home() / ".ycode"))


def css_rect_to_physical(rect: Dict[str, Any], scale: float) -> Tuple[int, int, int, int]:
    """CSS 像素矩形 → 物理像素矩形。

    frameless 主窗体客户区原点与窗口原点一致（无标题栏/边框偏移），因此
    直接把 CSS 坐标乘 DPI 缩放即可。负值截 0（面板可能短暂上报负偏移）。
    """
    try:
        factor = float(scale) if scale else 1.0
    except (TypeError, ValueError):
        factor = 1.0

    def _px(value: Any) -> int:
        try:
            return max(0, int(round(float(value) * factor)))
        except (TypeError, ValueError):
            return 0

    return (
        _px(rect.get("x", 0)),
        _px(rect.get("y", 0)),
        _px(rect.get("width", 0)),
        _px(rect.get("height", 0)),
    )


def _looks_like_host(u: str) -> bool:
    """判断无 scheme 的输入是否像域名 / localhost / IP（可带端口）。"""
    authority = re.split(r"[/?#]", u, maxsplit=1)[0]
    if not authority or "@" in authority:
        return False
    hostname = authority.split(":", 1)[0]
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    if _IPV4_RE.match(hostname):
        return True
    if "." in hostname and _HOST_CHARS_RE.match(hostname):
        tld = hostname.rsplit(".", 1)[1]
        return len(tld) >= 2 and tld.isalpha()
    return False


def path_to_file_uri(path: str) -> str:
    r"""本地绝对路径 → file:// URI（正斜杠 + 百分号编码）；非绝对路径返回 ''。

    只接受盘符路径（C:\x / C:/x）与 UNC 路径（\\server\share\x）：相对路径没有
    可靠基准目录，放行等于把任意文本当本地文件打开，故一律拒绝。含 ".." 的
    路径同样不放行（调用方已归一化，出现即视为异常输入）。
    """
    p = str(path).strip().strip('"')
    if not p:
        return ""
    p = p.replace("\\", "/")
    if _WINDOWS_DRIVE_RE.match(p):
        drive = p[:2]
        segs = [s for s in p[2:].split("/") if s not in ("", ".")]
        if not segs or ".." in segs:
            return ""
        return "file:///" + drive + "/" + "/".join(quote(s, safe="") for s in segs)
    if p.startswith("//"):
        parts = [s for s in p[2:].split("/") if s not in ("", ".")]
        if len(parts) < 2 or ".." in parts:
            return ""
        return "file://" + quote(parts[0], safe="") + "/" + "/".join(
            quote(s, safe="") for s in parts[1:]
        )
    return ""


def normalize_file_url(raw: Any) -> str:
    """file:// URL 或本地绝对路径 → 规范 file:// URI；非法输入返回 ''。"""
    if not isinstance(raw, str):
        return ""
    u = raw.strip().strip('"')
    if not u:
        return ""
    if u[:5].lower() == "file:":
        body = u[5:]
        if not body.startswith("//"):
            return ""  # file:C:/x 这类非标准写法一律拒绝
        # file:///C:/x → /C:/x；file://C:/x → C:/x
        decoded = unquote(body[2:])
        if decoded.startswith("/"):
            decoded = decoded[1:]
        return path_to_file_uri(decoded)
    return path_to_file_uri(u)


def normalize_url(raw: Any) -> str:
    """把前端上报的 URL 规整成可导航地址；非法输入返回 ''（拒绝加载）。"""
    if not isinstance(raw, str):
        return ""
    u = raw.strip()
    if not u:
        return ""
    low = u.lower()
    if low == "about:blank":
        return u
    # 本地文件（file:// URL / 盘符 / UNC 绝对路径）：规范成 file:// URI 后放行，
    # 供右栏内嵌浏览器按网页渲染本地 html。本分支必须先于下面的「含空格即拒绝」
    # ——本地路径带空格是常态（如 C:\My Docs\a.html）。
    if low.startswith("file:") or _WINDOWS_DRIVE_RE.match(u) or u.startswith("\\\\"):
        return normalize_file_url(u)
    # 含空格/制表符 → 视为搜索词或残缺输入，拒绝（避免把任意文本当 URL）
    if any(ch.isspace() for ch in u):
        return ""
    if low.startswith("http://") or low.startswith("https://"):
        return u
    if u.startswith("//"):
        return "https:" + u
    if _REJECTED_SCHEMES_RE.match(low):
        return ""
    if _looks_like_host(u):
        return "https://" + u
    return ""


def is_navigation_allowed(uri: Any) -> bool:
    """页面自身发起导航的 scheme 兜底判定（与 normalize_url 同口径）。

    放行 http/https（normalize_url 的规整结果）、about:blank 与本地文件 URI
    （file:/// 盘符路径 / UNC，供本地 html 页内互链）；javascript:/data:/
    about:config 等一律拒绝。uri 取不到（None / 空 / 旧版内核无该属性）时
    返回 True——不误杀，交由 WebView2 自身策略决定。
    """
    if uri is None:
        return True
    text = str(uri).strip()
    if not text:
        return True
    return normalize_url(text) != ""


def _make_delegate(callback: Callable[[], None]):
    """把 Python 可调用包装成 WinForms 委托（Form.Invoke 需要 Delegate）。

    非 Windows / 未加载 clr 时退回原可调用，便于测试桩直接执行。
    """
    try:
        import System.Windows.Forms as WinForms  # noqa: WPS433 (惰性)

        return WinForms.MethodInvoker(callback)
    except Exception:
        return callback


class EmbeddedBrowserController:
    """内嵌 WebView2 控件的生命周期与定位控制。

    设计约定：所有公开方法都返回 dict 且内部吞掉异常（{'ok': False, 'error': ...}），
    绝不让桥层因原生异常崩溃。on_state 是推送快照的回调（由桥层入队 emit_event）。
    """

    def __init__(
        self,
        on_state: Callable[[Dict[str, Any]], None],
        get_form: Callable[[], Optional[object]],
    ):
        self._on_state = on_state
        self._get_form = get_form
        self._webview: Optional[object] = None
        self._form: Optional[object] = None
        self._ready = False
        self._visible = False
        self._loading = False
        self._url = ""
        self._title = ""
        self._bounds: Optional[Tuple[int, int, int, int]] = None
        self._zoom = 1.0
        # CDP 调试端口：仅自动化准备路径（prepare_automation）创建的控件携带；
        # None = 当前控件未开调试端口（普通面板浏览），是重建判定依据。
        self._cdp_port: Optional[int] = None
        self._lock = threading.RLock()
        self._last_emit = 0.0
        self._emit_timer: Optional[threading.Timer] = None
        self._pending_emit = False
        # EnsureCoreWebView2Async 是异步的：内核就绪前收到的导航目标缓存在这里，
        # _on_init_completed 补导航。没有它，「打开面板后首次导航」会因
        # open→navigate 背靠背派发撞上初始化竞态而被静默丢弃（评审 B1）。
        self._pending_url = ""

    # ------------------------------------------------------------------ 内部

    def _invoke(self, form, callback: Callable[[], None]) -> bool:
        """把回调 marshal 到 GUI 线程执行；句柄未就绪时优雅返回 False。"""
        handle = getattr(form, "Handle", None)
        if handle is not None:
            try:
                if hasattr(handle, "ToInt32") and handle.ToInt32() == 0:
                    return False
            except Exception:
                pass
        try:
            form.Invoke(_make_delegate(callback))
            return True
        except Exception:
            logger.debug("GUI 线程 Invoke 失败", exc_info=True)
            return False

    @staticmethod
    def _compute_scale(form) -> float:
        """DPI 缩放：优先 GetDpiForWindow，回退 DeviceDpi，最终 1.0。"""
        try:
            hwnd = form.Handle.ToInt32()
            if hwnd:
                dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
                if dpi:
                    return dpi / 96.0
        except Exception:
            pass
        try:
            dpi = getattr(form, "DeviceDpi", 0)
            if dpi:
                return dpi / 96.0
        except Exception:
            pass
        return 1.0

    @staticmethod
    def _make_rectangle(x, y, w, h):
        from System.Drawing import Rectangle  # noqa: WPS433 (惰性)

        return Rectangle(int(x), int(y), int(w), int(h))

    def _load_webview2(self):
        """惰性加载 WebView2 程序集（照抄 pywebview edgechromium 头部）。"""
        import clr  # noqa: WPS433 (惰性)

        clr.AddReference("System.Windows.Forms")
        from webview.util import interop_dll_path

        clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.Core.dll"))
        clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.WinForms.dll"))
        # 原生 dll 依赖解析路径（同 edgechromium.py）
        for platform in ("win-arm64", "win-x64", "win-x86"):
            os.environ["Path"] = os.environ.get("Path", "") + ";" + interop_dll_path(platform)
        from Microsoft.Web.WebView2.WinForms import (  # noqa: WPS433
            CoreWebView2CreationProperties,
            WebView2,
        )

        return WebView2, CoreWebView2CreationProperties

    def _create_control(self, form, additional_browser_args: str = "", cdp_port: Optional[int] = None) -> None:
        """在 GUI 线程内创建并挂载 WebView2 控件（幂等）。

        additional_browser_args 仅自动化准备路径使用（CDP 调试端口），
        必须在 EnsureCoreWebView2Async 之前写入 CreationProperties。
        """
        if self._webview is not None:
            return
        WebView2, CreationProperties = self._load_webview2()
        wv = WebView2()
        props = CreationProperties()
        profile = profile_dir()
        try:
            profile.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.debug("创建浏览器 profile 目录失败", exc_info=True)
        props.UserDataFolder = str(profile)
        if additional_browser_args:
            props.AdditionalBrowserArguments = additional_browser_args
        wv.CreationProperties = props
        wv.Visible = False
        wv.CoreWebView2InitializationCompleted += self._on_init_completed
        form.Controls.Add(wv)
        wv.EnsureCoreWebView2Async(None)
        self._webview = wv
        self._form = form
        self._cdp_port = cdp_port
        self._zoom = 1.0  # 新控件从默认缩放起步，旧缓存不得带入

    # ------------------------------------------------------------ 事件回调

    def _on_init_completed(self, sender, args) -> None:
        """WebView2 内核就绪：配置 Settings 并挂接导航/新窗口事件。"""
        try:
            if not args.IsSuccess:
                logger.warning(
                    "内嵌 WebView2 初始化失败: %s",
                    getattr(args, "InitializationException", "unknown"),
                )
                return
            core = self._webview.CoreWebView2
            try:
                core.Settings.IsStatusBarEnabled = False
            except Exception:
                logger.debug("设置 IsStatusBarEnabled 失败", exc_info=True)
            core.SourceChanged += self._on_source_changed
            core.DocumentTitleChanged += self._on_title_changed
            core.NavigationStarting += self._on_navigation_starting
            core.NavigationCompleted += self._on_navigation_completed
            core.NewWindowRequested += self._on_new_window_requested
            self._ready = True
            # 补导航：内核就绪前 navigate()/open(url) 缓存的目标（评审 B1/S1）。
            # 与 navigate 的竞态闭环：双方都在 _lock 内读写 _pending_url/_ready，
            # 要么这里看到 pending 补导航，要么 navigate 看到 ready 直接导航。
            with self._lock:
                pending = self._pending_url
                self._pending_url = ""
            if pending:
                try:
                    self._url = pending
                    core.Navigate(pending)
                except Exception:
                    logger.debug("内嵌浏览器补导航失败: %s", pending, exc_info=True)
            self._schedule_emit()
        except Exception:
            logger.warning("内嵌 WebView2 事件挂接失败", exc_info=True)

    def _on_source_changed(self, sender, args) -> None:
        try:
            self._url = str(sender.Source or "")
        except Exception:
            pass
        self._schedule_emit()

    def _on_title_changed(self, sender, args) -> None:
        try:
            self._title = str(sender.DocumentTitle or "")
        except Exception:
            pass
        self._schedule_emit()

    def _on_navigation_starting(self, sender, args) -> None:
        self._loading = True
        self._schedule_emit()
        # 页面自身发起的顶层导航（点击链接 / location.href / meta refresh）不经过
        # 后端 normalize_url，必须在这里用同一口径兜底，否则 javascript:/data:
        # 等 scheme 可绕过白名单（本地文件 URI 与主入口同口径放行）。uri 取不到时不误杀。
        try:
            uri = getattr(args, "Uri", None)
        except Exception:
            uri = None
        if is_navigation_allowed(uri):
            return
        try:
            args.Cancel = True
            logger.info("已拦截非白名单内嵌浏览器导航: %s", uri)
        except Exception:
            logger.debug("取消内嵌浏览器导航失败", exc_info=True)

    def _on_navigation_completed(self, sender, args) -> None:
        self._loading = False
        try:
            self._url = str(sender.Source or self._url)
        except Exception:
            pass
        self._schedule_emit()

    def _on_new_window_requested(self, sender, args) -> None:
        """target=_blank 等新窗口请求：同控件内导航（不做弹窗）。"""
        try:
            args.Handled = True
            target = normalize_url(str(args.Uri))
            if target and self._webview is not None:
                self._webview.CoreWebView2.Navigate(target)
        except Exception:
            logger.debug("处理新窗口请求失败", exc_info=True)

    # ------------------------------------------------------------ 状态推送

    def _schedule_emit(self) -> None:
        """150ms 节流 + 尾随补发：密集事件合并为末态快照。"""
        try:
            with self._lock:
                self._pending_emit = True
                if self._emit_timer is not None:
                    return
                elapsed = time.monotonic() - self._last_emit
                if elapsed >= _EMIT_INTERVAL:
                    self._pending_emit = False
                    self._last_emit = time.monotonic()
                    emit_now = True
                else:
                    emit_now = False
                    timer = threading.Timer(_EMIT_INTERVAL - elapsed, self._emit_trailing)
                    timer.daemon = True
                    self._emit_timer = timer
                    timer.start()
            if emit_now:
                self._emit_now()
        except Exception:
            logger.debug("调度状态推送失败", exc_info=True)

    def _emit_trailing(self) -> None:
        try:
            with self._lock:
                self._emit_timer = None
                if not self._pending_emit:
                    return
                self._pending_emit = False
                self._last_emit = time.monotonic()
            self._emit_now()
        except Exception:
            logger.debug("尾随状态推送失败", exc_info=True)

    def _snapshot(self) -> Dict[str, Any]:
        """读取当前浏览器状态快照（尽可能在 GUI 线程读取 CoreWebView2）。"""
        url = self._url
        title = self._title
        can_back = False
        can_fwd = False
        webview = self._webview
        form = self._form
        if webview is not None and form is not None:
            box: Dict[str, Any] = {}

            def _read():
                try:
                    core = webview.CoreWebView2
                    if core is not None:
                        box["url"] = str(core.Source or "")
                        box["title"] = str(core.DocumentTitle or "")
                        box["can_go_back"] = bool(core.CanGoBack)
                        box["can_go_forward"] = bool(core.CanGoForward)
                except Exception:
                    pass

            self._invoke(form, _read)
            url = box.get("url", url)
            title = box.get("title", title)
            can_back = box.get("can_go_back", can_back)
            can_fwd = box.get("can_go_forward", can_fwd)
        return {
            "url": url,
            "title": title,
            "can_go_back": can_back,
            "can_go_forward": can_fwd,
            "loading": self._loading,
            "ready": self._ready,
            "visible": self._visible,
        }

    def _emit_now(self) -> None:
        try:
            self._on_state(self._snapshot())
        except Exception:
            logger.debug("推送内嵌浏览器状态失败", exc_info=True)

    # ------------------------------------------------------------ 公开接口

    def open(self, rect: Dict[str, Any], url: str = "", zoom: float = 1.0) -> Dict[str, Any]:
        """惰性创建控件，定位并显示；url 合法则立即导航。zoom 为渲染缩放。"""
        try:
            if not is_supported():
                return {"ok": False, "error": "unsupported_platform"}
            form = self._get_form()
            if form is None:
                return {"ok": False, "error": "form_unavailable"}
            box: Dict[str, Any] = {}

            def _do_open():
                try:
                    self._create_control(form)
                    self._apply_bounds(form, rect, zoom)
                    self._visible = True
                    if self._webview is not None:
                        self._webview.Visible = True
                    target = normalize_url(url)
                    if target and self._webview is not None:
                        self._url = target
                        if self._ready:
                            self._webview.CoreWebView2.Navigate(target)
                        else:
                            # 刚创建的控件内核必然未就绪：缓存目标等初始化完成
                            # 补导航（评审 S1，与 _on_init_completed 同线程无竞态）
                            self._pending_url = target
                    box["ok"] = True
                except Exception as exc:
                    box["ok"] = False
                    box["error"] = str(exc)

            if not self._invoke(form, _do_open):
                return {"ok": False, "error": "invoke_failed"}
            return box or {"ok": False, "error": "invoke_failed"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_bounds(self, rect: Dict[str, Any], zoom: float = 1.0) -> Dict[str, Any]:
        """更新控件矩形的物理像素 Bounds 与渲染缩放，并 BringToFront 压住主 webview。"""
        try:
            form = self._form or self._get_form()
            if form is None or self._webview is None:
                return {"ok": False, "error": "not_open"}
            box: Dict[str, Any] = {}

            def _apply():
                try:
                    self._apply_bounds(form, rect, zoom)
                    box["ok"] = True
                except Exception as exc:
                    box["ok"] = False
                    box["error"] = str(exc)

            if not self._invoke(form, _apply):
                return {"ok": False, "error": "invoke_failed"}
            return box or {"ok": False, "error": "invoke_failed"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _apply_bounds(self, form, rect: Dict[str, Any], zoom: float = 1.0) -> None:
        """GUI 线程内计算 DPI 并把 CSS 矩形换算为控件 Bounds。"""
        scale = self._compute_scale(form)
        x, y, w, h = css_rect_to_physical(rect, scale)
        self._webview.Bounds = self._make_rectangle(x, y, w, h)
        self._apply_zoom(zoom)
        self._webview.BringToFront()
        self._bounds = (x, y, w, h)

    def _apply_zoom(self, zoom: float) -> None:
        """设置渲染缩放（自由尺寸「适应窗口」）：网页按 bounds/zoom 的 CSS
        视口布局，整体缩放贴合控件矩形。幂等：值未变化不触碰控件。"""
        try:
            z = float(zoom)
        except (TypeError, ValueError):
            return
        if z != z:  # NaN
            return
        z = max(_ZOOM_MIN, min(_ZOOM_MAX, z))
        if self._zoom == z:
            return
        try:
            self._webview.ZoomFactor = z
            self._zoom = z
        except Exception:
            logger.debug("设置 WebView2 ZoomFactor 失败", exc_info=True)

    def navigate(self, url: str) -> Dict[str, Any]:
        target = normalize_url(url)
        if not target:
            return {"ok": False, "error": "invalid_url"}
        if not self._ready:
            # 内核未就绪（含控件尚未创建）：缓存目标，控件创建并完成初始化后
            # 由 _on_init_completed 补导航（评审 B1）。pywebview 对每个 api
            # 调用独立派发线程，navigate 先于 open 到达也不能丢。
            # 缓存后在锁内复查 ready，封住「init 已 flush 完才缓存」的竞态窗口。
            with self._lock:
                if self._ready:
                    self._pending_url = ""
                else:
                    self._pending_url = target
                became_ready = self._ready
            if not became_ready:
                self._url = target
                return {"ok": True, "state": self._snapshot()}
        return self._navigate_now(target)

    def _navigate_now(self, target: str) -> Dict[str, Any]:
        result = self._core_call(lambda core: core.Navigate(target))
        if result.get("ok"):
            with self._lock:
                self._url = target
                self._pending_url = ""
        return result

    def back(self) -> Dict[str, Any]:
        return self._core_call(lambda core: core.GoBack())

    def forward(self) -> Dict[str, Any]:
        return self._core_call(lambda core: core.GoForward())

    def reload(self) -> Dict[str, Any]:
        return self._core_call(lambda core: core.Reload())

    def stop(self) -> Dict[str, Any]:
        return self._core_call(lambda core: core.Stop())

    def _core_call(self, action: Callable[[Any], None]) -> Dict[str, Any]:
        """在 GUI 线程内对 CoreWebView2 执行一次操作。"""
        try:
            form = self._form or self._get_form()
            if form is None or self._webview is None:
                return {"ok": False, "error": "not_open"}
            box: Dict[str, Any] = {}

            def _run():
                try:
                    core = self._webview.CoreWebView2
                    if core is None:
                        raise RuntimeError("not_ready")
                    action(core)
                    box["ok"] = True
                except Exception as exc:
                    box["ok"] = False
                    box["error"] = str(exc)

            if not self._invoke(form, _run):
                return {"ok": False, "error": "invoke_failed"}
            return box or {"ok": False, "error": "invoke_failed"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def hide(self) -> Dict[str, Any]:
        """隐藏但不销毁控件（保活，避免重复创建内核的昂贵开销）。"""
        try:
            form = self._form or self._get_form()
            if form is None or self._webview is None:
                return {"ok": False, "error": "not_open"}
            box: Dict[str, Any] = {}

            def _hide():
                try:
                    self._webview.Visible = False
                    self._visible = False
                    box["ok"] = True
                except Exception as exc:
                    box["ok"] = False
                    box["error"] = str(exc)

            if not self._invoke(form, _hide):
                return {"ok": False, "error": "invoke_failed"}
            return box or {"ok": False, "error": "invoke_failed"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_state(self) -> Dict[str, Any]:
        try:
            return {"ok": True, "state": self._snapshot()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def prepare_automation(self) -> Dict[str, Any]:
        """星瑶自动化准备：确保控件带 CDP 调试端口存在（幂等）。

        - 控件已带端口：直接返回现有 endpoint（面板浏览不受影响）；
        - 控件存在但未带端口：Dispose 后带端口重建（页面会重载）；
        - 控件不存在：隐藏创建（不强制打开面板，可见性/定位仍由前端
          面板的 open/set_bounds 正常路径负责——WebView2 隐藏时渲染可能
          被节流，自动化开始后面板会自动切到浏览器页签使其可见）。

        返回 {'ok', 'endpoint', 'recreated'}；失败 {'ok': False, 'error'}。
        """
        try:
            if not is_supported():
                return {"ok": False, "error": "unsupported_platform"}
            form = self._get_form()
            if form is None:
                return {"ok": False, "error": "form_unavailable"}
            box: Dict[str, Any] = {}

            def _do_prepare():
                try:
                    if self._webview is not None and self._cdp_port is not None:
                        box["ok"] = True
                        box["endpoint"] = f"http://127.0.0.1:{self._cdp_port}"
                        box["recreated"] = False
                        return
                    if self._webview is not None:
                        # 已有控件但未带调试端口：带端口重建（页面重载）。
                        old = self._webview
                        self._webview = None
                        self._ready = False
                        self._visible = False
                        with self._lock:
                            self._pending_url = ""
                        self._zoom = 1.0
                        try:
                            old.Dispose()
                        except Exception:
                            logger.debug("释放旧内嵌 WebView2 失败", exc_info=True)
                        box["recreated"] = True
                    port = pick_free_loopback_port()
                    self._create_control(
                        form,
                        additional_browser_args=f"--remote-debugging-port={port}",
                        cdp_port=port,
                    )
                    box["ok"] = True
                    box["endpoint"] = f"http://127.0.0.1:{port}"
                    if not box.get("recreated"):
                        box["recreated"] = False
                except Exception as exc:
                    box["ok"] = False
                    box["error"] = str(exc)

            if not self._invoke(form, _do_prepare):
                return {"ok": False, "error": "invoke_failed"}
            return box or {"ok": False, "error": "invoke_failed"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def shutdown(self) -> Dict[str, Any]:
        """best-effort 释放：失败也不抛（进程退出路径）。"""
        try:
            # 先取消节流尾随定时器：避免退出后 Timer 线程再补发一次状态事件（评审 S8）
            with self._lock:
                if self._emit_timer is not None:
                    self._emit_timer.cancel()
                    self._emit_timer = None
                self._pending_emit = False
                self._pending_url = ""
                # 一并清 _cdp_port：控件即将 Dispose，端口不再可用；不清的话
                # 下一次 prepare_automation 会误认"控件仍带调试端口"而返回
                # 已失效的 endpoint（面板浏览与自动化并存时的静默错误）。
                self._cdp_port = None
            webview = self._webview
            form = self._form
            if webview is None:
                return {"ok": True}
            self._webview = None
            self._ready = False
            self._visible = False

            def _dispose():
                try:
                    webview.Dispose()
                except Exception:
                    logger.debug("释放内嵌 WebView2 失败", exc_info=True)

            if form is not None and not self._invoke(form, _dispose):
                _dispose()
            elif form is None:
                _dispose()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}