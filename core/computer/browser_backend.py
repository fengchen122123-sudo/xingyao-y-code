"""Edge/Chrome 专用 Playwright 浏览器 backend（设计 12、21、22）。

线程归属铁律（设计 12.1）：构造函数不导入也不启动 Playwright；默认 factory
在 worker 线程内才惰性 ``from playwright.sync_api import sync_playwright``。
``start()`` 创建专用 worker 线程，worker 独占 runtime/context/page；
外部 start/observe/execute/release_inputs/close 全部经有界命令队列 marshal
到 worker，调用线程绝不直接触碰 Playwright 对象。worker 未就绪、命令超时或
worker 退出都明确失败（稳定错误码），绝不在调用线程重建 Playwright 对象。

无降级原则（设计 21）：所选浏览器启动失败抛 ``computer_browser_unavailable``，
绝不 fallback 另一个浏览器；profile 锁冲突抛 ``computer_profile_locked``；
context/page 被用户关闭后后续动作抛 ``computer_browser_closed``，不自动重开；
截图失败抛错，绝不复用旧截图。

隐私（设计 22）：观察只暴露 ``scheme://netloc`` origin 与截断 200 字符的 title；
不读取 DOM 输入值、Cookie、localStorage 或密码库。
"""

from __future__ import annotations

import hashlib
import logging
import queue
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from core.computer.actions import MAX_WAIT_MS, MIN_WAIT_MS, _parse_url
from core.computer.models import (
    ComputerAction,
    ComputerControlError,
    ComputerObservation,
)

_LOGGER = logging.getLogger(__name__)

_CHANNELS = {"edge": "msedge", "chrome": "chrome"}
_VIEWPORT_WIDTH = 1440
_VIEWPORT_HEIGHT = 900
_MAX_TITLE_CHARS = 200
_COMMAND_QUEUE_SIZE = 8
_DEFAULT_COMMAND_TIMEOUT = 30.0
_DEFAULT_START_TIMEOUT = 30.0
_KEEP_SCREENSHOTS = 3

# 进程内 profile 锁注册表：同一 browser profile 只允许一个 Y-CODE context 持有
# （设计 12.2）。键为 resolve 后的 profile 目录字符串，值为持有者 id。
_PROFILE_REGISTRY_GUARD = threading.Lock()
_PROFILE_HOLDERS: dict[str, int] = {}

# launch_persistent_context 报错文案中的 profile 锁线索（真实 Playwright 在
# profile 被其他进程占用时抛 ProcessSingleton / already in use 类错误）。
_PROFILE_LOCK_HINTS = ("processsingleton", "singletonlock", "already in use", "profile locked")

_KEY_NAME_MAP = {
    "CTRL": "Control",
    "ALT": "Alt",
    "SHIFT": "Shift",
    "META": "Meta",
    "ENTER": "Enter",
    "TAB": "Tab",
    "ESC": "Escape",
    "SPACE": "Space",
    "BACKSPACE": "Backspace",
    "DELETE": "Delete",
    "HOME": "Home",
    "END": "End",
    "PAGEUP": "PageUp",
    "PAGEDOWN": "PageDown",
    "UP": "ArrowUp",
    "DOWN": "ArrowDown",
    "LEFT": "ArrowLeft",
    "RIGHT": "ArrowRight",
}


def _map_playwright_key(name: str) -> str:
    """统一动作键名 → Playwright key；字母在组合键中必须小写（Control+a）。"""
    if name in _KEY_NAME_MAP:
        return _KEY_NAME_MAP[name]
    if len(name) == 1 and name.isalpha():
        return name.lower()
    if len(name) == 1 and name.isdigit():
        return name
    if name.startswith("F") and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
        return name
    raise ComputerControlError("computer_invalid_action", f"不支持的键名: {name!r}")


class _Command:
    """投递到 worker 的一条命令；fn 为 None 表示关闭哨兵。"""

    __slots__ = ("fn", "done", "result", "error")

    def __init__(self, fn: Callable | None) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result = None
        self.error: BaseException | None = None


class BrowserComputerBackend:
    """有头 persistent context 浏览器 backend；线程模型见模块 docstring。"""

    target = "browser"

    def __init__(
        self,
        browser: str,
        profile_dir: Path,
        playwright_factory: Callable | None = None,
        *,
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
        start_timeout: float = _DEFAULT_START_TIMEOUT,
        on_page_change: Callable[[], None] | None = None,
    ) -> None:
        # 构造函数只校验参数，绝不导入或启动 Playwright。
        if type(browser) is not str or browser not in _CHANNELS:
            raise ComputerControlError(
                "computer_invalid_config",
                f"不支持的浏览器：{browser!r}，只支持 edge 或 chrome",
            )
        if not isinstance(profile_dir, Path):
            raise ComputerControlError("computer_invalid_config", "profile_dir 必须是 pathlib.Path")
        self._browser = browser
        self._profile_dir = profile_dir
        self._playwright_factory = playwright_factory
        self._command_timeout = float(command_timeout)
        self._start_timeout = float(start_timeout)
        self._on_page_change = on_page_change
        self._queue: queue.Queue = queue.Queue(maxsize=_COMMAND_QUEUE_SIZE)
        self._state_lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._ready = threading.Event()
        self._worker: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._boot_error: ComputerControlError | None = None
        # 以下字段只由 worker 线程读写（_active_page 同时作为测试只读 seam）。
        self._runtime = None
        self._playwright = None
        self._context = None
        self._active_page = None
        self._context_closed = False
        self._pressed_buttons: set[str] = set()
        self._profile_lock_key: str | None = None
        self._screenshot_dir: Path | None = None
        self._screenshot_counter = 0

    @property
    def browser(self) -> str:
        return self._browser

    @property
    def active_page(self):
        """测试/诊断只读 seam：返回 worker 持有的当前 active page 对象。

        调用方不得对返回对象调用任何 Playwright 方法（线程归属在 worker）。
        """
        return self._active_page

    # ---------------- 外部 API（全部 marshal 到 worker） ----------------

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                return
            if self._closed:
                raise ComputerControlError(
                    "computer_browser_closed",
                    "浏览器 backend 已关闭，请创建新实例",
                )
            thread = threading.Thread(
                target=self._worker_main,
                name=f"computer-browser-{self._browser}",
                daemon=True,
            )
            self._worker = thread
            thread.start()
        if not self._ready.wait(timeout=self._start_timeout):
            with self._state_lock:
                self._closed = True
            raise ComputerControlError(
                "computer_backend_start_failed",
                f"{self._browser} 浏览器启动超时",
            )
        if self._boot_error is not None:
            with self._state_lock:
                self._closed = True
            thread.join(timeout=10)
            raise self._boot_error
        with self._state_lock:
            self._started = True

    def observe(self, run_id: str) -> ComputerObservation:
        return self._call("observe", lambda: self._cmd_observe(run_id))

    def execute(self, run_id: str, action: ComputerAction) -> None:
        self._call(f"execute:{action.kind}", lambda: self._cmd_execute(run_id, action))

    def release_inputs(self) -> None:
        """尽力释放 worker 内跟踪的按下状态；backend 不可用时静默 no-op。"""
        with self._state_lock:
            worker = self._worker
            available = (
                self._started
                and not self._closed
                and worker is not None
                and worker.is_alive()
            )
        if not available:
            return
        try:
            self._call("release_inputs", self._cmd_release_inputs)
        except ComputerControlError:
            _LOGGER.warning("释放浏览器输入状态失败", exc_info=True)

    def close(self) -> None:
        with self._state_lock:
            if self._closed or not self._started:
                # 幂等；未启动时 no-op，不消耗生命周期。
                return
            self._closed = True
            worker = self._worker
        try:
            self._queue.put(_Command(None), timeout=5)
        except queue.Full:
            _LOGGER.warning("浏览器 worker 命令队列已满，关闭哨兵投递失败")
        if worker.is_alive():
            worker.join(timeout=10)
            if worker.is_alive():
                _LOGGER.warning("浏览器 worker 在 close 超时后仍未退出（daemon 线程）")

    # ---------------- worker 线程：启动、命令循环、收尾 ----------------

    def _worker_main(self) -> None:
        try:
            self._boot()
        except BaseException as error:
            if isinstance(error, ComputerControlError):
                self._boot_error = error
            else:
                _LOGGER.exception("浏览器 backend worker 启动失败")
                self._boot_error = ComputerControlError(
                    "computer_backend_start_failed",
                    f"浏览器 backend 启动失败：{type(error).__name__}",
                )
            self._ready.set()
            self._teardown()
            return
        self._ready.set()
        try:
            self._command_loop()
        finally:
            self._teardown()

    def _boot(self) -> None:
        try:
            self._profile_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ComputerControlError(
                "computer_backend_start_failed",
                f"无法创建浏览器 profile 目录：{error}",
            ) from error
        resolved = str(self._profile_dir.resolve())
        self._acquire_profile_lock(resolved)
        self._profile_lock_key = resolved
        factory = self._playwright_factory
        if factory is None:
            # 惰性导入：只有真实启动时才触碰 playwright，且发生在 worker 线程。
            from playwright.sync_api import sync_playwright as factory
        try:
            self._runtime = factory()
            self._playwright = self._runtime.start()
        except ComputerControlError:
            raise
        except Exception as error:
            raise ComputerControlError(
                "computer_backend_start_failed",
                f"Playwright runtime 启动失败：{type(error).__name__}",
            ) from error
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=resolved,
                channel=_CHANNELS[self._browser],
                headless=False,
                viewport={"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT},
                device_scale_factor=1,
            )
        except Exception as error:
            raise self._map_launch_error(error) from error
        self._screenshot_dir = Path(tempfile.mkdtemp(prefix="ycode-computer-browser-"))
        context = self._context
        pages = [page for page in context.pages if not page.is_closed()]
        if pages:
            # 已恢复的历史 page：无焦点信息时取最后一个可见 page。
            self._active_page = pages[-1]
        else:
            # 初始零 page：创建且只创建一个 about:blank。
            page = context.new_page()
            page.goto("about:blank")
            self._active_page = page
        # 初始 page 落定后再挂事件，避免启动过程误报页面变化。
        context.on("page", self._on_new_page)
        context.on("close", self._on_context_close)

    def _map_launch_error(self, error: Exception) -> ComputerControlError:
        message = str(error).lower()
        if any(hint in message for hint in _PROFILE_LOCK_HINTS):
            return ComputerControlError(
                "computer_profile_locked",
                f"浏览器 profile 被其他进程占用：{self._profile_dir}",
            )
        return ComputerControlError(
            "computer_browser_unavailable",
            f"无法启动所选浏览器 {self._browser}（channel {_CHANNELS[self._browser]}），"
            "请确认已安装；不会自动改用其他浏览器",
        )

    def _command_loop(self) -> None:
        while True:
            command = self._queue.get()
            if command.fn is None:
                return
            try:
                command.result = command.fn()
            except BaseException as error:
                command.error = error
            finally:
                command.done.set()

    def _teardown(self) -> None:
        context = self._context
        self._context = None
        if context is not None:
            try:
                context.close()
            except Exception:
                _LOGGER.exception("关闭浏览器 context 失败")
        playwright = self._playwright
        self._playwright = None
        self._runtime = None
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                _LOGGER.exception("停止 Playwright runtime 失败")
        if self._profile_lock_key is not None:
            self._release_profile_lock(self._profile_lock_key)
            self._profile_lock_key = None
        if self._screenshot_dir is not None:
            shutil.rmtree(self._screenshot_dir, ignore_errors=True)
            self._screenshot_dir = None
        self._pressed_buttons = set()
        self._active_page = None

    # ---------------- worker 内：页面事件 ----------------

    def _on_new_page(self, page) -> None:
        """context page 事件：最后获得焦点的可见 page 成为 active，并发出页面变化事件。"""
        self._active_page = page
        callback = self._on_page_change
        if callback is not None:
            try:
                callback()
            except Exception:
                _LOGGER.exception("浏览器页面变化事件回调失败")

    def _on_context_close(self, *args) -> None:
        self._context_closed = True

    # ---------------- worker 内：命令实现 ----------------

    def _ensure_usable(self):
        if self._context_closed or self._context is None:
            raise ComputerControlError(
                "computer_browser_closed",
                "浏览器窗口已被用户关闭，不会自动重开",
            )
        page = self._active_page
        if page is not None and not page.is_closed():
            return page
        # active page 被关闭但仍有其他可见 page：切换到最后一个，绝不自动重开新 page。
        for candidate in reversed(list(self._context.pages)):
            if not candidate.is_closed():
                self._active_page = candidate
                return candidate
        raise ComputerControlError(
            "computer_browser_closed",
            "浏览器页面已全部关闭，不会自动重开",
        )

    def _cmd_observe(self, run_id: str) -> ComputerObservation:
        page = self._ensure_usable()
        try:
            png = page.screenshot(type="png")
        except ComputerControlError:
            raise
        except Exception as error:
            raise ComputerControlError(
                "computer_action_failed",
                f"浏览器截图失败：{type(error).__name__}",
            ) from error
        if not isinstance(png, (bytes, bytearray)) or not bytes(png):
            raise ComputerControlError("computer_action_failed", "浏览器截图未返回 PNG bytes")
        png_bytes = bytes(png)
        try:
            title = page.title()
        except Exception as error:
            raise ComputerControlError(
                "computer_action_failed",
                f"读取页面标题失败：{type(error).__name__}",
            ) from error
        if not isinstance(title, str):
            title = ""
        url = page.url if isinstance(page.url, str) else ""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        path = self._store_screenshot(png_bytes)
        return ComputerObservation(
            run_id=run_id,
            target="browser",
            action="observe",
            ok=True,
            screen_sha256=hashlib.sha256(png_bytes).hexdigest(),
            png_path=str(path),
            width=_VIEWPORT_WIDTH,
            height=_VIEWPORT_HEIGHT,
            original_width=_VIEWPORT_WIDTH,
            original_height=_VIEWPORT_HEIGHT,
            origin_x=0,
            origin_y=0,
            scale_x=1.0,
            scale_y=1.0,
            page_origin=origin,
            page_title=title[:_MAX_TITLE_CHARS],
        )

    def _cmd_execute(self, run_id: str, action: ComputerAction) -> None:
        if action.target != "browser":
            raise ComputerControlError(
                "computer_invalid_action",
                f"浏览器 backend 只执行 browser target，收到 {action.target!r}",
            )
        kind = action.kind
        if kind == "observe":
            return
        if kind == "request_user_takeover":
            raise ComputerControlError(
                "computer_invalid_action",
                "request_user_takeover 由 controller 处理，不进入 backend",
            )
        if kind == "wait":
            duration = action.duration_ms
            if type(duration) is not int or not MIN_WAIT_MS <= duration <= MAX_WAIT_MS:
                raise ComputerControlError(
                    "computer_invalid_action",
                    f"duration_ms 必须在 {MIN_WAIT_MS}-{MAX_WAIT_MS} 毫秒之间",
                )
            time.sleep(duration / 1000)
            return
        page = self._ensure_usable()
        try:
            self._dispatch_action(page, action)
        except ComputerControlError:
            raise
        except Exception as error:
            raise ComputerControlError(
                "computer_action_failed",
                f"浏览器动作 {kind} 执行失败：{type(error).__name__}",
            ) from error

    def _cmd_release_inputs(self) -> None:
        buttons = list(self._pressed_buttons)
        self._pressed_buttons = set()
        if self._context_closed or self._context is None:
            return
        page = self._active_page
        if page is None or page.is_closed():
            return
        for button in buttons:
            try:
                page.mouse.up(button=button)
            except Exception:
                _LOGGER.exception("释放鼠标按键失败")

    def _dispatch_action(self, page, action: ComputerAction) -> None:
        kind = action.kind
        if kind == "click":
            x, y = self._checked_point(action)
            page.mouse.click(x, y, button=action.button)
        elif kind == "double_click":
            x, y = self._checked_point(action)
            page.mouse.dblclick(x, y, button=action.button)
        elif kind == "move":
            x, y = self._checked_point(action)
            page.mouse.move(x, y)
        elif kind == "scroll":
            if action.x is not None or action.y is not None:
                x, y = self._checked_point(action)
                page.mouse.move(x, y)
            page.mouse.wheel(action.delta_x, action.delta_y)
        elif kind == "drag":
            self._exec_drag(page, action)
        elif kind == "type_text":
            if not isinstance(action.text, str) or not action.text:
                raise ComputerControlError("computer_invalid_action", "type_text 需要非空 text")
            page.keyboard.insert_text(action.text)
        elif kind == "keypress":
            if not action.keys:
                raise ComputerControlError("computer_invalid_action", "keypress 需要至少一个键")
            page.keyboard.press("+".join(_map_playwright_key(key) for key in action.keys))
        elif kind == "navigate":
            # 复用 parse 层校验：只接受带主机名的 http/https URL。
            page.goto(_parse_url(action.url))
        elif kind == "back":
            page.go_back()
        elif kind == "forward":
            page.go_forward()
        elif kind == "reload":
            page.reload()
        else:
            raise ComputerControlError(
                "computer_invalid_action",
                f"浏览器 backend 不支持动作：{kind!r}",
            )

    def _exec_drag(self, page, action: ComputerAction) -> None:
        points = action.path
        if len(points) < 2:
            raise ComputerControlError("computer_invalid_action", "drag 需要至少 2 个 path 点")
        for point_x, point_y in points:
            self._check_coordinates(point_x, point_y)
        start_x, start_y = points[0]
        button = action.button
        page.mouse.move(start_x, start_y)
        page.mouse.down(button=button)
        self._pressed_buttons.add(button)
        try:
            for point_x, point_y in points[1:]:
                page.mouse.move(point_x, point_y)
        finally:
            # try/finally 保证 mouse.up；up 失败时保留按下标记供 release_inputs 重试。
            try:
                page.mouse.up(button=button)
            except Exception:
                _LOGGER.exception("拖拽收尾 mouse.up 失败")
                raise
            else:
                self._pressed_buttons.discard(button)

    def _checked_point(self, action: ComputerAction) -> tuple[int, int]:
        x, y = action.x, action.y
        if type(x) is not int or type(y) is not int:
            raise ComputerControlError("computer_invalid_action", "坐标 x/y 必须是整数")
        self._check_coordinates(x, y)
        return x, y

    def _check_coordinates(self, x: int, y: int) -> None:
        if not (0 <= x < _VIEWPORT_WIDTH and 0 <= y < _VIEWPORT_HEIGHT):
            raise ComputerControlError(
                "computer_coordinate_out_of_bounds",
                f"坐标 ({x}, {y}) 超出浏览器视口 {_VIEWPORT_WIDTH}x{_VIEWPORT_HEIGHT} 半开区间",
            )

    # ---------------- worker 内：截图短期存储 ----------------

    def _store_screenshot(self, png: bytes) -> Path:
        assert self._screenshot_dir is not None
        self._screenshot_counter += 1
        path = self._screenshot_dir / f"shot-{self._screenshot_counter}.png"
        path.write_bytes(png)
        shots = sorted(
            self._screenshot_dir.glob("shot-*.png"),
            key=lambda item: int(item.stem.rsplit("-", 1)[1]),
        )
        for old in shots[:-_KEEP_SCREENSHOTS]:
            old.unlink(missing_ok=True)
        return path

    # ---------------- 命令 marshal 与进程内 profile 锁 ----------------

    def _call(self, description: str, fn: Callable):
        with self._state_lock:
            worker = self._worker
            started = self._started
            closed = self._closed
        if closed:
            raise ComputerControlError("computer_browser_closed", "浏览器 backend 已关闭")
        if not started or worker is None:
            raise ComputerControlError(
                "computer_browser_unavailable",
                "浏览器 backend 尚未启动",
            )
        if not worker.is_alive():
            raise ComputerControlError(
                "computer_browser_closed",
                "浏览器 worker 已退出，浏览器不可用",
            )
        command = _Command(fn)
        with self._submit_lock:
            try:
                self._queue.put(command, timeout=self._command_timeout)
            except queue.Full as error:
                raise ComputerControlError(
                    "computer_action_timeout",
                    f"浏览器命令排队超时：{description}",
                ) from error
            if not command.done.wait(timeout=self._command_timeout):
                raise ComputerControlError(
                    "computer_action_timeout",
                    f"浏览器命令执行超时：{description}",
                )
        if command.error is not None:
            raise command.error
        return command.result

    def _acquire_profile_lock(self, resolved: str) -> None:
        with _PROFILE_REGISTRY_GUARD:
            if resolved in _PROFILE_HOLDERS:
                raise ComputerControlError(
                    "computer_profile_locked",
                    f"浏览器 profile 已被本进程另一个会话占用：{resolved}",
                )
            _PROFILE_HOLDERS[resolved] = id(self)

    def _release_profile_lock(self, resolved: str) -> None:
        with _PROFILE_REGISTRY_GUARD:
            if _PROFILE_HOLDERS.get(resolved) == id(self):
                del _PROFILE_HOLDERS[resolved]
