"""Windows 桌面 backend：截图、坐标映射与 SendInput 调度（设计 13、21、22）。

- 截图由构造注入的 ``image_grab`` 提供；生产默认 ``default_image_grab()``
  在方法内惰性 ``from PIL import ImageGrab`` 并 ``grab(all_screens=True)``，
  模块顶层不导入 PIL.ImageGrab 做任何副作用操作。
- 虚拟桌面原点/宽高来自 Win32 adapter 的 GetSystemMetrics；Pillow 图像尺寸
  必须与 Win32 宽高一致，否则 preflight/observe 失败。
- 最长边大于 1920 时按同一比例等比缩小，PNG 使用确定参数编码。
- 坐标反算只依赖最新 observation；SendInput 前由 adapter 再次检查虚拟桌面
  半开区间边界（越界 computer_coordinate_out_of_bounds）。
- 每次桌面动作前重新检查 interactive desktop；截图失败抛
  computer_backend_start_failed，绝不复用 _last_observation。
- pytest 通过 FakeImage/FakeWin32Adapter 注入，绝不真实截屏或注入输入。
"""

from __future__ import annotations

import hashlib
import io
import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from core.computer.actions import MAX_WAIT_MS, MIN_WAIT_MS
from core.computer.models import (
    ComputerAction,
    ComputerControlError,
    ComputerObservation,
)

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
_BROWSER_ONLY_KINDS = frozenset({"navigate", "back", "forward", "reload"})


def default_image_grab():
    """生产截图入口：方法内惰性导入 PIL.ImageGrab，捕获整个虚拟桌面。"""
    from PIL import ImageGrab

    return ImageGrab.grab(all_screens=True)


def screenshot_to_physical(
    x: int, y: int, observation: ComputerObservation
) -> tuple[int, int]:
    """按最新 observation 记录的比例把截图坐标反算为物理坐标（设计 13.1）。

    physical_x = origin_x + round(x * original_width / model_width)
    physical_y = origin_y + round(y * original_height / model_height)

    bool 不透传（computer_invalid_action）；模型坐标与换算后的物理坐标都
    必须在半开区间内（越界 computer_coordinate_out_of_bounds）。
    """
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


class WindowsComputerBackend:
    """Windows 桌面 backend（target="desktop"）；线程安全，关闭幂等。"""

    target = "desktop"

    def __init__(
        self,
        image_grab: Callable,
        win32_adapter,
        *,
        screenshot_dir: Path | None = None,
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
        self._screenshot_dir = Path(screenshot_dir) if screenshot_dir is not None else None
        self._owns_screenshot_dir = screenshot_dir is None
        self._screenshot_counter = 0
        self._lock = threading.Lock()
        self._started = False
        self._closed = False
        self._last_observation: ComputerObservation | None = None

    @classmethod
    def create_default(cls) -> "WindowsComputerBackend":
        """生产构造：真实 ImageGrab + 真实 Win32InputAdapter（惰性导入）。"""
        from core.computer.win32_adapter import Win32InputAdapter

        return cls(image_grab=default_image_grab, win32_adapter=Win32InputAdapter())

    # ---------------- ComputerBackend 协议 ----------------

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def observe(self, run_id: str) -> ComputerObservation:
        with self._lock:
            self._ensure_started_locked()
            # 每次桌面动作（含观察）前重新检查 interactive desktop。
            self._adapter.ensure_interactive()
            (origin_x, origin_y, original_width, original_height), image = (
                self._grab_checked_locked()
            )
            model_width, model_height = _scaled_dimensions(
                original_width, original_height
            )
            if (model_width, model_height) != (original_width, original_height):
                image = image.resize((model_width, model_height), _RESAMPLE)
            png = self._encode_png(image)
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
            )
            self._last_observation = observation
            return observation

    def execute(self, run_id: str, action: ComputerAction) -> None:
        with self._lock:
            self._ensure_started_locked()
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
            # 每次桌面动作前重新检查 interactive desktop（锁屏/安全桌面即失败）。
            self._adapter.ensure_interactive()
            if kind == "wait":
                duration = action.duration_ms
                if type(duration) is not int or not MIN_WAIT_MS <= duration <= MAX_WAIT_MS:
                    raise ComputerControlError(
                        "computer_invalid_action",
                        f"duration_ms 必须在 {MIN_WAIT_MS}-{MAX_WAIT_MS} 毫秒之间",
                    )
                time.sleep(duration / 1000)
                return
            self._dispatch_locked(action)

    def release_inputs(self) -> None:
        """尽力释放所有按下的键和鼠标键；失败只留痕不抛出。"""
        try:
            self._adapter.release_all()
        except Exception:
            _LOGGER.exception("释放桌面输入状态失败")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            screenshot_dir = self._screenshot_dir
            owns = self._owns_screenshot_dir
            self._screenshot_dir = None
            self._last_observation = None
        self.release_inputs()
        if owns and screenshot_dir is not None:
            shutil.rmtree(screenshot_dir, ignore_errors=True)

    # ---------------- 内部：启动、截图、动作分发 ----------------

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
        # preflight 级别试抓一次：验证 Pillow 尺寸与 Win32 metrics 一致。
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
        """确定参数 PNG 编码：同一画面产生同一字节。"""
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=False)
        return buffer.getvalue()

    def _window_title_summary(self) -> str:
        """前台窗口标题的受控摘要：截断 200 字符，失败时为空串。"""
        try:
            title = self._adapter.foreground_window_title()
        except Exception:
            _LOGGER.exception("读取前台窗口标题失败")
            return ""
        if not isinstance(title, str):
            return ""
        return title[:_MAX_TITLE_CHARS]

    def _require_observation(self) -> ComputerObservation:
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
        kind = action.kind
        if kind in ("click", "double_click", "move"):
            x, y = self._physical_point(action)
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
            if action.delta_y:
                self._adapter.send_mouse(action="wheel", delta=action.delta_y)
            if action.delta_x:
                self._adapter.send_mouse(action="hwheel", delta=action.delta_x)
        elif kind == "drag":
            self._adapter.send_mouse(
                action="drag",
                path=self._physical_path(action),
                button=action.button,
                modifiers=tuple(action.keys),
            )
        elif kind == "type_text":
            if not isinstance(action.text, str) or not action.text:
                raise ComputerControlError(
                    "computer_invalid_action", "type_text 需要非空 text"
                )
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

    # ---------------- 内部：截图短期存储（最多保留最近 3 张） ----------------

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
