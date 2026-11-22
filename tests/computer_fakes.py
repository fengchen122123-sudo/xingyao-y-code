import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

import config
from core.computer.browser_backend import (
    ELEMENT_COLLECT_SCRIPT,
    ELEMENT_HIT_SCRIPT,
    PAGE_TEXT_SCRIPT,
)
from core.computer.models import (
    ComputerCapability,
    ComputerControlError,
    ComputerObservation,
    ComputerRunSnapshot,
)
from core.computer.settings import ComputerSettingsSnapshot


def use_temp_config(tmp_path, monkeypatch, values):
    cfg = tmp_path / "config.json"
    payload = values if values else {"active_model_id": None, "providers": []}
    cfg.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    use_temp_config._seq = getattr(use_temp_config, "_seq", 0) + 1
    stamp = time.time() + use_temp_config._seq
    os.utime(cfg, (stamp, stamp))
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(tmp_path / "config.toml"))
    return cfg


@pytest.fixture
def enabled_settings():
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


class FakeHotkeyApi:

    def __init__(self, register_result: bool = True, last_error_code: int = 0):
        self._register_result = register_result
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
    return b"\x89PNG\r\n\x1a\n" + b"fake" + int(seed).to_bytes(4, "big", signed=False)


def http_status(url: str) -> int:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


class FakeMouse:

    def __init__(self, runtime):
        self._runtime = runtime
        self.calls = []
        self.move_fail_after = None
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


class FakeElementHandle:

    def __init__(self, ref, *, attached=True, click_error=None, fill_error=None):
        self.ref = ref
        self.attached = attached
        self.click_calls = []
        self.fill_calls = []
        self.click_error = click_error
        self.fill_error = fill_error
        self.disposed = False

    def as_element(self):
        return self if self.attached else None

    def click(self, **kwargs):
        self.click_calls.append(dict(kwargs))
        if self.click_error is not None:
            raise self.click_error

    def fill(self, text, **kwargs):
        self.fill_calls.append((text, dict(kwargs)))
        if self.fill_error is not None:
            raise self.fill_error

    def dispose(self):
        self.disposed = True


class FakePage:

    def __init__(self, runtime, url="about:blank", title="", screenshot_bytes=None):
        from core.computer.browser_backend import (
            ELEMENT_COLLECT_SCRIPT,
            ELEMENT_HIT_SCRIPT,
            PAGE_TEXT_SCRIPT,
        )

        self._element_collect_script = ELEMENT_COLLECT_SCRIPT
        self._element_hit_script = ELEMENT_HIT_SCRIPT
        self._page_text_script = PAGE_TEXT_SCRIPT
        self._runtime = runtime
        self._context = None
        self.url = url
        self.page_title = title
        self.closed = False
        self.visibility_state = "visible"
        self.focused = True
        self.mouse = FakeMouse(runtime)
        self.keyboard = FakeKeyboard(runtime)
        self.goto_calls = []
        self.navigation_calls = []
        self.navigation_kwargs = []
        self.screenshot_calls = []
        self.evaluate_calls = []
        self.screenshot_bytes = screenshot_bytes
        self.screenshot_error = None
        self.screenshot_gate = None
        self.screenshot_delay = 0.0
        self.concurrency_violations = 0
        self._inside_screenshot = False
        self.element_collect_result = None
        self.element_collect_error = None
        self.element_collect_hook = None
        self.element_hit_result = None
        self.element_attached = True
        self.doc_token = "fake-doc-1"
        self.page_text = ""
        self.page_text_error = None
        self.locator_calls = []
        self.last_locator = None
        self.last_element_handle = None
        self.locator_click_error = None
        self.locator_fill_error = None
        self.locator_count = 1

    def is_closed(self):
        self._runtime._record_call("page.is_closed")
        return self.closed

    def evaluate(self, expression, *args, **kwargs):
        from core.computer.browser_backend import (
            ELEMENT_COLLECT_SCRIPT,
            ELEMENT_HIT_SCRIPT,
            ELEMENT_WAIT_PROBE_SCRIPT,
            PAGE_TEXT_SCRIPT,
        )

        self._runtime._record_call("page.evaluate")
        self.evaluate_calls.append(expression)
        if expression == ELEMENT_COLLECT_SCRIPT:
            if self.element_collect_error is not None:
                raise self.element_collect_error
            result = self.element_collect_result
            if self.element_collect_hook is not None:
                self.element_collect_hook(self)
            return [] if result is None else result
        if expression == ELEMENT_HIT_SCRIPT:
            return self.element_hit_result
        if expression == ELEMENT_WAIT_PROBE_SCRIPT:
            return getattr(
                self,
                "element_wait_probe_result",
                {"stale": False, "exists": True, "enabled": True},
            )
        if expression == PAGE_TEXT_SCRIPT:
            if self.page_text_error is not None:
                raise self.page_text_error
            return self.page_text
        if "docToken" in str(expression):
            return self.doc_token
        if "visibilityState" in str(expression) and "hasFocus" in str(expression):
            return [self.visibility_state, self.focused]
        raise AssertionError(
            f"fake page 只允许可见性探针、元素收集与正文提取脚本，拒绝 evaluate: {expression!r}"
        )

    def evaluate_handle(self, expression, arg=None):
        self._runtime._record_call("page.evaluate_handle")
        ref = arg.get("id") if isinstance(arg, dict) else arg
        handle = FakeElementHandle(
            ref,
            attached=self.element_attached and (self.locator_count > 0),
            click_error=self.locator_click_error,
            fill_error=self.locator_fill_error,
        )
        self.last_element_handle = handle
        return handle

    def title(self):
        self._runtime._record_call("page.title")
        return self.page_title

    def locator(self, selector):
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


class FakeImage:

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

    def __init__(self, *, virtual_rect=(-1920, 0, 3840, 2160), desktop_name="Default",
                 has_foreground=True, foreground_title="Fake Window", foreground_id=1001,
                 preflight_error=None, fail_after=None):
        self.virtual_rect = virtual_rect
        self.desktop_name = desktop_name
        self.has_foreground = has_foreground
        self.foreground_title = foreground_title
        self.foreground_id = foreground_id
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
        self.window_from_point_calls = []
        self.set_foreground_calls = []
        self.call_order = []
        self.window_at_point_id = None
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
            self.foreground_id = hwnd
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
                self._emit()
                self._emit()
                self.buttons_down.add(button)
                try:
                    for _point in points[1:]:
                        self._emit()
                finally:
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
            self._emit()
            self._emit()
        self.texts.append(text)

    def set_clipboard_text(self, text):
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
            self.keys_down.difference_update(keys)

    def release_all(self):
        self.release_all_calls += 1
        self.keys_down.clear()
        self.buttons_down.clear()


class FakeComputerBackend:

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
        with self._lock:
            return self.reusable and not self.closed


class FakeBridgeController:

    def __init__(self):
        self._listeners = []
        self.run_id = ""
        self.status = "idle"
        self.action_count = 0
        self.pause_calls = []
        self.resume_calls = []
        self.stop_calls = []
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
                pass

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
        self._require_active_run(run_id)
        self.stop_calls.append((run_id, reason))
        self.stop_cancel_owner.append(cancel_owner)
        self.status = "stopped"
        self.emit_state(run_id=run_id, event_seq=4, status="stopped", reason=reason)
        return self.snapshot()

    def stop_for_user(self, run_id: str, reason: str = "user_stop"):
        return self.stop(run_id, reason, cancel_owner=False)

    def snapshot(self):
        return ComputerRunSnapshot(
            run_id=self.run_id,
            status=self.status,
            target="browser" if self.run_id else "",
            action_count=self.action_count,
        )


class FakeController:

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
        self.start_calls = 0
        self.start_targets = []
        self.start_error = None
        self.complete_calls = 0
        self.completed_run_ids = []
        self.complete_error = None
        self.stop_calls = 0
        self.stop_reasons = []
        self.stop_error = None
        self.run_status = "running"
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


class FakeSurfaceLocator:

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
