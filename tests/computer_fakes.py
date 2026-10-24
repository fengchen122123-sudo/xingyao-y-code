"""电脑操控测试共享 fixture 契约（计划 Cross-Task Interface Contract）。

Task 3 已提供 use_temp_config；Task 4 补充 enabled_settings fixture、
openai_compatible_provider() 数据 helper 与 capability 常量
GENERIC_CAPABILITY / NATIVE_BROWSER_CAPABILITY / DISABLED_CAPABILITY；
Task 5 补充 FakeHotkeyApi、fake_png_bytes(seed) 与 http_status(url)；
Task 6 补充 FakePlaywrightRuntime / FakeBrowserContext / FakePage /
FakeMouse / FakeKeyboard（记录 launch 参数、page 调用与每个调用的 thread id，
可注入 error，绝不启动真实浏览器）；
本 Task（Task 7）补充 FakeImage / FakeWin32Adapter（虚拟桌面截图与 SendInput
的纯 fake：记录点击/移动/滚轮/文本/组合键，支持 fail_after 注入失败与
secure desktop/无前台窗口/尺寸不一致场景，绝不触碰真实截屏、鼠标或键盘）。
Task 8 补充 FakeComputerBackend（ComputerBackend 协议纯 fake：记录
start/observe/execute/release_inputs/close 调用计数与线程，支持注入截图 hash
序列控制 no-progress、注入 observe/execute/close 错误、execute_gate 阻塞验证
动作串行，绝不触碰真实键鼠/浏览器）。
Task 9 补充 FakeController（ComputerControlService 最小 fake：固定 run/owner、
记录动作、按序注入观察或错误、snapshot 暴露 action_count，供固定适配器测试；
绝不触碰真实键鼠/浏览器/截屏）。
Task 13a 补充 FakeBridgeController（ComputerControlService 的 GUI Bridge fake：
listener 注册/事件发射 + start_run(run_id=)/pause/resume/stop 透传记录与
run_id 拒绝语义，供 Bridge 事件桥与控制 API 测试；绝不触碰真实键鼠/浏览器/截屏）。
Task 16 给 FakeComputerBackend 增补 png_path（观察携带的虚拟截图路径，不读
真实文件，供预览发布接线与隐私审计测试）。
元素定位修复 E1 给 FakePage 增补元素收集脚本注入点（element_collect_result /
element_collect_error，默认空列表，保证既有测试零影响）。
元素定位修复 E2 给 FakePage 增补 locator 记录点（locator_calls / last_locator，
FakeLocator 记录 click/fill 参数并支持异常注入），FakeKeyboard 增补 type
（set_value_element 的键盘回落路径）。
其余请求捕获 helper 由后续 Task 按需在本文件扩展，禁止平行实现。
"""

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

import config
from core.computer.browser_backend import ELEMENT_COLLECT_SCRIPT, PAGE_TEXT_SCRIPT
from core.computer.models import (
    ComputerCapability,
    ComputerControlError,
    ComputerObservation,
    ComputerRunSnapshot,
)
from core.computer.settings import ComputerSettingsSnapshot


def use_temp_config(tmp_path, monkeypatch, values):
    """把 values 写入临时 config.json 并 monkeypatch 指向它，返回 config 路径（Path）。

    同时把 TOML overlay 指向同目录下不存在的 config.toml，隔离真实
    ~/.ycode/config.toml 对读取结果的只读覆盖。
    """
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(tmp_path / "config.toml"))
    return cfg


@pytest.fixture
def enabled_settings():
    """两个开关均开启、Windows 平台、edge 浏览器的设置快照。"""
    return ComputerSettingsSnapshot(
        browser_control_enabled=True,
        computer_control_enabled=True,
        computer_browser="edge",
        browser_options=(),
        profile_display_path="",
        platform_supported=True,
        generation=1,
    )


def openai_compatible_provider() -> dict:
    """OpenAI 兼容（非官方）provider dict；generic 路径不校验 provider 字段。"""
    return {"protocol": "openai", "base_url": "https://api.deepseek.com"}


GENERIC_CAPABILITY = ComputerCapability(
    enabled=True,
    protocol="generic",
    targets=("browser", "desktop"),
    browser="edge",
    settings_generation=1,
)

NATIVE_BROWSER_CAPABILITY = ComputerCapability(
    enabled=True,
    protocol="native_openai",
    targets=("browser",),
    browser="edge",
    settings_generation=1,
)

# Task 11a：browser 与 desktop 双 target 同时开启的原生能力快照。
NATIVE_DUAL_TARGET_CAPABILITY = ComputerCapability(
    enabled=True,
    protocol="native_openai",
    targets=("browser", "desktop"),
    browser="edge",
    settings_generation=1,
)

DISABLED_CAPABILITY = ComputerCapability(
    enabled=False,
    error_code="computer_disabled",
    reason="电脑操控未开启",
)


# ---------------- Task 5 导出：热键 fake 与预览服务 helper ----------------


class FakeHotkeyApi:
    """最小确定性 HotkeyApi fake：只记录注册/注销，绝不触碰真实系统热键。"""

    def __init__(self, register_result: bool = True, last_error_code: int = 0):
        self._register_result = register_result
        # 与 WindowsHotkeyApi 对齐的可选诊断字段：注册失败时携带 Win32 错误码，
        # 供 EmergencyStopHotkey 生成可读原因（如 1409 = 热键已被占用）。
        self.last_error_code = last_error_code
        self.register_calls = 0
        self.unregister_calls = 0
        self.on_hotkey = None

    def register(self, on_hotkey):
        self.register_calls += 1
        if self._register_result:
            self.on_hotkey = on_hotkey
        return self._register_result

    def unregister(self):
        self.unregister_calls += 1
        self.on_hotkey = None


def fake_png_bytes(seed: int) -> bytes:
    """确定性伪 PNG 字节：合法 PNG 魔数 + seed 派生内容；仅供预览服务测试。"""
    return b"\x89PNG\r\n\x1a\n" + b"fake" + int(seed).to_bytes(4, "big", signed=False)


def http_status(url: str) -> int:
    """GET 一次并返回 HTTP 状态码；HTTPError（如 404）返回其 code。"""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


# ---------------- Task 6 导出：Playwright fake（绝不启动真实浏览器） ----------------


class FakeMouse:
    """page.mouse fake：按调用顺序记录 (name, *args)，可注入 move/up 失败。"""

    def __init__(self, runtime):
        self._runtime = runtime
        self.calls = []
        self.move_fail_after = None  # 第 N+1 次 move 起抛 RuntimeError
        self.up_error = None
        self._move_count = 0

    def click(self, x, y, button="left", **kwargs):
        self._runtime._record_call("mouse.click")
        self.calls.append(("click", x, y, button))

    def dblclick(self, x, y, button="left", **kwargs):
        self._runtime._record_call("mouse.dblclick")
        self.calls.append(("dblclick", x, y, button))

    def move(self, x, y, **kwargs):
        self._runtime._record_call("mouse.move")
        self._move_count += 1
        self.calls.append(("move", x, y))
        if self.move_fail_after is not None and self._move_count > self.move_fail_after:
            raise RuntimeError("injected mouse.move failure")

    def down(self, button="left", **kwargs):
        self._runtime._record_call("mouse.down")
        self.calls.append(("down", button))

    def up(self, button="left", **kwargs):
        self._runtime._record_call("mouse.up")
        self.calls.append(("up", button))
        if self.up_error is not None:
            raise self.up_error

    def wheel(self, delta_x, delta_y, **kwargs):
        self._runtime._record_call("mouse.wheel")
        self.calls.append(("wheel", delta_x, delta_y))


class FakeKeyboard:
    """page.keyboard fake：记录 press/insert_text/down/up。

    - ``down_fail_after``：第 N+1 次 down 起抛 ``down_error``（默认
      RuntimeError），用于验证部分按下后的逆序释放；
    - ``up_error``：up 时抛出，用于验证释放失败保留按下记录。
    """

    def __init__(self, runtime):
        self._runtime = runtime
        self.calls = []
        self.down_fail_after = None
        self.down_error = None
        self.up_error = None
        self._down_count = 0

    def press(self, key, **kwargs):
        self._runtime._record_call("keyboard.press")
        self.calls.append(("press", key))

    def insert_text(self, text, **kwargs):
        self._runtime._record_call("keyboard.insert_text")
        self.calls.append(("insert_text", text))

    def type(self, text, **kwargs):
        # E2：set_value_element 的 fill 回落路径（点击聚焦 + 键盘逐字符输入）。
        self._runtime._record_call("keyboard.type")
        self.calls.append(("type", text))

    def down(self, key, **kwargs):
        self._runtime._record_call("keyboard.down")
        self._down_count += 1
        if self.down_fail_after is not None and self._down_count > self.down_fail_after:
            raise self.down_error or RuntimeError("injected keyboard.down failure")
        self.calls.append(("down", key))

    def up(self, key, **kwargs):
        self._runtime._record_call("keyboard.up")
        self.calls.append(("up", key))
        if self.up_error is not None:
            raise self.up_error


class FakeLocator:
    """page.locator() fake（E2 元素动作）：记录 selector 与 click/fill 调用参数。

    - ``click_calls``：按序记录 click 的 kwargs（含 timeout）；
    - ``fill_calls``：按序记录 (text, kwargs)；
    - ``click_error`` / ``fill_error``：注入异常，验证元素语义错误码与
      set_value_element 的键盘回落路径；
    - ``count_result``：locator.count() 返回值，用于验证「元素不在 DOM →
      stale」的失效判定（E2 评审修复）；
    - 绝不触碰真实页面 DOM 或 Playwright。
    """

    def __init__(self, selector):
        self.selector = selector
        self.click_calls = []
        self.fill_calls = []
        self.click_error = None
        self.fill_error = None
        self.count_result = 1

    def click(self, **kwargs):
        self.click_calls.append(dict(kwargs))
        if self.click_error is not None:
            raise self.click_error

    def fill(self, text, **kwargs):
        self.fill_calls.append((text, dict(kwargs)))
        if self.fill_error is not None:
            raise self.fill_error

    def count(self, **kwargs):
        return self.count_result


class FakePage:
    """page fake：记录 goto/导航/截图调用，支持 closed 标记与错误/阻塞注入。"""

    def __init__(self, runtime, url="about:blank", title="", screenshot_bytes=None):
        self._runtime = runtime
        self._context = None
        self.url = url
        self.page_title = title
        self.closed = False
        # R5 可见性探针字段：document.visibilityState / document.hasFocus()
        self.visibility_state = "visible"
        self.focused = True
        self.mouse = FakeMouse(runtime)
        self.keyboard = FakeKeyboard(runtime)
        self.goto_calls = []
        self.navigation_calls = []
        self.navigation_kwargs = []
        self.screenshot_calls = []
        self.evaluate_calls = []
        self.screenshot_bytes = screenshot_bytes  # None 时回落到 runtime.screenshot_bytes
        self.screenshot_error = None
        self.screenshot_gate = None  # threading.Event：设置后 screenshot 阻塞等待
        self.screenshot_delay = 0.0
        self.concurrency_violations = 0
        self._inside_screenshot = False
        # E1 元素收集脚本注入点：None 表示返回空列表（既有测试零影响）；
        # error 非空时 evaluate 抛错，用于验证「收集失败不中断 observe」；
        # hook 非空时在收集脚本求值后调用（测试模拟收集期间页面变化）。
        self.element_collect_result = None
        self.element_collect_error = None
        self.element_collect_hook = None
        # B 方案（窗口被遮挡时的降级观察）：页面正文本注入点。默认空串；
        # error 非空时 evaluate 抛错，用于验证「正文读取失败不中断 observe」。
        self.page_text = ""
        self.page_text_error = None
        # E2 元素动作注入点：locator 调用记录（selector 列表）与最近创建的
        # FakeLocator（供断言 click/fill 参数与异常注入路径）；预设字段
        # 在 locator 创建时应用到新对象（execute 前设置即可注入失败/命中数）。
        self.locator_calls = []
        self.last_locator = None
        self.locator_click_error = None
        self.locator_fill_error = None
        self.locator_count = 1

    def is_closed(self):
        self._runtime._record_call("page.is_closed")
        return self.closed

    def evaluate(self, expression, *args, **kwargs):
        """仅支持 R5 可见性探针、E1 元素收集与正文提取脚本；其他表达式一律拒绝（隐私边界）。"""
        self._runtime._record_call("page.evaluate")
        self.evaluate_calls.append(expression)
        if expression == ELEMENT_COLLECT_SCRIPT:
            if self.element_collect_error is not None:
                raise self.element_collect_error
            result = self.element_collect_result
            if self.element_collect_hook is not None:
                self.element_collect_hook(self)
            return [] if result is None else result
        if expression == PAGE_TEXT_SCRIPT:
            if self.page_text_error is not None:
                raise self.page_text_error
            return self.page_text
        if "visibilityState" in str(expression) and "hasFocus" in str(expression):
            return [self.visibility_state, self.focused]
        raise AssertionError(
            f"fake page 只允许可见性探针、元素收集与正文提取脚本，拒绝 evaluate: {expression!r}"
        )

    def title(self):
        self._runtime._record_call("page.title")
        return self.page_title

    def locator(self, selector):
        """E2：返回记录型 FakeLocator；每次调用创建独立对象并记为最近对象。"""
        self._runtime._record_call("page.locator")
        self.locator_calls.append(selector)
        locator = FakeLocator(selector)
        locator.click_error = self.locator_click_error
        locator.fill_error = self.locator_fill_error
        locator.count_result = self.locator_count
        self.last_locator = locator
        return locator

    def goto(self, url, **kwargs):
        self._runtime._record_call("page.goto")
        self.goto_calls.append(url)
        self.navigation_kwargs.append(("goto", dict(kwargs)))
        self.url = url

    def go_back(self, **kwargs):
        self._runtime._record_call("page.go_back")
        self.navigation_calls.append("go_back")
        self.navigation_kwargs.append(("go_back", dict(kwargs)))

    def go_forward(self, **kwargs):
        self._runtime._record_call("page.go_forward")
        self.navigation_calls.append("go_forward")
        self.navigation_kwargs.append(("go_forward", dict(kwargs)))

    def reload(self, **kwargs):
        self._runtime._record_call("page.reload")
        self.navigation_calls.append("reload")
        self.navigation_kwargs.append(("reload", dict(kwargs)))

    def screenshot(self, type=None, **kwargs):  # noqa: A002 - playwright kwarg 名
        self._runtime._record_call("page.screenshot")
        if self._inside_screenshot:
            self.concurrency_violations += 1
        self._inside_screenshot = True
        try:
            if self.screenshot_gate is not None:
                self.screenshot_gate.wait(timeout=10)
            if self.screenshot_delay:
                time.sleep(self.screenshot_delay)
            if self.screenshot_error is not None:
                raise self.screenshot_error
            self.screenshot_calls.append({"type": type, **kwargs})
            if self.screenshot_bytes is not None:
                return self.screenshot_bytes
            return self._runtime.screenshot_bytes
        finally:
            self._inside_screenshot = False

    def close(self):
        self.closed = True


class FakeBrowserContext:
    """persistent context fake：pages 列表、page/close 事件、new_page 计数。"""

    def __init__(self, runtime, pages=()):
        self._runtime = runtime
        self.pages = list(pages)
        for page in self.pages:
            page._context = self
        self.new_page_calls = 0
        self.closed = False
        self._handlers = {"page": [], "close": []}

    def on(self, event, handler):
        self._runtime._record_call("context.on")
        self._handlers.setdefault(event, []).append(handler)

    def new_page(self):
        self._runtime._record_call("context.new_page")
        self.new_page_calls += 1
        page = FakePage(self._runtime)
        page._context = self
        self.pages.append(page)
        self._emit("page", page)
        return page

    def simulate_popup(self, url="https://popup.example/", title="Popup"):
        """测试专用：模拟用户/脚本打开的 popup，触发 context 的 page 事件。

        模拟真实焦点转移（R5）：既有页转为 hidden/失焦，新 popup 可见且聚焦。
        """
        for page in self.pages:
            if not page.closed:
                page.visibility_state = "hidden"
                page.focused = False
        page = FakePage(self._runtime, url=url, title=title)
        page._context = self
        self.pages.append(page)
        self._emit("page", page)
        return page

    def close(self):
        self._runtime._record_call("context.close")
        self.closed = True
        for page in self.pages:
            page.closed = True
        self._emit("close")

    def _emit(self, event, *args):
        for handler in list(self._handlers.get(event, [])):
            handler(*args)


class FakeChromium:
    """runtime.chromium fake：记录 launch_persistent_context 参数与 channel。"""

    def __init__(self, runtime):
        self._runtime = runtime
        self.launch_persistent_context_calls = []

    def launch_persistent_context(self, **kwargs):
        self._runtime._record_call("chromium.launch_persistent_context")
        self.launch_persistent_context_calls.append(dict(kwargs))
        self._runtime.requested_channels.append(kwargs.get("channel"))
        if self._runtime.error is not None:
            raise self._runtime.error
        context = FakeBrowserContext(self._runtime, pages=self._runtime.initial_pages)
        self._runtime.contexts.append(context)
        return context


class FakePlaywrightRuntime:
    """sync_playwright() 上下文管理器 fake。

    - ``error``：launch_persistent_context 时抛出（记录 channel 后再抛）；
    - ``start_error``：runtime.start() 时抛出；
    - ``start_thread_id`` 记录 runtime 启动线程；``call_thread_ids`` 按序记录
      每个 fake Playwright 调用的 thread id，用于断言 worker 线程独占；
    - 绝不启动真实浏览器、绝不导入 playwright。
    """

    def __init__(self, error=None, initial_pages=(), screenshot_bytes=None, start_error=None):
        self.error = error
        self.start_error = start_error
        self.initial_pages = list(initial_pages)
        for page in self.initial_pages:
            page._runtime = self
        self.screenshot_bytes = screenshot_bytes if screenshot_bytes is not None else fake_png_bytes(0)
        self.requested_channels = []
        self.chromium = FakeChromium(self)
        self.contexts = []
        self.started = False
        self.stopped = False
        self.start_thread_id = None
        self.call_thread_ids = []

    @property
    def last_context(self):
        return self.contexts[-1] if self.contexts else None

    def _record_call(self, label):
        self.call_thread_ids.append(threading.get_ident())

    def start(self):
        self._record_call("runtime.start")
        if self.start_error is not None:
            raise self.start_error
        self.started = True
        self.start_thread_id = threading.get_ident()
        return self

    def stop(self):
        self._record_call("runtime.stop")
        self.started = False
        self.stopped = True


# ---------------- Task 7 导出：Windows 桌面截图与 SendInput fake ----------------


class FakeImage:
    """Pillow Image 的纯 fake：只携带尺寸与虚拟桌面原点，绝不真实截屏。

    - ``size``/``resize()``/``save()`` 与 Pillow 最小接口对齐；
    - ``save()`` 写入由 (width, height, origin_x, origin_y) 决定的确定性字节，
      并把调用参数记录到 ``saved_with`` 供断言确定参数编码；
    - 默认 origin 为 (0, 0)；brief 样例使用的 3840x2160 @ (-1920, 0) 模拟
      主屏右侧 + 左侧扩展屏的多显示器虚拟桌面。
    """

    def __init__(self, width, height, origin_x=0, origin_y=0):
        self.width = width
        self.height = height
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.resize_calls = []
        self.crop_calls = []
        self.saved_with = []

    @property
    def size(self):
        return (self.width, self.height)

    def resize(self, size, resample=None):
        self.resize_calls.append((tuple(size), resample))
        return FakeImage(size[0], size[1], self.origin_x, self.origin_y)

    def crop(self, box):
        """R10：区域裁剪 fake；只记录并返回区域尺寸的确定性 FakeImage。"""
        self.crop_calls.append(tuple(box))
        return FakeImage(box[2] - box[0], box[3] - box[1], self.origin_x, self.origin_y)

    def save(self, buffer, format=None, **params):
        self.saved_with.append((format, dict(params)))
        buffer.write(
            b"\x89PNG\r\n\x1a\n"
            + b"fake"
            + int(self.width).to_bytes(4, "big", signed=False)
            + int(self.height).to_bytes(4, "big", signed=False)
            + int(self.origin_x).to_bytes(4, "big", signed=True)
            + int(self.origin_y).to_bytes(4, "big", signed=True)
        )


class FakeWin32Adapter:
    """Win32InputAdapter 的纯 fake：记录全部输入，绝不触碰真实 SendInput/截屏。

    - ``virtual_rect`` 默认 (-1920, 0, 3840, 2160)，与 brief 样例的
      FakeImage(3840, 2160, origin_x=-1920) 多屏虚拟桌面一致；
    - ``fail_after``：第 fail_after+1 个底层输入事件起抛
      computer_input_rejected（模拟 SendInput 部分失败/UIPI 拒绝）；
    - ``desktop_name``/``has_foreground`` 用于构造 secure desktop 与
      无前台窗口场景；``preflight_error`` 直接注入 preflight 失败；
    - ``keys_down``/``buttons_down`` 暴露按下状态，验证 finally 释放。
    """

    def __init__(self, *, virtual_rect=(-1920, 0, 3840, 2160), desktop_name="Default",
                 has_foreground=True, foreground_title="Fake Window", foreground_id=1001,
                 preflight_error=None, fail_after=None):
        self.virtual_rect = virtual_rect
        self.desktop_name = desktop_name
        self.has_foreground = has_foreground
        self.foreground_title = foreground_title
        self.foreground_id = foreground_id  # R5：前台窗口本地身份，测试可改写模拟切窗
        self.preflight_error = preflight_error
        self.fail_after = fail_after
        self.preflight_calls = 0
        self.interactive_checks = 0
        self.clicks = []
        self.double_clicks = []
        self.moves = []
        self.wheels = []
        self.hwheels = []
        self.drags = []
        self.texts = []
        self.keys_sent = []
        self.clipboard_writes = []
        self.mouse_calls = []
        self.keys_down = set()
        self.buttons_down = set()
        self.release_all_calls = 0
        self._events_sent = 0
        # 通用化重构 Task C：输入前置激活的记录版 seam。
        self.window_from_point_calls = []
        self.set_foreground_calls = []
        # 统一时间线（"window_from_point"/"set_foreground"/"send_mouse"/...）：
        # 断言激活发生在真实输入之前。
        self.call_order = []
        # 目标点所属窗口；None 表示与当前前台一致（默认不触发激活）。
        self.window_at_point_id = None
        # 置 False 模拟 SetForegroundWindow 失败（含 Alt 键技巧重试后仍失败）。
        self.set_foreground_result = True

    def window_from_point(self, x, y):
        self.window_from_point_calls.append((x, y))
        self.call_order.append(("window_from_point", x, y))
        if self.window_at_point_id is not None:
            return self.window_at_point_id
        return self.foreground_id

    def set_foreground_window(self, hwnd):
        self.set_foreground_calls.append(hwnd)
        self.call_order.append(("set_foreground", hwnd))
        if self.set_foreground_result:
            self.foreground_id = hwnd  # 激活成功：前台身份随之切换（与真实语义一致）
            return True
        return False

    def preflight(self):
        self.preflight_calls += 1
        if self.preflight_error is not None:
            raise self.preflight_error

    def ensure_interactive(self):
        self.interactive_checks += 1
        if self.desktop_name != "Default":
            raise ComputerControlError(
                "computer_secure_desktop",
                f"当前 input desktop 为 {self.desktop_name!r}，不是交互式 Default 桌面",
            )
        if not self.has_foreground:
            raise ComputerControlError(
                "computer_secure_desktop",
                "当前没有前台窗口，桌面不可操控",
            )

    def virtual_screen_rect(self):
        return self.virtual_rect

    def foreground_window_title(self):
        return self.foreground_title

    def foreground_window_id(self):
        return self.foreground_id

    def _emit(self):
        """模拟一个底层输入事件送达；超过 fail_after 后按 SendInput 部分失败处理。"""
        self._events_sent += 1
        if self.fail_after is not None and self._events_sent > self.fail_after:
            raise ComputerControlError(
                "computer_input_rejected",
                "fake SendInput 注入的部分失败",
            )

    def send_mouse(self, *, action, x=None, y=None, button="left", delta=0, path=(), modifiers=()):
        self.call_order.append(("send_mouse", action))
        self.mouse_calls.append((action, x, y, button, delta, tuple(path), tuple(modifiers)))
        for name in modifiers:
            self._emit()
            self.keys_down.add(name)
        try:
            if action == "move":
                self._emit()
                self.moves.append((x, y))
            elif action == "click":
                self._emit()
                self.clicks.append((x, y, button))
            elif action == "double_click":
                self._emit()
                self.double_clicks.append((x, y, button))
            elif action == "down":
                self._emit()
                self.buttons_down.add(button)
            elif action == "up":
                self._emit()
                self.buttons_down.discard(button)
            elif action == "wheel":
                self._emit()
                self.wheels.append(delta)
            elif action == "hwheel":
                self._emit()
                self.hwheels.append(delta)
            elif action == "drag":
                points = tuple(path)
                self.drags.append((points, button))
                self._emit()  # 移动到起点
                self._emit()  # 按下
                self.buttons_down.add(button)
                try:
                    for _point in points[1:]:
                        self._emit()  # 路径移动
                finally:
                    # try/finally 保证释放，与真实 adapter 的 drag 收尾语义一致。
                    self.buttons_down.discard(button)
            else:
                raise ComputerControlError(
                    "computer_invalid_action",
                    f"未知鼠标动作: {action!r}",
                )
        finally:
            for name in reversed(list(modifiers)):
                self.keys_down.discard(name)

    def send_text(self, text):
        self.call_order.append(("send_text", text))
        for _char in text:
            self._emit()  # Unicode down
            self._emit()  # Unicode up
        self.texts.append(text)

    def set_clipboard_text(self, text):
        # 2026-09-06：type_text 非 ASCII 剪贴板路径的记录点（提速计划 Task 4）。
        self.call_order.append(("set_clipboard", text))
        self.clipboard_writes.append(text)

    def send_keys(self, keys):
        keys = tuple(keys)
        self.call_order.append(("send_keys", keys))
        self.keys_sent.append(keys)
        try:
            for name in keys:
                self._emit()
                self.keys_down.add(name)
            for name in reversed(keys):
                self.keys_down.discard(name)
        finally:
            # 任何失败路径都必须清空本组合键的按下状态。
            self.keys_down.difference_update(keys)

    def release_all(self):
        self.release_all_calls += 1
        self.keys_down.clear()
        self.buttons_down.clear()


# ---------------- Task 8 导出：ComputerBackend 协议 fake ----------------


class FakeComputerBackend:
    """ComputerBackend 协议的纯 fake：绝不触碰真实键鼠、浏览器或截屏。

    - ``execute_calls`` 按序记录 ``(run_id, kind, target)``；``execute_log``
      按序记录 ``("enter"|"exit", kind)``，用于断言动作串行；
      ``concurrent_execute_violations`` 记录并发进入 execute 的次数；
    - ``screen_hashes``：注入的截图 hash 序列，observe 依次消费、用尽后重复
      最后一个，用于精确控制 no-progress 计数；``set_fingerprint()`` 切换
      活动指纹（browser 体现为 page_title，desktop 体现为 window_title）；
    - ``observe_error`` / ``execute_error`` / ``close_error`` 注入失败；
    - ``execute_gate``（threading.Event）：设置后 execute 阻塞等待，用于
      验证 controller 的 action_lock 串行语义；
    - ``release_count`` / ``close_count`` 计数，验证幂等停止只释放一次；
    - ``png_path``（Task 16）：观察携带的虚拟截图路径（不读真实文件），
      供 controller 预览发布等接线测试使用。
    """

    def __init__(self, target="browser", *, screen_hashes=(), fingerprint="fake-surface",
                 observe_error=None, execute_error=None, close_error=None,
                 execute_gate=None, png_path=""):
        self.target = target
        self.start_calls = 0
        self.execute_calls = []
        self.execute_log = []
        self.execute_thread_ids = []
        self.observe_calls = 0
        self.release_count = 0
        self.close_count = 0
        self.concurrent_execute_violations = 0
        self.observe_error = observe_error
        self.execute_error = execute_error
        self.close_error = close_error
        self.execute_gate = execute_gate
        self.png_path = png_path
        self.closed = False
        self.reusable = True
        self._screen_hashes = list(screen_hashes)
        self._last_hash = f"sha-{target}-0"
        self._fingerprint = fingerprint
        self._lock = threading.Lock()
        self._inside_execute = False

    def set_fingerprint(self, value):
        with self._lock:
            self._fingerprint = value

    def start(self):
        with self._lock:
            self.start_calls += 1

    def observe(self, run_id):
        from core.computer.models import ComputerObservation

        with self._lock:
            self.observe_calls += 1
            error = self.observe_error
            if error is None:
                if self._screen_hashes:
                    self._last_hash = self._screen_hashes.pop(0)
                sha = self._last_hash
                fingerprint = self._fingerprint
        if error is not None:
            raise error
        fields = {"run_id": run_id, "target": self.target, "action": "observe",
                  "screen_sha256": sha, "width": 1280, "height": 720,
                  "png_path": self.png_path}
        if self.target == "browser":
            fields["page_origin"] = "https://fake.example"
            fields["page_title"] = fingerprint
        else:
            fields["window_title"] = fingerprint
        return ComputerObservation(**fields)

    def execute(self, run_id, action):
        with self._lock:
            if self._inside_execute:
                self.concurrent_execute_violations += 1
            self._inside_execute = True
            self.execute_log.append(("enter", action.kind))
        try:
            with self._lock:
                self.execute_calls.append((run_id, action.kind, action.target))
                self.execute_thread_ids.append(threading.get_ident())
            if self.execute_gate is not None:
                self.execute_gate.wait(timeout=10)
            with self._lock:
                error = self.execute_error
            if error is not None:
                raise error
        finally:
            with self._lock:
                self._inside_execute = False
                self.execute_log.append(("exit", action.kind))

    def release_inputs(self):
        with self._lock:
            self.release_count += 1

    def close(self):
        with self._lock:
            self.close_count += 1
            self.closed = True
        if self.close_error is not None:
            raise self.close_error

    def is_reusable(self):
        """retained 领取裁决 fake（R4）：``reusable=False`` 模拟不健康实例。"""
        with self._lock:
            return self.reusable and not self.closed


# ---------------- Task 13a 导出：GUI Bridge 电脑操控 Controller fake ----------------


class FakeBridgeController:
    """ComputerControlService 的 GUI Bridge fake（Task 13a 事件桥与控制 API）。

    - ``add_listener``/``remove_listener`` 镜像真实 controller（listener 异常
      吞掉留痕）；``listener_count`` 暴露当前注册数，供“桥接建立一次”断言；
    - ``emit_state``/``emit_action_started``/``emit_action_finished``/
      ``emit_observation`` 按真实事件形状（type/run_id/event_seq + 各自字段）
      发给已注册 listener；``emit_observation`` 的 run_id/event_seq 可覆盖
      （Task 13a brief 样例逐字使用）；
    - ``start_run(run_id=...)`` 建立活动 run；pause/resume/stop 记录透传调用
      并发出 computer_state 事件；run_id 与当前活动 run 不匹配抛
      computer_invalid_state（旧 UI 事件的 run id 拒绝语义，镜像
      _require_active_run_locked）；``stop`` 接受 ``cancel_owner`` 关键字
      （默认 True）并记录到 ``stop_cancel_owner``，行为不变；
    - ``snapshot()`` 返回真实 ComputerRunSnapshot（终态保留 run_id，
      镜像 _last_snapshot 语义）；
    - 绝不触碰真实键鼠、浏览器或截屏。
    """

    def __init__(self):
        self._listeners = []
        self.run_id = ""
        self.status = "idle"
        self.action_count = 0
        self.pause_calls = []
        self.resume_calls = []
        self.stop_calls = []
        # 接管交还修复 L2：记录 stop 的 cancel_owner 关键字（默认 True），
        # 使该桩能接受调用方传入的新关键字而不 TypeError。
        self.stop_cancel_owner = []

    @property
    def listener_count(self):
        return len(self._listeners)

    def add_listener(self, listener):
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener):
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def _emit(self, event):
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass  # 镜像真实 controller：listener 异常只留痕不传播

    def emit_state(self, run_id="run-a", event_seq=1, status="running", reason=""):
        self._emit({
            "type": "computer_state",
            "run_id": run_id,
            "event_seq": event_seq,
            "status": status,
            "target": "browser",
            "reason": reason,
        })

    def emit_action_started(self, run_id="run-a", event_seq=1, action_ordinal=1,
                            kind="click"):
        self._emit({
            "type": "computer_action_started",
            "run_id": run_id,
            "event_seq": event_seq,
            "action_ordinal": action_ordinal,
            "action_kind": kind,
            "target": "browser",
        })

    def emit_action_finished(self, run_id="run-a", event_seq=1, action_ordinal=1,
                             kind="click", ok=True, error_code=""):
        self._emit({
            "type": "computer_action_finished",
            "run_id": run_id,
            "event_seq": event_seq,
            "action_ordinal": action_ordinal,
            "action_kind": kind,
            "ok": ok,
            "error_code": error_code,
        })

    def emit_observation(self, run_id="run-a", event_seq=1, action_ordinal=1,
                         screen_sha256="sha-fake-1",
                         preview_url="/computer-preview/tok/1.png"):
        self._emit({
            "type": "computer_observation",
            "run_id": run_id,
            "event_seq": event_seq,
            "action_ordinal": action_ordinal,
            "target": "browser",
            "screen_sha256": screen_sha256,
            "width": 1280,
            "height": 720,
            "origin_or_window": "https://fake.example",
            "preview_url": preview_url,
        })

    def start_run(self, run_id="run-a"):
        self.run_id = run_id
        self.status = "running"
        self.action_count = 0
        self.emit_state(run_id=run_id, event_seq=1, status="running", reason="start")
        return self.snapshot()

    def _require_active_run(self, run_id):
        if not self.run_id or run_id != self.run_id:
            raise ComputerControlError(
                "computer_invalid_state", f"未知的电脑操控 run：{run_id}"
            )

    def pause(self, run_id, reason):
        self._require_active_run(run_id)
        self.pause_calls.append((run_id, reason))
        self.status = "paused"
        self.emit_state(run_id=run_id, event_seq=2, status="paused", reason=reason)
        return self.snapshot()

    def resume(self, run_id):
        self._require_active_run(run_id)
        self.resume_calls.append(run_id)
        self.status = "running"
        self.emit_state(run_id=run_id, event_seq=3, status="running", reason="resume")
        return self.snapshot()

    def stop(self, run_id, reason, *, cancel_owner=True):
        # 接受 stop 的 cancel_owner 关键字（镜像真实 controller 签名）：该桩
        # 只记录、行为不变，避免调用方传新关键字时 TypeError。
        self._require_active_run(run_id)
        self.stop_calls.append((run_id, reason))
        self.stop_cancel_owner.append(cancel_owner)
        self.status = "stopped"
        self.emit_state(run_id=run_id, event_seq=4, status="stopped", reason=reason)
        return self.snapshot()

    def stop_for_user(self, run_id: str, reason: str = "user_stop"):
        # 镜像真实 controller：用户停止只结束 run，不置位回合 cancel_event。
        return self.stop(run_id, reason, cancel_owner=False)

    def snapshot(self):
        return ComputerRunSnapshot(
            run_id=self.run_id,
            status=self.status,
            target="browser" if self.run_id else "",
            action_count=self.action_count,
        )


# ---------------- Task 9 导出：固定适配器 Controller fake ----------------


class FakeController:
    """ComputerControlService 的最小 fake（Task 9 固定适配器测试）。

    - 固定 run_id/owner/capability；``execute_calls`` 按序记录 (run_id, kind, target)；
    - 未知 run_id 抛 computer_invalid_state、owner 不一致抛 computer_owner_mismatch
      （镜像真实 controller 的租约语义）；
    - ``observations`` 按序注入 ComputerObservation（用尽后用默认观察），
      默认观察的 png_path 取 ``png_path``（由测试用 tmp_path 写入虚拟字节）；
    - ``execute_errors`` 按序注入异常（用尽后正常执行）；
    - ``snapshot()`` 返回真实 ComputerRunSnapshot，action_count 随动作递增，
      供固定适配器读取动作 ordinal；
    - Task 10 补充最小 run 生命周期：``start_calls``/``start_error``、
      ``complete_calls``/``completed_run_ids``/``complete_error``、
      ``stop_calls``/``stop_reasons``/``stop_error``；生命周期方法只记录调用，
      ``run_status`` 随 complete/stop 变化供 Turn 收尾判定；
    - 接管交还修复 L1/L2 补充：``complete_run`` 记录 ``retain_browser_session``
      到 ``complete_retain_flags``，``stop`` 记录 ``cancel_owner`` 到
      ``stop_cancel_owner``（签名带默认值 True）；``execute`` 遇
      ``request_user_takeover`` 时把 ``run_status`` 置为 ``waiting_for_user``
      （镜像真实 controller 的接管交接），供「接管回合收尾」测试建模；
    - 绝不触碰真实键鼠、浏览器或截屏。
    """

    def __init__(self, *, owner, capability=GENERIC_CAPABILITY, run_id="run-a",
                 png_path="", observations=(), execute_errors=()):
        self.owner = owner
        self.capability = capability
        self.run_id = run_id
        self.png_path = png_path
        self.execute_calls = []
        self.action_count = 0
        self._observations = list(observations)
        self.execute_errors = list(execute_errors)
        # Task 10：Turn 接线的最小生命周期记录
        self.start_calls = 0
        self.start_targets = []  # Task 11a：记录 start_run 传入的 target
        self.start_error = None
        self.complete_calls = 0
        self.completed_run_ids = []
        self.complete_error = None
        self.stop_calls = 0
        self.stop_reasons = []
        self.stop_error = None
        self.run_status = "running"
        # 接管交还修复 L1/L2：记录每次 stop/complete_run 的关键关键字参数，
        # 供 Turn 收尾测试断言「兜底 stop 传 cancel_owner=False」「接管态收尾
        # 传 retain_browser_session=False」。
        self.stop_cancel_owner = []
        self.complete_retain_flags = []

    def start_run(self, owner, capability, target=None):
        self.start_calls += 1
        self.start_targets.append(target)
        if self.start_error is not None:
            raise self.start_error
        self.owner = owner
        self.capability = capability
        self.run_status = "running"
        return self.snapshot()

    def complete_run(self, run_id, owner, retain_browser_session=True):
        self.complete_calls += 1
        if self.complete_error is not None:
            raise self.complete_error
        self.completed_run_ids.append(run_id)
        self.complete_retain_flags.append(retain_browser_session)
        self.run_status = "completed"
        return self.snapshot()

    def stop(self, run_id, reason, *, cancel_owner=True):
        self.stop_calls += 1
        self.stop_reasons.append((run_id, reason))
        self.stop_cancel_owner.append(cancel_owner)
        if self.stop_error is not None:
            raise self.stop_error
        self.run_status = "stopped"
        return self.snapshot()

    def _default_observation(self, run_id, action):
        return ComputerObservation(
            run_id=run_id,
            target=action.target,
            screen_sha256=f"fake-sha-{self.action_count}",
            width=1280,
            height=720,
            page_origin="https://fake.example" if action.target == "browser" else "",
            window_title="" if action.target == "browser" else "Fake Window",
        )

    def execute(self, run_id, owner, action):
        if run_id != self.run_id:
            raise ComputerControlError(
                "computer_invalid_state", f"未知的电脑操控 run：{run_id}"
            )
        if owner != self.owner:
            raise ComputerControlError(
                "computer_owner_mismatch", "当前电脑操控 run 属于其他会话/Turn，拒绝操作"
            )
        self.action_count += 1
        self.execute_calls.append((run_id, action.kind, action.target))
        if self.execute_errors:
            raise self.execute_errors.pop(0)
        base = self._observations.pop(0) if self._observations else (
            self._default_observation(run_id, action)
        )
        if action.kind == "request_user_takeover":
            # 镜像真实 controller 的接管交接行为：request_user_takeover 成功
            # 执行后 run 转入 waiting_for_user，供「接管回合收尾」测试建模。
            self.run_status = "waiting_for_user"
        return replace(
            base,
            run_id=run_id,
            target=action.target,
            action=action.kind,
            png_path=base.png_path or self.png_path,
        )

    def snapshot(self):
        return ComputerRunSnapshot(
            run_id=self.run_id,
            owner_session_id=self.owner.session_id,
            owner_turn_id=self.owner.turn_id,
            status=self.run_status,
            action_count=self.action_count,
        )


# ---------------- R10 导出：语义定位 fake（绝不触碰真实窗口/UIA） ----------------


class FakeSurfaceLocator:
    """SurfaceLocator 协议的纯 fake：候选由测试配置，记录 activate/invoke 调用。

    - ``windows``/``tray_icons``：SurfaceCandidate 数据字典列表（label/bounds/
      operations）；每次 list_* 调用递增代数并重发候选（旧 ref 随即失效）；
    - ``activate_error``/``invoke_error``：注入失败；``activate_calls``/
      ``invoke_calls`` 按序记录 (ref, generation)；
    - ``bounds_override``：ref → 当前边界，模拟候选移动/消失（None）。
    """

    def __init__(self, *, windows=(), tray_icons=(), activate_error=None, invoke_error=None):
        from core.computer.surfaces import SurfaceCandidate

        self._window_specs = list(windows)
        self._tray_specs = list(tray_icons)
        self.activate_error = activate_error
        self.invoke_error = invoke_error
        self.activate_calls = []
        self.invoke_calls = []
        self.bounds_override = {}
        self.close_calls = 0
        self._generation = 0
        self._candidates = {}
        self._closed = False
        self._candidate_cls = SurfaceCandidate

    def _reissue(self, kind, specs):
        self._generation += 1
        self._candidates = {
            ref: c for ref, c in self._candidates.items() if c.kind != kind
        }
        issued = []
        for index, spec in enumerate(specs, start=1):
            ref = f"fake-{self._generation}-{kind}-{index}"
            candidate = self._candidate_cls(
                ref=ref,
                kind=kind,
                label=str(spec.get("label", "")),
                surface_generation=self._generation,
                supported_operations=tuple(spec.get("operations", ("activate", "click_point"))),
                bounds=tuple(spec.get("bounds", (0, 0, 32, 32))),
            )
            self._candidates[ref] = candidate
            issued.append(candidate)
        return tuple(issued)

    def _check_open(self):
        if self._closed:
            raise ComputerControlError("computer_surfaces_unavailable", "语义定位器已关闭")

    def list_windows(self):
        self._check_open()
        return self._reissue("window", self._window_specs)

    def list_tray_icons(self):
        self._check_open()
        return self._reissue("tray_icon", self._tray_specs)

    def _resolve(self, ref, generation):
        self._check_open()
        candidate = self._candidates.get(ref)
        if candidate is None or candidate.surface_generation != generation:
            raise ComputerControlError(
                "computer_surface_unknown", "语义定位候选不存在或观察代数已过期"
            )
        return candidate

    def resolve_candidate(self, ref, generation):
        candidate = self._candidates.get(ref)
        if candidate is None or candidate.surface_generation != generation:
            return None
        return candidate

    def current_candidate_bounds(self, ref, generation):
        candidate = self.resolve_candidate(ref, generation)
        if candidate is None:
            return None
        if ref in self.bounds_override:
            return self.bounds_override[ref]
        return candidate.bounds

    def activate_window(self, ref, generation):
        candidate = self._resolve(ref, generation)
        if "activate" not in candidate.supported_operations:
            raise ComputerControlError("computer_invalid_action", "候选不支持 activate")
        self.activate_calls.append((ref, generation))
        if self.activate_error is not None:
            raise self.activate_error

    def invoke_element(self, ref, generation):
        candidate = self._resolve(ref, generation)
        if "invoke" not in candidate.supported_operations:
            raise ComputerControlError("computer_invalid_action", "候选不支持 invoke")
        self.invoke_calls.append((ref, generation))
        if self.invoke_error is not None:
            raise self.invoke_error

    def close(self):
        self.close_calls += 1
        self._closed = True
        self._candidates = {}
