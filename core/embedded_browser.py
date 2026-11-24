from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

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
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

_ZOOM_MIN = 0.25
_ZOOM_MAX = 8.0

_DEFAULT_BROWSER_MEMORY_ARGS = (
    "--disable-features=CalculateNativeWinOcclusion --disk-cache-size=52428800"
)


def _merge_additional_browser_args(*groups: str) -> str:
    order = []
    by_key = {}
    for group in groups:
        for token in str(group or "").split():
            key = token.split("=", 1)[0]
            if key not in by_key:
                order.append(key)
            by_key[key] = token
    return " ".join(by_key[key] for key in order)

# CDP 端点就绪等待：控件创建是异步的（EnsureCoreWebView2Async），端口要等
# 浏览器进程起来才可连。有界轮询，超时即视为本次自动化准备失败——绝不把
# 一个连不上的端口当成功返回。
_CDP_READY_TIMEOUT = 20.0
_CDP_PROBE_INTERVAL = 0.25
_CDP_PROBE_HTTP_TIMEOUT = 1.0
_DISPOSE_PROCESS_EXIT_TIMEOUT = 3.0


def _read_browser_process_id(webview) -> Optional[int]:
    try:
        core = getattr(webview, "CoreWebView2", None)
        raw_pid = getattr(core, "BrowserProcessId", None) if core is not None else None
    except Exception:
        return None
    if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0:
        return int(raw_pid)
    return None


def _wait_browser_process_exit(
    pid: Optional[int], timeout: float = _DISPOSE_PROCESS_EXIT_TIMEOUT
) -> None:
    """Dispose 后等待旧 WebView2 宿主进程退出并释放 UserDataFolder 锁。"""
    if not pid or isinstance(pid, bool) or pid <= 0 or sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x00100000, 0, int(pid))
        if not handle:
            return
        try:
            kernel32.WaitForSingleObject(handle, max(0, int(float(timeout) * 1000)))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        logger.debug("等待 WebView2 进程退出失败", exc_info=True)

# 滚动后轻量重绘 nudge 的节流下限（秒）。这是**唯一数据源**：JS 侧的节流毫秒
# 阈值由它派生（见下方 _SCROLL_NUDGE_SCRIPT 的占位替换），不再在脚本里另写一个
# 数字，否则两处各改一处就会悄悄漂移。原注释声称"共用这个最小间隔"，但脚本里
# 实际写死了 150，属于注释与实现不符，这里一并改正。
_SCROLL_NUDGE_MIN_INTERVAL = 0.15

_SCROLL_NUDGE_SCRIPT = """
(function () {
  if (window.__ycodeScrollNudgeInstalled) return;
  window.__ycodeScrollNudgeInstalled = true;
  var last = 0;
  function onScroll() {
    var now = Date.now();
    if (now - last < __YCODE_NUDGE_MS__) return;
    last = now;
    try {
      // 只发字符串：宿主主路径是 TryGetWebMessageAsString()，发对象会让它抛
      // 异常、绕道 WebMessageAsJson 兜底，白付一次异常开销（缺陷 3）。
      window.chrome.webview.postMessage('scroll-nudge');
    } catch (e) { /* 通道不可用时静默放弃，不影响页面滚动 */ }
  }
  document.addEventListener('scroll', onScroll, { passive: true, capture: true });
  window.addEventListener('scroll', onScroll, { passive: true });
})();
"""

# 占位替换成毫秒整数。用 .replace 而非 str.format：JS 里大量 {} 会被当成
# 占位符直接炸掉。max(1, ...) 防止常量被改小到 0 导致节流失效（每帧都发）。
_SCROLL_NUDGE_SCRIPT = _SCROLL_NUDGE_SCRIPT.replace(
    "__YCODE_NUDGE_MS__", str(max(1, int(_SCROLL_NUDGE_MIN_INTERVAL * 1000)))
)


def is_supported() -> bool:
    """内嵌浏览器仅 Windows（WebView2/WinForms）可用。"""
    return sys.platform == "win32"


def profile_dir() -> Path:
    return _data_dir() / "browser_profile"


def automation_profile_dir() -> Path:
    return _data_dir() / "browser_profile_cdp"


def probe_cdp_endpoint(
    port: int, timeout: float = _CDP_READY_TIMEOUT
) -> Tuple[bool, str]:
    deadline = time.monotonic() + max(0.0, float(timeout))
    last = "端口未就绪"
    while True:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{int(port)}/json/version",
                timeout=_CDP_PROBE_HTTP_TIMEOUT,
            ) as response:
                status = int(getattr(response, "status", 200))
                if status == 200:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
                    return True, str(payload.get("Browser") or "ready")
                last = f"HTTP {status}"
        except Exception as error:  # noqa: BLE001 —— 探测失败一律按未就绪处理
            last = f"{type(error).__name__}: {error}"
        if time.monotonic() >= deadline:
            return False, " ".join(str(last).split())[:200]
        time.sleep(_CDP_PROBE_INTERVAL)


def pick_free_loopback_port() -> int:
    """随机空闲回环端口：CDP 调试端口只绑 127.0.0.1，端口随机化降低
    本机其它进程探测/连接的面。存在理论竞态（端口被抢），由 CDP 连接
    重试与明确报错兜底。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _data_dir() -> Path:
    return Path(os.environ.get("YCODE_DATA_DIR") or (Path.home() / ".ycode"))


def css_rect_to_physical(rect: Dict[str, Any], scale: float) -> Tuple[int, int, int, int]:
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
            return ""
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
    if uri is None:
        return True
    text = str(uri).strip()
    if not text:
        return True
    return normalize_url(text) != ""


def _make_delegate(callback: Callable[[], None]):
    try:
        import System.Windows.Forms as WinForms  # noqa: WPS433 (惰性)

        return WinForms.MethodInvoker(callback)
    except Exception:
        return callback


class EmbeddedBrowserController:

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
        self._cdp_port: Optional[int] = None
        # 自动化准备的就绪探测（可注入假探针，测试用）与超时；_init_error 记录
        # WebView2 初始化失败原因，供 prepare_automation 报出真实错误。
        self._cdp_probe = probe_cdp_endpoint
        self._wait_process_exit = _wait_browser_process_exit
        self._cdp_ready_timeout = _CDP_READY_TIMEOUT
        self._init_error = ""
        self._lock = threading.RLock()
        self._surface_op_lock = threading.Lock()
        self._surface_generation = 0
        self._latest_surface_generation = 0
        self._explicit_hidden = False
        self._last_emit = 0.0
        self._emit_timer: Optional[threading.Timer] = None
        self._pending_emit = False
        # EnsureCoreWebView2Async 是异步的：内核就绪前收到的导航目标缓存在这里，
        # _on_init_completed 补导航。没有它，「打开面板后首次导航」会因
        # open→navigate 背靠背派发撞上初始化竞态而被静默丢弃（评审 B1）。
        self._pending_url = ""
        # 会话 cookie 注入：内嵌 WebView2 控件没有主 webview 的登录态，Python 侧
        # 会话 cookie 必须经 CoreWebView2.CookieManager 写进去。控件内核是异步
        # 初始化的（EnsureCoreWebView2Async），所以这里只做记录，实际写入在
        # 内核就绪与每次导航前重放（页面把 cookie 清掉也能补回来）。
        self._session_cookies: list[dict] = []
        self._session_cookie_domain = ""
        self._last_nudge = 0.0

    def _invoke(self, form, callback: Callable[[], None]) -> bool:
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

    def _begin_invoke(self, form, callback: Callable[[], None]) -> bool:
        try:
            form.BeginInvoke(_make_delegate(callback))
            return True
        except Exception:
            logger.debug("GUI 线程 BeginInvoke 失败", exc_info=True)
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
        import clr  # noqa: WPS433 (惰性)

        clr.AddReference("System.Windows.Forms")
        from webview.util import interop_dll_path

        clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.Core.dll"))
        clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.WinForms.dll"))
        for platform in ("win-arm64", "win-x64", "win-x86"):
            os.environ["Path"] = os.environ.get("Path", "") + ";" + interop_dll_path(platform)
        from Microsoft.Web.WebView2.WinForms import (  # noqa: WPS433
            CoreWebView2CreationProperties,
            WebView2,
        )

        return WebView2, CoreWebView2CreationProperties

    def _create_control(
        self,
        form,
        additional_browser_args: str = "",
        cdp_port: Optional[int] = None,
        user_data_folder: Optional[Path] = None,
    ) -> None:
        if self._webview is not None:
            return
        WebView2, CreationProperties = self._load_webview2()
        wv = WebView2()
        props = CreationProperties()
        profile = Path(user_data_folder) if user_data_folder is not None else profile_dir()
        try:
            profile.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.debug("创建浏览器 profile 目录失败", exc_info=True)
        props.UserDataFolder = str(profile)
        # 默认内存参数 + 调用方参数合并去重后写入（默认在前，调用方同键取值优先）。
        # 必须在 EnsureCoreWebView2Async 之前落到 CreationProperties 上，之后再写
        # 不会生效。
        merged_args = _merge_additional_browser_args(
            _DEFAULT_BROWSER_MEMORY_ARGS, additional_browser_args
        )
        if merged_args:
            props.AdditionalBrowserArguments = merged_args
        wv.CreationProperties = props
        wv.Visible = False
        wv.CoreWebView2InitializationCompleted += self._on_init_completed
        form.Controls.Add(wv)
        self._init_error = ""
        wv.EnsureCoreWebView2Async(None)
        self._webview = wv
        self._form = form
        self._cdp_port = cdp_port
        self._zoom = 1.0  # 新控件从默认缩放起步，旧缓存不得带入
        self._last_nudge = 0.0

    def _on_init_completed(self, sender, args) -> None:
        try:
            if not args.IsSuccess:
                self._init_error = " ".join(
                    str(getattr(args, "InitializationException", "unknown")).split()
                )[:300]
                logger.warning(
                    "内嵌 WebView2 初始化失败: %s",
                    getattr(args, "InitializationException", "unknown"),
                )
                if sender is self._webview:
                    self._recycle_failed_control()
                self._schedule_emit()
                return
            self._init_error = ""
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
            try:
                core.WebMessageReceived += self._on_web_message_received
            except Exception:
                logger.debug("订阅 WebMessageReceived 失败（滚动 nudge 通道收不到消息）", exc_info=True)
            try:
                core.AddScriptToExecuteOnDocumentCreatedAsync(_SCROLL_NUDGE_SCRIPT)
            except Exception:
                logger.debug("注入滚动重绘 nudge 脚本失败（新文档将无法上报滚动）", exc_info=True)
            self._ready = True
            # 补导航：内核就绪前 navigate()/open(url) 缓存的目标（评审 B1/S1）。
            # 与 navigate 的竞态闭环：双方都在 _lock 内读写 _pending_url/_ready，
            # 要么这里看到 pending 补导航，要么 navigate 看到 ready 直接导航。
            with self._lock:
                pending = self._pending_url
                self._pending_url = ""
            # 会话 cookie 重放：必须早于补导航，否则首个页面仍按未登录渲染。
            self._flush_session_cookies()
            if pending:
                try:
                    self._url = pending
                    core.Navigate(pending)
                except Exception:
                    logger.debug("内嵌浏览器补导航失败: %s", pending, exc_info=True)
            with self._lock:
                should_present = self._visible and not self._explicit_hidden
            if should_present:
                self._present_surface()
            self._schedule_emit()
        except Exception:
            logger.warning("内嵌 WebView2 事件挂接失败", exc_info=True)

    def _recycle_failed_control(self) -> None:
        try:
            webview = self._webview
            if webview is None:
                return
            self._webview = None
            self._ready = False
            self._visible = False
            self._zoom = 1.0
            with self._lock:
                self._pending_url = ""
                self._explicit_hidden = False
            form = self._form

            def _dispose():
                try:
                    parent = getattr(webview, "Parent", None)
                    if parent is not None:
                        parent.Controls.Remove(webview)
                except Exception:
                    logger.debug("移除初始化失败的 WebView2 控件失败", exc_info=True)
                try:
                    webview.Dispose()
                except Exception:
                    logger.debug("释放初始化失败的 WebView2 控件失败", exc_info=True)

            if form is None or not self._begin_invoke(form, _dispose):
                _dispose()
        except Exception:
            logger.debug("回收初始化失败的 WebView2 控件失败", exc_info=True)

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
        try:
            if self._webview is not None:
                self._webview.CoreWebView2.ExecuteScriptAsync(_SCROLL_NUDGE_SCRIPT)
        except Exception:
            logger.debug("补注入滚动重绘 nudge 脚本失败", exc_info=True)
        self._schedule_emit()

    def _on_new_window_requested(self, sender, args) -> None:
        try:
            args.Handled = True
            target = normalize_url(str(args.Uri))
            if target and self._webview is not None:
                self._webview.CoreWebView2.Navigate(target)
        except Exception:
            logger.debug("处理新窗口请求失败", exc_info=True)

    def _on_web_message_received(self, sender, args) -> None:
        try:
            raw = args.TryGetWebMessageAsString()
        except Exception:
            # 防御性兜底（非主路径）：仅应对消息不是字符串形态的情况（例如页面
            # 别处或旧版脚本发了对象）。旧实现把对象当唯一形态，主路径每次都抛
            # 异常再兜底，白付开销；现在反过来。兜底失败即放弃本次通知。
            try:
                raw = args.WebMessageAsJson
            except Exception:
                return
        if "scroll-nudge" not in str(raw or ""):
            return
        self._nudge_after_scroll()

    def _schedule_emit(self) -> None:
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

    def _surface_phase_locked(self) -> str:
        if self._init_error:
            return "error"
        if self._webview is None:
            return "idle"
        if self._explicit_hidden or not self._visible:
            return "hidden"
        if not self._ready:
            return "opening"
        return "ready"

    def _stale_surface_result(self) -> Dict[str, Any]:
        with self._lock:
            current_gen = self._latest_surface_generation
        return {
            "ok": False,
            "stale": True,
            "error": "stale_surface_generation",
            "surface_generation": current_gen,
            "state": self._snapshot(),
        }

    def _register_requested_generation(
        self, surface_generation: Optional[int]
    ) -> Optional[int]:
        with self._lock:
            if surface_generation is None:
                self._latest_surface_generation += 1
                return self._latest_surface_generation
            gen = max(0, int(surface_generation))
            if gen < self._latest_surface_generation:
                return None
            self._latest_surface_generation = gen
            return gen

    def _snapshot(self) -> Dict[str, Any]:
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
        with self._lock:
            phase = self._surface_phase_locked()
            generation = max(self._surface_generation, self._latest_surface_generation)
        return {
            "url": url,
            "title": title,
            "can_go_back": can_back,
            "can_go_forward": can_fwd,
            "loading": self._loading,
            "ready": self._ready,
            "visible": self._visible,
            "phase": phase,
            "surface_generation": generation,
        }

    def _emit_now(self) -> None:
        try:
            self._on_state(self._snapshot())
        except Exception:
            logger.debug("推送内嵌浏览器状态失败", exc_info=True)

    def open(
        self,
        rect: Dict[str, Any],
        url: str = "",
        zoom: float = 1.0,
        surface_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        """惰性创建控件，定位并显示；url 合法则立即导航。zoom 为渲染缩放。"""
        try:
            if not is_supported():
                return {"ok": False, "error": "unsupported_platform"}
            form = self._get_form()
            if form is None:
                return {"ok": False, "error": "form_unavailable"}
            op_gen = self._register_requested_generation(surface_generation)
            if op_gen is None:
                return self._stale_surface_result()
            with self._surface_op_lock:
                with self._lock:
                    if op_gen < self._latest_surface_generation:
                        return self._stale_surface_result()
                    self._surface_generation = op_gen
                box: Dict[str, Any] = {}

                def _do_open():
                    try:
                        with self._lock:
                            if op_gen < self._latest_surface_generation:
                                box.update(
                                    {
                                        "ok": False,
                                        "stale": True,
                                        "error": "stale_surface_generation",
                                        "surface_generation": self._latest_surface_generation,
                                    }
                                )
                                return
                        self._create_control(form)
                        # 先露出控件再摆位置。自动化重建会把控件留在隐藏态，
                        # 而 Bounds 写到未就绪的 WebView2 上可能一直不返回；
                        # 若把 Visible 放在那之后，右栏就会停在「正在准备」。
                        self._present_surface()
                        self._apply_bounds(form, rect, zoom)
                        with self._lock:
                            if op_gen < self._latest_surface_generation:
                                box.update(
                                    {
                                        "ok": False,
                                        "stale": True,
                                        "error": "stale_surface_generation",
                                        "surface_generation": self._latest_surface_generation,
                                    }
                                )
                                return
                            self._explicit_hidden = False
                            self._visible = True
                        self._present_surface()
                        target = normalize_url(url)
                        if target and self._webview is not None:
                            self._url = target
                            if self._ready:
                                self._webview.CoreWebView2.Navigate(target)
                            else:
                                # 刚创建的控件内核必然未就绪：缓存目标等初始化完成
                                # 补导航（评审 S1，与 _on_init_completed 同线程无竞态）
                                with self._lock:
                                    self._pending_url = target
                        box["ok"] = True
                    except Exception as exc:
                        box["ok"] = False
                        box["error"] = str(exc)

                if not self._invoke(form, _do_open):
                    return {"ok": False, "error": "invoke_failed"}
                if not box:
                    return {"ok": False, "error": "invoke_failed"}
                if box.get("stale"):
                    box["state"] = self._snapshot()
                    return box
                if not box.get("ok"):
                    return box
                state = self._snapshot()
                return {
                    "ok": True,
                    "ack": True,
                    "surface_generation": op_gen,
                    "phase": state["phase"],
                    "state": state,
                }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_bounds(
        self,
        rect: Dict[str, Any],
        zoom: float = 1.0,
        surface_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        """更新控件矩形的物理像素 Bounds 与渲染缩放，并 BringToFront 压住主 webview。"""
        try:
            form = self._form or self._get_form()
            if form is None or self._webview is None:
                return {"ok": False, "error": "not_open"}
            op_gen = self._register_requested_generation(surface_generation)
            if op_gen is None:
                return self._stale_surface_result()
            with self._surface_op_lock:
                with self._lock:
                    if op_gen < self._latest_surface_generation:
                        return self._stale_surface_result()
                    self._surface_generation = op_gen
                box: Dict[str, Any] = {}

                def _apply():
                    try:
                        with self._lock:
                            if op_gen < self._latest_surface_generation:
                                box.update(
                                    {
                                        "ok": False,
                                        "stale": True,
                                        "error": "stale_surface_generation",
                                        "surface_generation": self._latest_surface_generation,
                                    }
                                )
                                return
                        self._apply_bounds(form, rect, zoom)
                        with self._lock:
                            should_show = (
                                not self._explicit_hidden
                                and not self._visible
                                and self._webview is not None
                                and op_gen >= self._latest_surface_generation
                            )
                            if should_show:
                                self._visible = True
                        if should_show:
                            self._present_surface()
                        box["ok"] = True
                    except Exception as exc:
                        box["ok"] = False
                        box["error"] = str(exc)

                if not self._invoke(form, _apply):
                    return {"ok": False, "error": "invoke_failed"}
                if not box:
                    return {"ok": False, "error": "invoke_failed"}
                if box.get("stale"):
                    box["state"] = self._snapshot()
                    return box
                if not box.get("ok"):
                    return box
                state = self._snapshot()
                return {
                    "ok": True,
                    "ack": True,
                    "surface_generation": op_gen,
                    "phase": state["phase"],
                    "state": state,
                }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _present_surface(self) -> None:
        """把已有控件摆到主 WebView 之上。必须先于摆矩形：Bounds/Zoom 卡住时
        也不能继续把已经加载好的页面藏着。"""
        webview = self._webview
        if webview is None:
            return
        try:
            webview.Visible = True
        except Exception:
            logger.debug("显示内嵌浏览器控件失败", exc_info=True)
        bring = getattr(webview, "BringToFront", None)
        if not callable(bring):
            return
        try:
            bring()
        except Exception:
            logger.debug("内嵌浏览器置顶失败", exc_info=True)

    def _apply_bounds(self, form, rect: Dict[str, Any], zoom: float = 1.0) -> None:
        scale = self._compute_scale(form)
        x, y, w, h = css_rect_to_physical(rect, scale)
        self._webview.Bounds = self._make_rectangle(x, y, w, h)
        self._apply_zoom(zoom)
        bring = getattr(self._webview, "BringToFront", None)
        if callable(bring):
            try:
                bring()
            except Exception:
                logger.debug("内嵌浏览器置顶失败", exc_info=True)
        self._bounds = (x, y, w, h)

    def _apply_zoom(self, zoom: float) -> None:
        """设置渲染缩放（自由尺寸「适应窗口」）：网页按 bounds/zoom 的 CSS
        视口布局，整体缩放贴合控件矩形。幂等：值未变化不触碰控件。"""
        try:
            z = float(zoom)
        except (TypeError, ValueError):
            return
        if z != z:
            return
        z = max(_ZOOM_MIN, min(_ZOOM_MAX, z))
        if self._zoom == z:
            return
        try:
            self._webview.ZoomFactor = z
            self._zoom = z
        except Exception:
            logger.debug("设置 WebView2 ZoomFactor 失败", exc_info=True)

    def _nudge_after_scroll(self) -> None:
        try:
            if not self._ready or self._webview is None or not self._visible:
                return
            if self._loading:
                return
            bounds = self._bounds
            if not bounds:
                return
            now = time.monotonic()
            if now - self._last_nudge < _SCROLL_NUDGE_MIN_INTERVAL:
                return
            form = self._form or self._get_form()
            if form is None:
                return
            self._last_nudge = now

            def _nudge():
                try:
                    if self._webview is None or self._bounds is None:
                        return
                    x, y, w, h = self._bounds
                    if w <= 0 or h <= 0:
                        return
                    self._webview.Bounds = self._make_rectangle(x, y, w, h + 1)
                    logger.debug(
                        "滚动重绘 nudge 执行：页面滚动静默重绘兜底，h %d -> %d",
                        h, h + 1,
                    )
                except Exception:
                    logger.debug("滚动重绘 nudge 执行失败", exc_info=True)
                    return

                def _restore():
                    try:
                        if self._webview is None:
                            return
                        # 必须重新读 self._bounds 而非闭包里的旧 (x,y,w,h)：
                        # 这两帧之间 set_bounds 可能已换成新矩形，用旧值还原
                        # 会把用户刚设的新尺寸覆盖掉。
                        cur = self._bounds
                        if not cur:
                            return
                        rx, ry, rw, rh = cur
                        if rw <= 0 or rh <= 0:
                            return
                        # 语义选择：还原是"恢复动作"，不受 _loading / _visible
                        # 影响——否则跳过会把 h+1 永久留在控件上（隐藏后重新
                        # 显示时高度就多了 1px，且没有任何路径会再修正）。
                        # 隐藏态下写 Bounds 无副作用，导航中也只是回到既有矩形。
                        self._webview.Bounds = self._make_rectangle(rx, ry, rw, rh)
                    except Exception:
                        logger.debug("滚动重绘 nudge 还原 Bounds 失败", exc_info=True)
                    # 注意：还原回调里绝不调用 _nudge_after_scroll，否则会自激
                    # 成 nudge 死循环。

                # 一帧延迟：还原单独投递一次，保证 +1 与还原落在不同的消息循环
                # 轮次（BeginInvoke 就是"下一轮 GUI 消息"）。投递失败也不回滚，
                # 那说明控件/句柄已不可用，此后 _create_control 会重建。
                if not self._begin_invoke(form, _restore):
                    logger.debug("滚动重绘 nudge 还原未能投递到 GUI 线程")

            if not self._begin_invoke(form, _nudge):
                logger.debug("滚动重绘 nudge 未能投递到 GUI 线程")
        except Exception:
            logger.debug("滚动重绘 nudge 调度失败", exc_info=True)

    def set_session_cookies(
        self, cookies: list[dict], domain: str = "tokenrhythm.studio"
    ) -> None:
        """记住要注入的 cookie 并立即尝试写入；内核未就绪时等 _on_init_completed 重放。"""
        try:
            items: list[dict] = []
            for raw in cookies or []:
                if not isinstance(raw, dict):
                    continue
                name = raw.get("name")
                value = raw.get("value")
                if not name or not value:
                    continue
                items.append(
                    {
                        "name": str(name),
                        "value": str(value),
                        "domain": str(raw.get("domain") or domain or ""),
                        "path": str(raw.get("path") or "/"),
                        "http_only": bool(raw.get("http_only")),
                        "secure": bool(raw.get("secure")),
                    }
                )
            self._session_cookies = items
            self._session_cookie_domain = str(domain or "")
        except Exception:
            logger.debug("记录会话 cookie 失败", exc_info=True)
            return
        self._flush_session_cookies()

    def _flush_session_cookies(self) -> None:
        try:
            webview = self._webview
            if webview is None or not self._ready:
                return
            form = self._form
            if form is None:
                return
            cookies = list(self._session_cookies)
            if not cookies:
                return

            def _write():
                try:
                    core = self._webview.CoreWebView2
                    if core is None:
                        return
                    manager = core.CookieManager
                    for item in cookies:
                        cookie = manager.CreateCookie(
                            item["name"], item["value"], item["domain"], item["path"]
                        )
                        cookie.IsHttpOnly = bool(item.get("http_only")) and item.get("name") != "tr_csrf"
                        cookie.IsSecure = bool(item.get("secure"))
                        manager.AddOrUpdateCookie(cookie)
                except Exception:
                    # 日志级别压到 debug 且只给摘要：cookie 的 value 是登录凭据，
                    # 任何形态（含异常消息）都不得写进日志。
                    logger.debug("写入会话 cookie 失败", exc_info=True)

            if not self._invoke(form, _write):
                logger.debug("写入会话 cookie 失败：GUI 线程不可用")
        except Exception:
            logger.debug("重放会话 cookie 失败", exc_info=True)

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
        self._flush_session_cookies()
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

    def hide(self, surface_generation: Optional[int] = None) -> Dict[str, Any]:
        """隐藏但不销毁控件（保活，避免重复创建内核的昂贵开销）。"""
        try:
            form = self._form or self._get_form()
            if form is None or self._webview is None:
                return {"ok": False, "error": "not_open"}
            op_gen = self._register_requested_generation(surface_generation)
            if op_gen is None:
                return self._stale_surface_result()
            with self._surface_op_lock:
                with self._lock:
                    if op_gen < self._latest_surface_generation:
                        return self._stale_surface_result()
                    self._surface_generation = op_gen
                    self._explicit_hidden = True
                    self._visible = False
                box: Dict[str, Any] = {}

                def _hide():
                    try:
                        with self._lock:
                            if op_gen < self._latest_surface_generation:
                                box.update(
                                    {
                                        "ok": False,
                                        "stale": True,
                                        "error": "stale_surface_generation",
                                        "surface_generation": self._latest_surface_generation,
                                    }
                                )
                                return
                            self._explicit_hidden = True
                            self._visible = False
                        if self._webview is not None:
                            self._webview.Visible = False
                        box["ok"] = True
                    except Exception as exc:
                        box["ok"] = False
                        box["error"] = str(exc)

                if not self._invoke(form, _hide):
                    return {"ok": False, "error": "invoke_failed"}
                if not box:
                    return {"ok": False, "error": "invoke_failed"}
                if box.get("stale"):
                    box["state"] = self._snapshot()
                    return box
                if not box.get("ok"):
                    return box
                state = self._snapshot()
                return {
                    "ok": True,
                    "ack": True,
                    "surface_generation": op_gen,
                    "phase": state["phase"],
                    "state": state,
                }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_state(self) -> Dict[str, Any]:
        try:
            return {"ok": True, "state": self._snapshot()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _discard_control(self, form) -> None:
        """丢弃当前控件并清空端口状态（自动化准备失败路径）。

        必须一并清 _cdp_port：否则下一次 prepare_automation 会误认"控件仍带
        调试端口"而返回已失效的 endpoint（与 shutdown 同一理由）。
        同时在 WinForms Dispose 之后等待旧 WebView2 进程退出，避免同目录
        带不同参数重建时触发 HRESULT 0x8007139F。
        """
        pid_box: Dict[str, Optional[int]] = {"pid": None}

        def _drop():
            webview = self._webview
            self._webview = None
            self._ready = False
            self._visible = False
            self._cdp_port = None
            self._zoom = 1.0
            with self._lock:
                self._explicit_hidden = False
                self._pending_url = ""
            if webview is not None:
                pid_box["pid"] = _read_browser_process_id(webview)
                if form is not None:
                    try:
                        form.Controls.Remove(webview)
                    except Exception:
                        pass
                try:
                    webview.Dispose()
                except Exception:
                    logger.debug("释放内嵌 WebView2 失败", exc_info=True)

        if form is None:
            _drop()
        elif not self._invoke(form, _drop):
            _drop()
        self._wait_process_exit(pid_box.get("pid"))

    def prepare_automation(
        self, *, confirm_recreate: Optional[Callable[[str], bool]] = None
    ) -> Dict[str, Any]:
        try:
            if not is_supported():
                return {"ok": False, "error": "unsupported_platform"}
            form = self._get_form()
            if form is None:
                return {"ok": False, "error": "form_unavailable"}
            inspect_box: Dict[str, Any] = {}

            def _inspect_control() -> None:
                try:
                    has_wv = self._webview is not None
                    port = self._cdp_port
                    cur_url = self._url or ""
                    if has_wv:
                        try:
                            core = getattr(self._webview, "CoreWebView2", None)
                            if core is not None and getattr(core, "Source", None):
                                cur_url = str(core.Source or "")
                        except Exception:
                            pass
                    inspect_box["has_webview"] = has_wv
                    inspect_box["port"] = port
                    inspect_box["url"] = cur_url
                    inspect_box["ok"] = True
                except Exception as exc:
                    inspect_box["ok"] = False
                    inspect_box["error"] = str(exc)

            if not self._invoke(form, _inspect_control):
                return {"ok": False, "error": "invoke_failed"}
            if not inspect_box.get("ok"):
                return inspect_box or {"ok": False, "error": "invoke_failed"}

            has_webview = bool(inspect_box.get("has_webview"))
            existing_port = inspect_box.get("port")
            current_url = str(inspect_box.get("url") or "")
            recreated = False

            if has_webview and existing_port is not None:
                ready, _detail = self._cdp_probe(
                    existing_port, self._cdp_ready_timeout
                )
                if ready:
                    return {
                        "ok": True,
                        "endpoint": f"http://127.0.0.1:{existing_port}",
                        "recreated": False,
                    }
                # 已有 CDP 控件但端口已死：若用户已停留非空白页面则同样先征求确认
                if current_url and current_url.lower() != "about:blank":
                    if not (callable(confirm_recreate) and confirm_recreate(current_url)):
                        return {
                            "ok": False,
                            "error": "recreate_confirmation_required",
                            "msg": (
                                "当前内置浏览器自动化调试端口已失联，接管需要刷新重建当前页面"
                                "（仅尝试恢复当前 URL，表单与页面内存状态无法保证保留），"
                                "请确认后再重载。"
                            ),
                        }
                self._discard_control(form)
                recreated = True
            elif has_webview and existing_port is None:
                # 禁止后台静默 Dispose/recreate 已打开的无 CDP 页面：保留原页，由用户明确确认后才重载
                if not (callable(confirm_recreate) and confirm_recreate(current_url)):
                    return {
                        "ok": False,
                        "error": "recreate_confirmation_required",
                        "msg": (
                            "当前内置浏览器已打开页面且未启用自动化调试端口；"
                            "接管自动化需要重建控件并重新加载当前 URL（表单输入、SPA 内存状态"
                            "及前进/后退历史可能丢失），请确认后再刷新接管。"
                        ),
                    }
                self._discard_control(form)
                recreated = True

            box: Dict[str, Any] = {}

            def _create_with_cdp(
                profile_override: Optional[Path] = None,
            ) -> None:
                try:
                    port = pick_free_loopback_port()
                    kwargs: Dict[str, Any] = {
                        "additional_browser_args": f"--remote-debugging-port={port}",
                        "cdp_port": port,
                    }
                    if profile_override is not None:
                        kwargs["user_data_folder"] = profile_override
                    self._create_control(form, **kwargs)
                    restore_target = normalize_url(current_url)
                    if restore_target and restore_target.lower() != "about:blank":
                        self._url = restore_target
                        with self._lock:
                            self._pending_url = restore_target
                    box["port"] = port
                    box["ok"] = True
                except Exception as exc:
                    box["ok"] = False
                    box["error"] = str(exc)

            if not self._invoke(form, _create_with_cdp):
                return {"ok": False, "error": "invoke_failed"}
            if not box.get("ok"):
                return box or {"ok": False, "error": "invoke_failed"}
            ready, detail = self._cdp_probe(box["port"], self._cdp_ready_timeout)
            init_error = self._init_error
            if not ready and not recreated:
                # 首次新建带 CDP 控件若遇到端口竞争等瞬态失败：丢弃后换新端口重试一次
                self._discard_control(form)
                box.clear()
                recreated = True
                if not self._invoke(form, _create_with_cdp):
                    return {"ok": False, "error": "invoke_failed"}
                if not box.get("ok"):
                    return box or {"ok": False, "error": "invoke_failed"}
                ready, detail = self._cdp_probe(box["port"], self._cdp_ready_timeout)
                init_error = self._init_error or init_error
            elif not ready and "0x8007139F" in str(init_error or ""):
                # 重建后仍遇同 UserDataFolder 环境冲突（如无参初始化尚未收尾）：
                # 兜底切换到专用 automation_profile_dir() 再重建一次。
                self._discard_control(form)
                box.clear()
                if not self._invoke(
                    form, lambda: _create_with_cdp(automation_profile_dir())
                ):
                    return {"ok": False, "error": "invoke_failed"}
                if not box.get("ok"):
                    return box or {"ok": False, "error": "invoke_failed"}
                ready, detail = self._cdp_probe(box["port"], self._cdp_ready_timeout)
                init_error = self._init_error or init_error
            if not ready:
                self._discard_control(form)
                reason = f"内置浏览器 CDP 端点不可用（{detail}）"
                if init_error:
                    reason += f"；WebView2 初始化失败：{init_error}"
                return {"ok": False, "error": reason}
            return {
                "ok": True,
                "endpoint": f"http://127.0.0.1:{box['port']}",
                "recreated": recreated,
            }
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
                self._cdp_port = None
                self._explicit_hidden = False
            webview = self._webview
            form = self._form
            if webview is None:
                return {"ok": True}
            self._webview = None
            self._ready = False
            self._visible = False

            pid_box: Dict[str, Optional[int]] = {"pid": None}

            def _dispose():
                pid_box["pid"] = _read_browser_process_id(webview)
                if form is not None:
                    try:
                        form.Controls.Remove(webview)
                    except Exception:
                        pass
                try:
                    webview.Dispose()
                except Exception:
                    logger.debug("释放内嵌 WebView2 失败", exc_info=True)

            if form is not None and not self._invoke(form, _dispose):
                _dispose()
            elif form is None:
                _dispose()
            self._wait_process_exit(pid_box.get("pid"))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}