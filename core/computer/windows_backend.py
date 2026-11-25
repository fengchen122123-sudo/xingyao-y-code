from __future__ import annotations

import hashlib
import io
import logging
import shutil
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from core.computer.actions import (
    MAX_WAIT_MS,
    MIN_WAIT_MS,
    WAIT_FOR_DEFAULT_INTERVAL_MS,
    WAIT_FOR_DEFAULT_TIMEOUT_MS,
)
from core.computer.models import (
    ComputerAction,
    ComputerControlError,
    ComputerObservation,
    ObservationFrame,
)
from core.computer.frames import coordinate_disabled_error
from core.computer.platforms.windows_elements import _call_with_supported_kwargs

try:
    from PIL import Image

    _RESAMPLE = Image.Resampling.LANCZOS
except ImportError:  # pragma: no cover - 依赖缺失时 preflight 明确失败
    Image = None
    _RESAMPLE = None

_LOGGER = logging.getLogger(__name__)

_MAX_MODEL_EDGE = 1920
_MAX_TITLE_CHARS = 200
_KEEP_SCREENSHOTS = 3
_CLIPBOARD_PASTE_DELAY_S = 0.05
_ACTIVATION_SETTLE_S = 0.15
_BROWSER_ONLY_KINDS = frozenset({"navigate", "back", "forward", "reload"})
_ELEMENT_KINDS = frozenset({"list_elements", "click_element", "set_value_element"})
# 自动补表的元素上限（与 WindowsElementLocator.MAX_ELEMENTS 对齐）：超大树
# 只截前 N 个，避免把一次观察拖成整树遍历后的超长上下文。
_MAX_OBSERVE_ELEMENTS = 200
_SEMANTIC_DESKTOP_KINDS = frozenset(
    {"list_surfaces", "activate_surface", "invoke_surface", "observe_region"}
)
# 键盘类动作：没有目标点，发送前必须复核前台窗口仍是最近一次点击的目标窗口。
_KEYBOARD_KINDS = frozenset({"type_text", "keypress"})


def default_image_grab():
    """生产截图入口：方法内惰性导入 PIL.ImageGrab，捕获整个虚拟桌面。"""
    from PIL import ImageGrab

    return ImageGrab.grab(all_screens=True)


def screenshot_to_physical(
    x: int, y: int, observation: ComputerObservation
) -> tuple[int, int]:
    if type(x) is not int or type(y) is not int:
        raise ComputerControlError(
            "computer_invalid_action",
            "坐标 x/y 必须是整数（bool 不透传）",
        )
    if observation.width <= 0 or observation.height <= 0:
        raise ComputerControlError(
            "computer_invalid_action",
            "观察缺少有效的模型宽高，无法换算坐标",
        )
    if not (0 <= x < observation.width and 0 <= y < observation.height):
        raise ComputerControlError(
            "computer_coordinate_out_of_bounds",
            f"截图坐标 ({x}, {y}) 超出观察画面 "
            f"{observation.width}x{observation.height} 半开区间",
        )
    physical_x = observation.origin_x + round(
        x * observation.original_width / observation.width
    )
    physical_y = observation.origin_y + round(
        y * observation.original_height / observation.height
    )
    if not (
        observation.origin_x
        <= physical_x
        < observation.origin_x + observation.original_width
        and observation.origin_y
        <= physical_y
        < observation.origin_y + observation.original_height
    ):
        raise ComputerControlError(
            "computer_coordinate_out_of_bounds",
            f"物理坐标 ({physical_x}, {physical_y}) 超出虚拟桌面半开区间",
        )
    return physical_x, physical_y


def _scaled_dimensions(width: int, height: int) -> tuple[int, int]:
    """最长边大于 1920 时按同一比例等比缩小；否则保持原尺寸。"""
    longest = max(width, height)
    if longest <= _MAX_MODEL_EDGE:
        return width, height
    ratio = _MAX_MODEL_EDGE / longest
    return max(1, round(width * ratio)), max(1, round(height * ratio))


_CAPTION_NAMES = frozenset(
    {
        "关闭",
        "系统",
        "最小化",
        "最大化",
        "还原",
        "向下还原",
        "close",
        "system",
        "minimize",
        "maximize",
        "restore",
    }
)
_SELF_DRAWN_NOTE = (
    "此窗口是自绘界面，控件树只有标题栏按钮。请依据截图坐标点击，"
    "坐标以这张截图左上角为 (0,0)。铺满窗口的标题栏按钮已从可点击列表移除。"
)
_MIN_CROP_EDGE = 64


def _is_caption_name(name: str) -> bool:
    return str(name or "").strip().casefold() in _CAPTION_NAMES


def _present_desktop_elements(elements, left: int, top: int, width: int, height: int):
    """去掉铺满窗口的标题栏按钮，以及中心落在裁剪画面外的控件。"""
    area = max(0, int(width)) * max(0, int(height))
    right = int(left) + int(width)
    bottom = int(top) + int(height)
    kept = []
    for element in elements or ():
        if not isinstance(element, dict):
            kept.append(element)
            continue
        bounds = tuple(element.get("bounds") or ())
        if len(bounds) == 4:
            try:
                el, et, er, eb = (int(value) for value in bounds)
            except (TypeError, ValueError):
                kept.append(element)
                continue
            element_width, element_height = er - el, eb - et
            if (
                element_width > 0
                and element_height > 0
                and area > 0
                and _is_caption_name(str(element.get("name") or ""))
                and element_width * element_height >= int(area * 0.4)
            ):
                continue
            center_x = (el + er) / 2
            center_y = (et + eb) / 2
            if not (int(left) <= center_x < right and int(top) <= center_y < bottom):
                continue
        kept.append(element)
    only_caption = bool(elements) and all(
        isinstance(item, dict) and _is_caption_name(str(item.get("name") or ""))
        for item in elements
    )
    return tuple(kept), (_SELF_DRAWN_NOTE if only_caption else "")


def _crop_image_to_window(image, virtual, window_bounds):
    """把整张虚拟桌面裁到前台窗口与虚拟桌面的交集。铺满虚拟桌面时不裁。"""
    origin_x, origin_y, width, height = (int(value) for value in virtual)
    left, top, right, bottom = (int(value) for value in window_bounds)
    crop_left = max(left, origin_x)
    crop_top = max(top, origin_y)
    crop_right = min(right, origin_x + width)
    crop_bottom = min(bottom, origin_y + height)
    if (
        crop_right - crop_left < _MIN_CROP_EDGE
        or crop_bottom - crop_top < _MIN_CROP_EDGE
    ):
        return None
    if (
        crop_left == origin_x
        and crop_top == origin_y
        and crop_right == origin_x + width
        and crop_bottom == origin_y + height
    ):
        return None
    crop = getattr(image, "crop", None)
    if not callable(crop):
        return None
    box = (
        crop_left - origin_x,
        crop_top - origin_y,
        crop_right - origin_x,
        crop_bottom - origin_y,
    )
    return crop(box), (
        crop_left,
        crop_top,
        crop_right - crop_left,
        crop_bottom - crop_top,
    )


class WindowsComputerBackend:
    """Windows 桌面 backend（target="desktop"）；线程安全，关闭幂等。"""

    target = "desktop"

    def __init__(
        self,
        image_grab: Callable,
        win32_adapter,
        *,
        screenshot_dir: Path | None = None,
        surface_locator=None,
        element_locator=None,
    ) -> None:
        # 构造函数只校验参数，绝不截屏或触碰 Win32。
        if not callable(image_grab):
            raise ComputerControlError(
                "computer_invalid_config", "image_grab 必须是可调用对象"
            )
        if win32_adapter is None:
            raise ComputerControlError(
                "computer_invalid_config", "win32_adapter 不能为空"
            )
        self._image_grab = image_grab
        self._adapter = win32_adapter
        self._surface_locator = surface_locator
        self._element_locator = element_locator
        self._screenshot_dir = Path(screenshot_dir) if screenshot_dir is not None else None
        self._owns_screenshot_dir = screenshot_dir is None
        self._screenshot_counter = 0
        self._lock = threading.Lock()
        self._started = False
        self._closed = False
        # R8：backend 停止事件；显式 wait 用 Event.wait 分片实现可中断等待，
        # 绝不持大锁睡满整个等待时长让 close 无法收尾。
        self._stop_event = threading.Event()
        self._last_observation: ComputerObservation | None = None
        self._pending_surface_candidates: tuple = ()
        self._pending_elements: tuple = ()
        self._last_elements: tuple = ()
        self._elements_surface_generation: int = -1
        self._elements_dirty: bool = False
        self._pending_region: tuple | None = None
        self._region_baseline: tuple | None = None
        self._surface_signature: tuple | None = None
        self._surface_generation = 0
        self._frame_seq = 0
        self._frames: dict[str, ObservationFrame] = {}
        self._active_frame: ObservationFrame | None = None
        self._last_activation_changed = False
        self._last_pointer_target_hwnd: int | None = None
        self._in_sequence = False
        self._sequence_keyboard_may_switch = False

    @classmethod
    def create_default(cls) -> "WindowsComputerBackend":
        """生产构造：真实 ImageGrab + 真实 Win32InputAdapter + 语义/元素定位器（惰性导入）。"""
        from core.computer.win32_adapter import Win32InputAdapter
        from core.computer.platforms.windows_surfaces import WindowsSurfaceLocator
        from core.computer.platforms.windows_elements import WindowsElementLocator

        return cls(
            image_grab=default_image_grab,
            win32_adapter=Win32InputAdapter(),
            surface_locator=WindowsSurfaceLocator(),
            element_locator=WindowsElementLocator(),
        )

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def _stale_capture_observation_locked(self, run_id: str) -> ComputerObservation:
        if self._last_elements and self._element_locator is not None:
            try:
                self._element_locator.invalidate()
            except Exception:
                _LOGGER.exception("失效旧元素引用失败")
        self._surface_generation += 1
        self._surface_signature = None
        self._last_observation = None
        self._last_elements = ()
        self._elements_surface_generation = -1
        self._region_baseline = None
        self._last_pointer_target_hwnd = None
        return ComputerObservation(
            run_id=run_id,
            target="desktop",
            action="observe",
            ok=False,
            error_code="computer_surface_changed",
            error_message="截屏或控件采集期间前景窗口或桌面几何变化，当前观察不可用于坐标输入，请重新 observe",
            width=0,
            height=0,
            original_width=0,
            original_height=0,
            surface_id="",
            surface_generation=self._surface_generation,
            elements=(),
        )

    def _try_crop_foreground_locked(self, image, virtual_rect):
        """前台窗口没铺满虚拟桌面时，把截图裁到该窗口。失败则保持整屏。"""
        getter = getattr(self._adapter, "foreground_window_rect", None)
        if not callable(getter):
            return None
        try:
            bounds = getter()
        except Exception:
            _LOGGER.exception("读取前台窗口矩形失败")
            return None
        if not bounds or len(tuple(bounds)) != 4:
            return None
        try:
            return _crop_image_to_window(image, virtual_rect, bounds)
        except Exception:
            _LOGGER.exception("裁剪前台窗口失败，观察退回整张虚拟桌面")
            return None

    def observe(self, run_id: str) -> ComputerObservation:
        with self._lock:
            self._ensure_started_locked()
            self._adapter.ensure_interactive()
            observation = self._capture_desktop_observation_locked(run_id)
            if observation.ok or observation.error_code != "computer_surface_changed":
                return observation
            # 窗口切换（如 Alt+Tab / Alt+Esc）动画尚未落稳时短暂让出时间再复拍
            self._stop_event.wait(0.08)
            return self._capture_desktop_observation_locked(run_id)

    def _capture_desktop_observation_locked(self, run_id: str) -> ComputerObservation:
        """调用方持有锁。抓取期间前台或虚拟桌面变化时返回失效观察，不在这里重试。"""
        before_hwnd = int(self._adapter.foreground_window_id() or 0)
        (origin_x, origin_y, original_width, original_height), image = (
            self._grab_checked_locked()
        )
        virtual_rect = (origin_x, origin_y, original_width, original_height)
        after_grab_hwnd = int(self._adapter.foreground_window_id() or 0)
        after_grab_rect = self._adapter.virtual_screen_rect()
        if (after_grab_hwnd, after_grab_rect) != (before_hwnd, virtual_rect):
            return self._stale_capture_observation_locked(run_id)
        pending_candidates = self._pending_surface_candidates
        self._pending_surface_candidates = ()
        pending_elements = self._pending_elements
        self._pending_elements = ()
        pending_region = self._pending_region
        self._pending_region = None
        effective_elements = pending_elements
        signature = (before_hwnd, *virtual_rect)
        if signature != self._surface_signature:
            self._surface_generation += 1
            self._surface_signature = signature
        # 观察重建焦点基线：清空 pointer 目标参照，键盘护栏回落到本次前台，
        # 避免「先点 B、再观察 C」时拿旧点击目标误判焦点丢失。
        self._last_pointer_target_hwnd = None
        if pending_region is not None:
            observation = self._build_region_observation_locked(
                run_id, image, (origin_x, origin_y), pending_region
            )
            if pending_elements:
                self._last_elements = pending_elements
                self._elements_surface_generation = self._surface_generation
        else:
            cropped = self._try_crop_foreground_locked(image, virtual_rect)
            if cropped is not None:
                image, (origin_x, origin_y, original_width, original_height) = cropped
            model_width, model_height = _scaled_dimensions(
                original_width, original_height
            )
            if (model_width, model_height) != (original_width, original_height):
                image = image.resize((model_width, model_height), _RESAMPLE)
            png = self._encode_png(image)
            elements = pending_elements
            if elements:
                self._last_elements = elements
                self._elements_surface_generation = self._surface_generation
                self._elements_dirty = False
            elif (
                not self._elements_dirty
                and self._last_elements
                and self._elements_surface_generation == self._surface_generation
            ):
                elements = self._last_elements
            else:
                elements = ()
                if self._last_elements:
                    self._last_elements = ()
                    self._elements_surface_generation = -1
                    if self._element_locator is not None:
                        try:
                            self._element_locator.invalidate()
                        except Exception:
                            _LOGGER.exception("失效旧元素引用失败")
                elements = self._collect_elements_locked(before_hwnd)
                after_collect_hwnd = int(self._adapter.foreground_window_id() or 0)
                after_collect_rect = self._adapter.virtual_screen_rect()
                if (after_collect_hwnd, after_collect_rect) != (before_hwnd, virtual_rect):
                    self._pending_surface_candidates = pending_candidates
                    self._pending_elements = pending_elements
                    self._pending_region = pending_region
                    return self._stale_capture_observation_locked(run_id)
                self._elements_dirty = False
                if elements:
                    self._last_elements = elements
                    self._elements_surface_generation = self._surface_generation
            elements, ui_note = _present_desktop_elements(
                elements, origin_x, origin_y, original_width, original_height
            )
            self._last_elements = elements
            if elements:
                self._elements_surface_generation = self._surface_generation
            effective_elements = elements
            if elements:
                png = self._annotate_elements_locked(
                    png,
                    elements,
                    original_width / model_width,
                    original_height / model_height,
                    origin_x,
                    origin_y,
                )
            path = self._store_screenshot_locked(png)
            observation = ComputerObservation(
                run_id=run_id,
                target="desktop",
                action="observe",
                ok=True,
                screen_sha256=hashlib.sha256(png).hexdigest(),
                png_path=str(path),
                width=model_width,
                height=model_height,
                original_width=original_width,
                original_height=original_height,
                origin_x=origin_x,
                origin_y=origin_y,
                scale_x=original_width / model_width,
                scale_y=original_height / model_height,
                window_title=self._window_title_summary(),
                ui_note=ui_note,
            )
            self._region_baseline = None
        observation = replace(
            observation,
            surface_id=f"hwnd:{signature[0]}",
            surface_generation=self._surface_generation,
            surface_candidates=pending_candidates,
            elements=effective_elements,
        )
        if pending_region is not None:
            self._region_baseline = pending_region
        self._frame_seq += 1
        frame_id = f"desktop-{self._surface_generation}-{self._frame_seq}"
        observation = replace(
            observation,
            frame_id=frame_id,
            viewport_generation=self._surface_generation,
            input_activated=self._last_activation_changed,
        )
        frame = ObservationFrame(
            frame_id=frame_id,
            run_id=run_id,
            target="desktop",
            surface_id=observation.surface_id,
            surface_generation=observation.surface_generation,
            viewport_generation=observation.viewport_generation,
            origin_x=observation.origin_x,
            origin_y=observation.origin_y,
            crop_width=observation.original_width or observation.width,
            crop_height=observation.original_height or observation.height,
            image_width=observation.width,
            image_height=observation.height,
            model_to_css_x=float(observation.scale_x or 1.0),
            model_to_css_y=float(observation.scale_y or 1.0),
            coordinate_input_enabled=True,
            disabled_reason="",
        )
        self._frames[frame_id] = frame
        while len(self._frames) > 32:
            self._frames.pop(next(iter(self._frames)))
        self._active_frame = frame
        self._last_activation_changed = False
        self._last_observation = observation
        return observation

    def sync_observation_frame(self, frame: ObservationFrame) -> None:
        """同步 Provider 重缩放逆变换或超限禁用状态到桌面当前帧。"""
        with self._lock:
            if not frame.frame_id:
                return
            self._frames[frame.frame_id] = frame
            while len(self._frames) > 32:
                self._frames.pop(next(iter(self._frames)))
            if self._active_frame is not None and self._active_frame.frame_id == frame.frame_id:
                self._active_frame = frame
                if (
                    frame.coordinate_input_enabled
                    and self._last_observation is not None
                    and frame.image_width > 0
                    and frame.image_height > 0
                ):
                    self._last_observation = replace(
                        self._last_observation,
                        width=frame.image_width,
                        height=frame.image_height,
                        scale_x=frame.model_to_css_x,
                        scale_y=frame.model_to_css_y,
                    )

    def begin_sequence(self, run_id: str) -> None:
        with self._lock:
            self._in_sequence = True
            self._sequence_keyboard_may_switch = False

    def end_sequence(self, run_id: str) -> None:
        with self._lock:
            self._in_sequence = False
            self._sequence_keyboard_may_switch = False

    def execute(self, run_id: str, action: ComputerAction) -> None:
        wait_duration: int | None = None
        wait_for_action: ComputerAction | None = None
        with self._lock:
            self._ensure_started_locked()
            self._raise_if_stopped_locked()
            if action.target != "desktop":
                raise ComputerControlError(
                    "computer_invalid_action",
                    f"桌面 backend 只执行 desktop target，收到 {action.target!r}",
                )
            kind = action.kind
            if kind == "observe":
                return
            if kind == "request_user_takeover":
                raise ComputerControlError(
                    "computer_invalid_action",
                    "request_user_takeover 由 controller 处理，不进入 backend",
                )
            if kind in _BROWSER_ONLY_KINDS:
                raise ComputerControlError(
                    "computer_invalid_action",
                    f"桌面 backend 不支持动作：{kind!r}",
                )
            self._adapter.ensure_interactive()
            self._last_activation_changed = False
            if kind == "wait":
                duration = action.duration_ms
                if type(duration) is not int or not MIN_WAIT_MS <= duration <= MAX_WAIT_MS:
                    raise ComputerControlError(
                        "computer_invalid_action",
                        f"duration_ms 必须在 {MIN_WAIT_MS}-{MAX_WAIT_MS} 毫秒之间",
                    )
                self._elements_dirty = True
                wait_duration = duration
            elif kind == "wait_for":
                # 只读条件等待：不改变界面状态，不需要画面基线（不要求先 observe），
                # 但每次动作前的 interactive desktop 检查已在上面做过。
                self._elements_dirty = True
                wait_for_action = action
            elif kind in _ELEMENT_KINDS:
                self._dispatch_element_locked(action)
                return
            elif kind in _SEMANTIC_DESKTOP_KINDS:
                self._dispatch_semantic_locked(action)
                return
            elif kind in _KEYBOARD_KINDS:
                self._require_keyboard_surface_and_focus_locked()
                self._dispatch_locked(action)
                self._elements_dirty = True
                if self._in_sequence and kind == "keypress":
                    self._sequence_keyboard_may_switch = True
                    if any(k in ("ALT", "META") for k in (action.keys or ())):
                        self._last_pointer_target_hwnd = None
                return
            else:
                self._require_current_surface_locked()
                self._dispatch_locked(action)
                self._elements_dirty = True
                return
        if wait_for_action is not None:
            self._wait_for_interruptible(
                int(self._adapter.foreground_window_id()), wait_for_action
            )
        elif wait_duration is not None:
            self._wait_interruptible(wait_duration)

    def _dispatch_element_locked(self, action: ComputerAction) -> None:
        """元素动作分发（调用方持有锁）：list_elements 纯观察不要求画面基线；
        click_element/set_value_element 与普通输入动作一样要求当前画面基线（R5）。"""
        locator = self._element_locator
        if locator is None:
            raise ComputerControlError(
                "computer_elements_unavailable",
                "当前桌面 backend 未提供 UIA 元素定位能力",
            )
        kind = action.kind
        if kind == "list_elements":
            hwnd = int(self._adapter.foreground_window_id() or 0)
            collected = tuple(locator.collect(hwnd))
            if int(self._adapter.foreground_window_id() or 0) != hwnd:
                try:
                    locator.invalidate()
                except Exception:
                    pass
                raise ComputerControlError(
                    "computer_surface_changed",
                    "控件采集期间前台窗口已切换，请重新 observe 后再操作",
                )
            self._pending_elements = collected
            self._elements_surface_generation = self._surface_generation
            self._elements_dirty = False
            return
        self._require_current_surface_locked()
        self._raise_if_stopped_locked()
        expected_hwnd = (
            int(self._surface_signature[0])
            if self._surface_signature is not None
            else None
        )
        if kind == "click_element":
            _call_with_supported_kwargs(
                locator.click, action.element_ref, expected_hwnd=expected_hwnd
            )
            self._elements_dirty = True
            return
        if not isinstance(action.text, str) or not action.text:
            raise ComputerControlError(
                "computer_invalid_action", "set_value_element 需要非空 text"
            )
        _call_with_supported_kwargs(
            locator.set_value,
            action.element_ref,
            action.text,
            expected_hwnd=expected_hwnd,
        )
        self._elements_dirty = True

    def _collect_elements_locked(self, hwnd: int | None = None) -> tuple:
        locator = self._element_locator
        if locator is None:
            return ()
        try:
            target_hwnd = (
                int(hwnd)
                if hwnd is not None
                else int(self._adapter.foreground_window_id() or 0)
            )
            listing = tuple(
                _call_with_supported_kwargs(
                    locator.collect,
                    target_hwnd,
                    deadline=time.monotonic() + 3.0,
                    cancel_event=self._stop_event,
                )
            )
        except Exception:
            _LOGGER.exception("自动提取前台窗口元素失败，观察退回普通截图")
            return ()
        return listing[:_MAX_OBSERVE_ELEMENTS]

    def _ensure_target_window_active_locked(self, point: tuple[int, int]) -> bool:
        px, py = int(point[0]), int(point[1])
        try:
            hwnd = int(self._adapter.window_from_point(px, py) or 0)
            if hwnd <= 0:
                raise ComputerControlError(
                    "computer_surface_ambiguous",
                    "无法确定坐标命中的目标窗口，请重新 observe 核对界面后再操作",
                    {"point": [px, py]},
                )
            self._last_pointer_target_hwnd = hwnd
            if hwnd == int(self._adapter.foreground_window_id() or 0):
                return False
            if not self._adapter.set_foreground_window(hwnd):
                raise ComputerControlError(
                    "computer_foreground_activation_failed",
                    "目标窗口未能置前，已在发送输入前中止，避免输入落到其他窗口。请先调用 activate_surface 激活目标窗口，或重新 observe 建立基线后再试。",
                    {"hwnd": hwnd, "point": [px, py]},
                )
            if self._stop_event.wait(_ACTIVATION_SETTLE_S):
                raise ComputerControlError(
                    "computer_cancelled", "桌面操控已停止，取消前置激活后的输入"
                )
            actual_fg = int(self._adapter.foreground_window_id() or 0)
            if actual_fg != hwnd:
                raise ComputerControlError(
                    "computer_foreground_activation_failed",
                    f"前置激活后实际前台窗口（hwnd {actual_fg}）与目标（hwnd {hwnd}）不符，已在发送输入前中止。",
                    {"hwnd": hwnd, "actual_hwnd": actual_fg, "point": [px, py]},
                )
            post_hwnd = int(self._adapter.window_from_point(px, py) or 0)
            if post_hwnd != hwnd:
                raise ComputerControlError(
                    "computer_surface_changed",
                    "激活目标窗口后坐标位置命中窗口已发生变化，请重新 observe 后再操作",
                    {"expected_hwnd": hwnd, "actual_hwnd": post_hwnd, "point": [px, py]},
                )
            return True
        except ComputerControlError:
            raise
        except Exception as exc:
            _LOGGER.exception("输入前置激活失败")
            raise ComputerControlError(
                "computer_foreground_activation_failed",
                "激活目标窗口时出错，已在发送输入前中止，避免输入落到错误窗口。请重新 observe 建立基线后再试。",
                {"point": [px, py], "reason": str(exc)},
            ) from exc

    def _annotate_elements_locked(
        self,
        png: bytes,
        elements: tuple,
        scale_x: float,
        scale_y: float,
        origin_x: int,
        origin_y: int,
    ) -> bytes:
        from core.computer.som_annotator import annotate_screenshot

        try:
            return annotate_screenshot(
                png,
                elements,
                scale_x,
                scale_y,
                origin_x=float(origin_x),
                origin_y=float(origin_y),
            )
        except Exception:
            _LOGGER.exception("SoM 编号标注失败，观察退回普通截图")
            return png

    def _dispatch_semantic_locked(self, action: ComputerAction) -> None:
        self._raise_if_stopped_locked()
        locator = self._surface_locator
        if locator is None:
            raise ComputerControlError(
                "computer_surfaces_unavailable",
                "当前桌面 backend 未提供语义定位能力",
            )
        kind = action.kind
        if kind == "list_surfaces":
            # 枚举经候选落进紧随其后的观察；多候选原样返回，不自动选第一个。
            # 窗口列表与托盘图标是两条独立路径：窗口走纯 Win32 枚举（快且稳），
            # 托盘走 UIA 遍历（依赖缺失/调用超时会失败）。两者不得绑成一个原子
            # 动作——托盘失败只降级为「托盘候选为空」，已经枚举到的窗口候选必须
            # 照常返回，否则用户拿不到任何候选、无法用 activate_surface 调前台。
            windows = locator.list_windows()
            try:
                tray = locator.list_tray_icons()
            except Exception as error:
                # 降级可诊断：带失败原因留痕，绝不静默吞错。
                _LOGGER.warning(
                    "托盘图标枚举失败，list_surfaces 降级为仅窗口候选：%s: %s",
                    type(error).__name__,
                    error,
                    exc_info=True,
                )
                tray = ()
            self._pending_surface_candidates = tuple(
                {
                    "ref": candidate.ref,
                    "kind": candidate.kind,
                    "label": candidate.label,
                    "bounds": candidate.bounds,
                    "operations": candidate.supported_operations,
                    "surface_generation": candidate.surface_generation,
                }
                for candidate in (*windows, *tray)
            )
            return
        if kind == "activate_surface":
            locator.activate_window(action.surface_ref, action.surface_generation)
            # 激活/恢复窗口后旧观察坐标与元素引用立即失效，必须重新 observe 建立新基线
            self._last_observation = None
            self._active_frame = None
            self._last_elements = ()
            self._elements_surface_generation = -1
            self._region_baseline = None
            self._surface_signature = None
            self._last_pointer_target_hwnd = None
            if self._element_locator is not None:
                try:
                    self._element_locator.invalidate()
                except Exception:
                    _LOGGER.exception("激活窗口后失效旧元素引用失败")
            return
        if kind == "invoke_surface":
            # 按候选实际支持的语义调用（UIA Invoke）；失败显式报错，
            # 绝不自动退化为同位置盲点（那是模型的显式 click 选择）。
            locator.invoke_element(action.surface_ref, action.surface_generation)
            return
        candidate = locator.resolve_candidate(action.surface_ref, action.surface_generation)
        if candidate is None:
            raise ComputerControlError(
                "computer_surface_unknown",
                "语义定位候选不存在或观察代数已过期，请重新 list_surfaces 观察",
            )
        bounds = locator.current_candidate_bounds(
            action.surface_ref, action.surface_generation
        )
        if bounds is None:
            raise ComputerControlError(
                "computer_surface_unknown", "语义定位候选已消失，请重新 list_surfaces 观察"
            )
        if bounds[2] <= 0 or bounds[3] <= 0:
            raise ComputerControlError(
                "computer_invalid_action", "候选区域尺寸无效，无法局部观察"
            )
        self._pending_region = (action.surface_ref, action.surface_generation, tuple(bounds))

    def _build_region_observation_locked(
        self, run_id: str, image, virtual_origin: tuple[int, int], region_info: tuple
    ) -> ComputerObservation:
        ref, generation, (region_x, region_y, region_w, region_h) = region_info
        virtual_x, virtual_y = virtual_origin
        desktop_w, desktop_h = image.size
        crop_left = max(region_x, virtual_x)
        crop_top = max(region_y, virtual_y)
        crop_right = min(region_x + region_w, virtual_x + desktop_w)
        crop_bottom = min(region_y + region_h, virtual_y + desktop_h)
        if crop_right <= crop_left or crop_bottom <= crop_top:
            raise ComputerControlError(
                "computer_coordinate_out_of_bounds",
                "语义定位区域超出虚拟桌面半开区间，拒绝裁剪（不 clamp 越界坐标）",
            )
        crop_w = crop_right - crop_left
        crop_h = crop_bottom - crop_top
        cropped = image.crop(
            (
                crop_left - virtual_x,
                crop_top - virtual_y,
                crop_right - virtual_x,
                crop_bottom - virtual_y,
            )
        )
        model_width, model_height = _scaled_dimensions(crop_w, crop_h)
        if (model_width, model_height) != (crop_w, crop_h):
            cropped = cropped.resize((model_width, model_height), _RESAMPLE)
        png = self._encode_png(cropped)
        path = self._store_screenshot_locked(png)
        return ComputerObservation(
            run_id=run_id,
            target="desktop",
            action="observe_region",
            ok=True,
            screen_sha256=hashlib.sha256(png).hexdigest(),
            png_path=str(path),
            width=model_width,
            height=model_height,
            original_width=crop_w,
            original_height=crop_h,
            origin_x=crop_left,
            origin_y=crop_top,
            scale_x=crop_w / model_width,
            scale_y=crop_h / model_height,
            window_title=self._window_title_summary(),
        )

    def _wait_interruptible(self, duration_ms: int) -> None:
        end = time.monotonic() + duration_ms / 1000
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            if self._stop_event.wait(min(remaining, 0.05)):
                raise ComputerControlError(
                    "computer_cancelled", "显式等待被停止中断"
                )

    def _wait_for_interruptible(self, hwnd: int, action: ComputerAction) -> None:
        condition = str(action.condition or "text_exists")
        timeout_ms = int(action.timeout_ms or WAIT_FOR_DEFAULT_TIMEOUT_MS)
        interval_ms = int(action.interval_ms or WAIT_FOR_DEFAULT_INTERVAL_MS)
        matcher = {
            "condition": condition,
            "text": action.text,
            "element_ref": action.element_ref,
        }
        window_name = str(action.window_name or "").casefold()
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if self._stop_event.is_set():
                raise ComputerControlError(
                    "computer_cancelled", "条件等待被停止中断"
                )
            matched = self._wait_for_match(
                int(hwnd), condition, matcher, window_name, deadline=deadline
            )
            if self._stop_event.is_set():
                raise ComputerControlError(
                    "computer_cancelled", "条件等待被停止中断"
                )
            remaining = deadline - time.monotonic()
            if matched and remaining > 0:
                return
            if remaining <= 0:
                raise ComputerControlError(
                    "computer_wait_timeout",
                    f"条件 {condition} 未在 {timeout_ms} ms 内满足；"
                    "可改用 observe 检查当前画面（或调整 text/window_name 后重试）",
                )
            if self._stop_event.wait(min(interval_ms / 1000, remaining)):
                raise ComputerControlError(
                    "computer_cancelled", "条件等待被停止中断"
                )

    def _wait_for_match(
        self,
        hwnd: int,
        condition: str,
        matcher: dict,
        window_name: str,
        *,
        deadline: float | None = None,
    ) -> dict | None:
        """单轮条件检查：window_active 只查实际前台 HWND 标题，其余走只读 probe。"""
        if condition == "window_active":
            if not window_name:
                return None
            fg_hwnd = int(self._adapter.foreground_window_id() or 0)
            if fg_hwnd <= 0:
                return None
            title = str(self._adapter.foreground_window_title() or "")
            if window_name in title.casefold():
                return {"condition": condition, "title": title}
            return None
        if window_name:
            # window_name 作范围限定：前台窗口标题不含该子串时本轮不命中
            # （不切换窗口、不激活任何窗口，纯只读判断）。
            try:
                title = str(self._adapter.foreground_window_title() or "").casefold()
            except Exception:
                _LOGGER.exception("读取前台窗口标题失败")
                title = ""
            if window_name not in title:
                return None
        locator = self._element_locator
        if locator is None:
            raise ComputerControlError(
                "computer_elements_unavailable",
                "当前桌面 backend 未提供 UIA 元素探测能力，无法执行该等待条件",
            )
        live_hwnd = int(self._adapter.foreground_window_id() or 0) or int(hwnd)
        return _call_with_supported_kwargs(
            locator.probe,
            live_hwnd,
            matcher,
            deadline=deadline,
            cancel_event=self._stop_event,
        )

    def _surface_signature_current_locked(
        self, origin_x: int, origin_y: int, width: int, height: int
    ) -> tuple:
        return (
            int(self._adapter.foreground_window_id()),
            origin_x,
            origin_y,
            width,
            height,
        )

    def _require_virtual_rect_unchanged_locked(self) -> tuple[int, tuple]:
        origin_x, origin_y, width, height = self._adapter.virtual_screen_rect()
        current = self._surface_signature_current_locked(
            origin_x, origin_y, width, height
        )
        if (
            self._surface_signature is not None
            and current[1:] != self._surface_signature[1:]
        ):
            raise ComputerControlError(
                "computer_surface_changed",
                "前台窗口或桌面几何已变化，旧坐标已失效，"
                "必须重新观察（observe）后再操作",
            )
        return current[0], current

    def _require_keyboard_surface_and_focus_locked(self) -> None:
        if self._last_pointer_target_hwnd is not None:
            if self._last_observation is not None and self._surface_signature is not None:
                self._require_virtual_rect_unchanged_locked()
            self._require_keyboard_focus_locked()
            return
        if self._in_sequence and self._sequence_keyboard_may_switch:
            if self._last_observation is not None and self._surface_signature is not None:
                fg_hwnd, _ = self._require_virtual_rect_unchanged_locked()
                if fg_hwnd <= 0:
                    raise ComputerControlError(
                        "computer_surface_changed",
                        "当前没有活动前台窗口，必须重新观察（observe）后再操作",
                    )
            return
        self._require_current_surface_locked()
        self._require_keyboard_focus_locked()

    def _require_current_surface_locked(self) -> None:
        if self._last_observation is None or self._surface_signature is None:
            return
        origin_x, origin_y, width, height = self._adapter.virtual_screen_rect()
        current = self._surface_signature_current_locked(
            origin_x, origin_y, width, height
        )
        if current != self._surface_signature:
            raise ComputerControlError(
                "computer_surface_changed",
                "前台窗口或桌面几何已变化，旧坐标已失效，"
                "必须重新观察（observe）后再操作",
            )
        baseline = self._region_baseline
        if baseline is not None and self._surface_locator is not None:
            ref, generation, bounds = baseline
            current_bounds = self._surface_locator.current_candidate_bounds(ref, generation)
            if current_bounds is None or tuple(current_bounds) != tuple(bounds):
                raise ComputerControlError(
                    "computer_surface_changed",
                    "局部观察区域对应的窗口/控件已移动或消失，"
                    "必须重新 list_surfaces 观察后再操作",
                )

    def _require_keyboard_focus_locked(self) -> None:
        expected = self._last_pointer_target_hwnd
        if expected is None:
            signature = self._surface_signature
            expected = None if signature is None else int(signature[0])
        if expected is None:
            return
        current = int(self._adapter.foreground_window_id())
        if current == int(expected):
            return
        raise ComputerControlError(
            "computer_foreground_lost",
            f"前台窗口（hwnd {current}）已不是最近一次点击的目标窗口"
            f"（hwnd {int(expected)}），键盘输入会落到错误窗口，已在发送前中止。"
            "请先用 click 点回目标窗口，或用 activate_surface 重新激活目标窗口"
            "后再输入。",
            {"expected_hwnd": int(expected), "current_hwnd": current},
        )

    def _raise_if_stopped_locked(self) -> None:
        if self._stop_event.is_set():
            raise ComputerControlError(
                "computer_cancelled", "桌面 backend 已停止，拒绝提交新输入"
            )

    def request_stop(self) -> None:
        """关闭输入准入而不等待当前 backend 锁，供 controller 的停止路径先调用。"""
        self._stop_event.set()
        adapter_stop = getattr(self._adapter, "request_stop", None)
        if callable(adapter_stop):
            adapter_stop()

    def release_inputs(self) -> None:
        """尽力释放所有按下的键和鼠标键；失败只留痕不抛出。"""
        try:
            with self._lock:
                self._adapter.release_all()
        except Exception:
            _LOGGER.exception("释放桌面输入状态失败")
            raise

    def is_reusable(self) -> bool:
        """retained 领取裁决（R4）：桌面 backend 同步执行，无 pending 命令。"""
        with self._lock:
            return self._started and not self._closed

    def close(self) -> None:
        self.request_stop()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            screenshot_dir = self._screenshot_dir
            owns = self._owns_screenshot_dir
            self._screenshot_dir = None
            self._last_observation = None
        self.release_inputs()
        locator = self._surface_locator
        if locator is not None:
            try:
                locator.close()
            except Exception:
                _LOGGER.exception("关闭桌面语义定位器失败")
        element_locator = self._element_locator
        if element_locator is not None:
            try:
                element_locator.close()
            except Exception:
                _LOGGER.exception("关闭桌面元素定位器失败")
        if owns and screenshot_dir is not None:
            shutil.rmtree(screenshot_dir, ignore_errors=True)

    def _start_locked(self) -> None:
        if self._closed:
            raise ComputerControlError(
                "computer_backend_start_failed",
                "桌面 backend 已关闭，请创建新实例",
            )
        if self._started:
            return
        if Image is None:
            raise ComputerControlError(
                "computer_backend_start_failed",
                "Pillow 不可用，无法进行桌面截图",
            )
        self._adapter.preflight()
        self._grab_checked_locked()
        self._started = True

    def _ensure_started_locked(self) -> None:
        if self._closed:
            raise ComputerControlError(
                "computer_backend_start_failed",
                "桌面 backend 已关闭，请创建新实例",
            )
        if not self._started:
            self._start_locked()

    def _grab_checked_locked(self) -> tuple[tuple[int, int, int, int], object]:
        """截取一帧并校验尺寸与 Win32 虚拟桌面一致；失败绝不复用旧观察。"""
        origin_x, origin_y, width, height = self._adapter.virtual_screen_rect()
        try:
            image = self._image_grab()
        except ComputerControlError:
            raise
        except Exception as error:
            raise ComputerControlError(
                "computer_backend_start_failed",
                f"桌面截图失败：{type(error).__name__}",
            ) from error
        size = getattr(image, "size", None)
        try:
            matched = tuple(size) == (width, height)
        except TypeError:
            matched = False
        if not matched:
            raise ComputerControlError(
                "computer_backend_start_failed",
                f"截图尺寸 {size} 与 Win32 虚拟桌面 {width}x{height} 不一致",
            )
        return (origin_x, origin_y, width, height), image

    def _encode_png(self, image) -> bytes:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=False)
        return buffer.getvalue()

    def _window_title_summary(self) -> str:
        try:
            title = self._adapter.foreground_window_title()
        except Exception:
            _LOGGER.exception("读取前台窗口标题失败")
            return ""
        if not isinstance(title, str):
            return ""
        return title[:_MAX_TITLE_CHARS]

    def _require_observation(self) -> ComputerObservation:
        frame = self._active_frame
        if frame is not None and not frame.coordinate_input_enabled:
            raise coordinate_disabled_error(frame.disabled_reason)
        observation = self._last_observation
        if observation is None:
            raise ComputerControlError(
                "computer_invalid_action",
                "尚未有任何观察，坐标动作前必须先 observe",
            )
        return observation

    def _physical_point(self, action: ComputerAction) -> tuple[int, int]:
        return screenshot_to_physical(action.x, action.y, self._require_observation())

    def _physical_path(self, action: ComputerAction) -> tuple[tuple[int, int], ...]:
        points = action.path
        if len(points) < 2:
            raise ComputerControlError(
                "computer_invalid_action", "drag 需要至少 2 个 path 点"
            )
        observation = self._require_observation()
        return tuple(
            screenshot_to_physical(point_x, point_y, observation)
            for point_x, point_y in points
        )

    def _dispatch_locked(self, action: ComputerAction) -> None:
        self._raise_if_stopped_locked()
        kind = action.kind
        if kind in ("click", "double_click", "move"):
            x, y = self._physical_point(action)
            if kind != "move":
                if self._ensure_target_window_active_locked((x, y)):
                    self._last_activation_changed = True
            self._adapter.send_mouse(
                action=kind, x=x, y=y, button=action.button, modifiers=tuple(action.keys)
            )
        elif kind == "scroll":
            if action.x is not None or action.y is not None:
                x, y = self._physical_point(action)
                self._adapter.send_mouse(action="move", x=x, y=y)
            if type(action.delta_x) is not int or type(action.delta_y) is not int:
                raise ComputerControlError(
                    "computer_invalid_action", "delta_x/delta_y 必须是整数"
                )
            # R7：桌面滚动只接受 wheel_step（逻辑步进，正值向下/向右）；
            # 像素语义请求显式拒绝，绝不静默近似换算。
            unit = action.unit or "wheel_step"
            if unit != "wheel_step":
                raise ComputerControlError(
                    "computer_invalid_action",
                    f"桌面滚动仅支持 wheel_step 单位，收到 {unit!r}",
                )
            if not action.delta_x and not action.delta_y:
                raise ComputerControlError(
                    "computer_invalid_action", "scroll 的 delta_x/delta_y 不能同时为零"
                )
            if abs(action.delta_x) > 10 or abs(action.delta_y) > 10:
                raise ComputerControlError(
                    "computer_invalid_action",
                    "桌面滚动每轴范围 ±10 步进，超限拒绝（不裁剪）",
                )
            if action.delta_y:
                self._adapter.send_mouse(action="wheel", delta=action.delta_y)
            if action.delta_x:
                self._raise_if_stopped_locked()
                self._adapter.send_mouse(action="hwheel", delta=action.delta_x)
        elif kind == "drag":
            path = self._physical_path(action)
            if self._ensure_target_window_active_locked(path[0]):
                self._last_activation_changed = True
            self._adapter.send_mouse(
                action="drag",
                path=path,
                button=action.button,
                modifiers=tuple(action.keys),
            )
        elif kind == "type_text":
            if not isinstance(action.text, str) or not action.text:
                raise ComputerControlError(
                    "computer_invalid_action", "type_text 需要非空 text"
                )
            if any(ord(ch) > 127 for ch in action.text):
                self._adapter.set_clipboard_text(action.text)
                if self._stop_event.wait(_CLIPBOARD_PASTE_DELAY_S):
                    raise ComputerControlError(
                        "computer_cancelled",
                        "电脑操控已停止，取消尚未提交的中文粘贴",
                    )
                self._adapter.send_keys(("CTRL", "V"))
            else:
                self._adapter.send_text(action.text)
        elif kind == "keypress":
            if not action.keys:
                raise ComputerControlError(
                    "computer_invalid_action", "keypress 需要至少一个键"
                )
            self._adapter.send_keys(tuple(action.keys))
        else:
            raise ComputerControlError(
                "computer_invalid_action",
                f"桌面 backend 不支持动作：{kind!r}",
            )

    def _store_screenshot_locked(self, png: bytes) -> Path:
        if self._screenshot_dir is None:
            self._screenshot_dir = Path(
                tempfile.mkdtemp(prefix="ycode-computer-desktop-")
            )
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
