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
# 剪贴板写入到 Ctrl+V 粘贴之间的小间隔：给目标窗口焦点线程让路（2026-09-06）。
_CLIPBOARD_PASTE_DELAY_S = 0.05
# Task C（P3 输入前置激活）：SetForegroundWindow 成功后的激活沉降等待。
_ACTIVATION_SETTLE_S = 0.15
_BROWSER_ONLY_KINDS = frozenset({"navigate", "back", "forward", "reload"})
# 阶段二（2026-09-06）：UIA 元素观察与元素引用动作（desktop only）。
_ELEMENT_KINDS = frozenset({"list_elements", "click_element", "set_value_element"})
# 自动补表的元素上限（与 WindowsElementLocator.MAX_ELEMENTS 对齐）：超大树
# 只截前 N 个，避免把一次观察拖成整树遍历后的超长上下文。
_MAX_OBSERVE_ELEMENTS = 200
# R10：语义定位与局部观察动作（不做 surface 输入复核——它们自带 ref+代数校验）。
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
        # R10：语义定位的待发布候选/待观察区域与区域基线（ref, generation, bounds）。
        self._pending_surface_candidates: tuple = ()
        # 阶段二：list_elements 待发布的控件元素列表（与候选同一取用-清空模式）。
        self._pending_elements: tuple = ()
        # Task B（SoM）：最近一次有效元素表与其观察代数——list_elements 后
        # 紧邻 observe 记录；后续 observe 在代数未变时复用（观察继续标注），
        # 代数变化即清空并失效 locator 的 ref 全集。
        self._last_elements: tuple = ()
        self._elements_surface_generation: int = -1
        self._pending_region: tuple | None = None
        self._region_baseline: tuple | None = None
        # R5：桌面 surface 签名 = (前台窗口身份, 虚拟桌面几何)；变化时代数递增，
        # 输入前复核不一致即拒绝（旧坐标失效）。
        self._surface_signature: tuple | None = None
        self._surface_generation = 0
        # Task C（P3）：本动作是否发生了前台切换；observe 构造观察时消费
        # （写入 input_activated 并清零），输入动作分发前重置。
        self._last_activation_changed = False
        # 键盘焦点护栏：最近一次 pointer 动作（click/double_click/drag）的目标
        # 窗口身份；键盘动作发送前比对当前前台，不一致即 computer_foreground_lost。
        # 新观察会重建焦点基线（清空该参照），届时回落到观察记录的前台窗口。
        self._last_pointer_target_hwnd: int | None = None

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
            pending_candidates = self._pending_surface_candidates
            self._pending_surface_candidates = ()
            pending_elements = self._pending_elements
            self._pending_elements = ()
            pending_region = self._pending_region
            self._pending_region = None
            # 本次观察最终附加的元素集：pending 优先，其次代数未变的上代元素表。
            effective_elements = pending_elements
            # R5/Task B：代数先于观察构造更新——元素表有效性按最新代数判定。
            signature = self._surface_signature_current_locked(
                origin_x, origin_y, original_width, original_height
            )
            if signature != self._surface_signature:
                self._surface_generation += 1
                self._surface_signature = signature
            # 观察重建焦点基线：清空 pointer 目标参照，键盘护栏回落到本次前台，
            # 避免「先点 B、再观察 C」时拿旧点击目标误判焦点丢失。
            self._last_pointer_target_hwnd = None
            if pending_region is not None:
                # R10 局部观察：从原始分辨率整屏裁剪候选区域，区域原点/尺寸
                # 进入观察的 origin/original_*，坐标反算沿用既有公式结构。
                observation = self._build_region_observation_locked(
                    run_id, image, (origin_x, origin_y), pending_region
                )
                if pending_elements:
                    self._last_elements = pending_elements
                    self._elements_surface_generation = self._surface_generation
            else:
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
                elif (
                    self._last_elements
                    and self._elements_surface_generation == self._surface_generation
                ):
                    # Task B：上代元素表在代数未变时仍有效——观察继续标注。
                    elements = self._last_elements
                else:
                    elements = ()
                    if self._last_elements:
                        # 代数变化：旧元素表作废，locator 的 ref 全集同时失效。
                        self._last_elements = ()
                        self._elements_surface_generation = -1
                        if self._element_locator is not None:
                            try:
                                self._element_locator.invalidate()
                            except Exception:
                                _LOGGER.exception("失效旧元素引用失败")
                    # 自动补表：没有新 list_elements 时直接抓一次前台窗口元素，
                    # 模型第一次 observe 就能拿到 ref 用 click_element 精确点击
                    # （32×33 像素的小按钮靠截图目测必然点偏）。best-effort：
                    # 失败只留痕，观察照常返回普通截图。
                    elements = self._collect_elements_locked()
                    if elements:
                        self._last_elements = elements
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
                # 区域观察建立区域基线（候选 ref/代数/边界），供输入前失效复核。
                self._region_baseline = pending_region
            # Task C：消费本动作的前置激活结果（仅本地字段，绝不进入投影）。
            observation = replace(
                observation, input_activated=self._last_activation_changed
            )
            self._last_activation_changed = False
            self._last_observation = observation
            return observation

    def execute(self, run_id: str, action: ComputerAction) -> None:
        wait_duration: int | None = None
        # wait_for（2026-11-09 Windows-MCP 对齐）：锁内只记录动作本体，轮询在
        # 锁外进行——条件等待最长 30s，持锁会阻塞 close/停止收尾（与 wait 同层）。
        wait_for_action: ComputerAction | None = None
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
            # Task C：激活标记只反映本动作——分发前重置（异常路径不留陈旧标记）。
            self._last_activation_changed = False
            if kind == "wait":
                duration = action.duration_ms
                if type(duration) is not int or not MIN_WAIT_MS <= duration <= MAX_WAIT_MS:
                    raise ComputerControlError(
                        "computer_invalid_action",
                        f"duration_ms 必须在 {MIN_WAIT_MS}-{MAX_WAIT_MS} 毫秒之间",
                    )
                wait_duration = duration
            elif kind == "wait_for":
                # 只读条件等待：不改变界面状态，不需要画面基线（不要求先 observe），
                # 但每次动作前的 interactive desktop 检查已在上面做过。
                wait_for_action = action
            elif kind in _ELEMENT_KINDS:
                self._dispatch_element_locked(action)
                return
            elif kind in _SEMANTIC_DESKTOP_KINDS:
                self._dispatch_semantic_locked(action)
                return
            else:
                # 真机验收修正（2026-09-06）：键盘动作不做事前激活——键盘没有
                # 目标点，"目标=上次观察的窗口"在按键打开新窗口（如 Win+R 运行
                # 对话框）的场景会把旧窗口抢回前台，真机实测 3 条文本被误发进
                # 微信群聊。键盘输入的焦点漂移维持 R5 fail-closed：前台与最近
                # 观察不一致即拒绝，强制重新 observe 后再输入。
                self._require_current_surface_locked()
                if kind in _KEYBOARD_KINDS:
                    # 键盘没有目标点：额外复核前台仍是最近一次 pointer 目标窗口。
                    self._require_keyboard_focus_locked()
                self._dispatch_locked(action)
                return
        # R8：wait 在锁外分片等待，close 可收尾、停止可中断。
        if wait_for_action is not None:
            self._wait_for_interruptible(
                int(self._adapter.foreground_window_id()), wait_for_action
            )
        elif wait_duration is not None:
            self._wait_interruptible(wait_duration)

    # ---------------- 阶段二：UIA 元素观察与元素引用动作 ----------------

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
            hwnd = self._adapter.foreground_window_id()
            self._pending_elements = tuple(locator.collect(hwnd))
            # Task B：记录收集时刻的观察代数，供后续 observe 判定元素表有效性。
            self._elements_surface_generation = self._surface_generation
            return
        self._require_current_surface_locked()
        if kind == "click_element":
            locator.click(action.element_ref)
            return
        # set_value_element
        if not isinstance(action.text, str) or not action.text:
            raise ComputerControlError(
                "computer_invalid_action", "set_value_element 需要非空 text"
            )
        locator.set_value(action.element_ref, action.text)

    def _collect_elements_locked(self) -> tuple:
        """自动补元素表（调用方持有锁）：前台窗口 UIA 元素，best-effort。

        observe 没有新的 list_elements 结果、也没有代数未变的上代元素表时调用：
        直接抓一次前台窗口元素，模型第一次观察即可用 click_element 精确点击，
        不必退回截图目测坐标（小控件只有几十像素，目测必然点偏）。

        任何失败（UIA 不可用、窗口关闭、COM 异常）只记日志并返回空表——观察
        退回普通截图，绝不因为补表失败阻断观察。元素数量按
        ``_MAX_OBSERVE_ELEMENTS`` 截断，避免超大树拖慢观察。
        """
        locator = self._element_locator
        if locator is None:
            return ()
        try:
            hwnd = self._adapter.foreground_window_id()
            listing = tuple(locator.collect(hwnd))
        except Exception:
            _LOGGER.exception("自动提取前台窗口元素失败，观察退回普通截图")
            return ()
        return listing[:_MAX_OBSERVE_ELEMENTS]

    # ---------------- Task C：输入前置激活（best-effort，绝不阻断输入） ----------------

    def _ensure_target_window_active_locked(self, point: tuple[int, int]) -> bool:
        """把输入目标点所属顶层窗口提到前台并等激活沉降；返回是否发生了前台切换。

        - 目标 = 输入点所属顶层窗口（WindowFromPoint，64 位安全经 adapter）：
          点击命中点所在窗口是权威目标，激活它不改变动作语义；
        - 已是前台/查询失败/无目标：不切换，返回 False；
        - SetForegroundWindow 失败或激活过程异常：抛 ComputerControlError 中止本次输入。
          旧实现是 best-effort 直接继续发送，输入会落到仍在前台的旧窗口，
          表现为失去焦点、点到别处，因此改为显式失败。

        仅用于 pointer 类动作（click/double_click/drag）；键盘动作没有目标点，
        不做前置激活（真机验收修正：键盘激活会把旧观察窗口抢回前台）。

        查询到的目标窗口无论是否发生切换，都记为「最近一次 pointer 目标窗口」，
        供后续键盘动作复核焦点（_require_keyboard_focus_locked）。
        """
        try:
            hwnd = self._adapter.window_from_point(int(point[0]), int(point[1]))
            if hwnd:
                # 记录最近一次 pointer 动作的目标窗口（键盘焦点参照）。
                self._last_pointer_target_hwnd = int(hwnd)
            if not hwnd or hwnd == self._adapter.foreground_window_id():
                return False
            if not self._adapter.set_foreground_window(hwnd):
                raise ComputerControlError(
                    "computer_foreground_activation_failed",
                    "目标窗口未能置前，已在发送输入前中止，避免输入落到其他窗口。请先调用 activate_surface 激活目标窗口，或重新 observe 建立基线后再试。",
                    {"hwnd": int(hwnd), "point": [int(point[0]), int(point[1])]},
                )
            time.sleep(_ACTIVATION_SETTLE_S)
            return True
        except ComputerControlError:
            raise
        except Exception as exc:
            _LOGGER.exception("输入前置激活失败")
            raise ComputerControlError(
                "computer_foreground_activation_failed",
                "激活目标窗口时出错，已在发送输入前中止，避免输入落到错误窗口。请重新 observe 建立基线后再试。",
                {"point": [int(point[0]), int(point[1])], "reason": str(exc)},
            ) from exc

    # ---------------- Task B：SoM 编号标注（best-effort，绝不阻断观察） ----------------

    def _annotate_elements_locked(
        self,
        png: bytes,
        elements: tuple,
        scale_x: float,
        scale_y: float,
        origin_x: int,
        origin_y: int,
    ) -> bytes:
        """把元素 ref 编号画到观察 PNG 上；标注失败退回普通截图并留痕。"""
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

    # ---------------- R10：语义定位与局部观察 ----------------

    def _dispatch_semantic_locked(self, action: ComputerAction) -> None:
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
            # 激活窗口并验证实际前台身份（SetForegroundWindow 失败显式报错）。
            locator.activate_window(action.surface_ref, action.surface_generation)
            return
        if kind == "invoke_surface":
            # 按候选实际支持的语义调用（UIA Invoke）；失败显式报错，
            # 绝不自动退化为同位置盲点（那是模型的显式 click 选择）。
            locator.invoke_element(action.surface_ref, action.surface_generation)
            return
        # observe_region：解析候选当前边界，交由紧随的观察裁剪原分辨率区域。
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
        if not (
            virtual_x <= region_x
            and virtual_y <= region_y
            and region_x + region_w <= virtual_x + desktop_w
            and region_y + region_h <= virtual_y + desktop_h
        ):
            raise ComputerControlError(
                "computer_coordinate_out_of_bounds",
                "语义定位区域超出虚拟桌面半开区间，拒绝裁剪（不 clamp 越界坐标）",
            )
        cropped = image.crop(
            (
                region_x - virtual_x,
                region_y - virtual_y,
                region_x - virtual_x + region_w,
                region_y - virtual_y + region_h,
            )
        )
        model_width, model_height = _scaled_dimensions(region_w, region_h)
        if (model_width, model_height) != (region_w, region_h):
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
            original_width=region_w,
            original_height=region_h,
            origin_x=region_x,
            origin_y=region_y,
            scale_x=region_w / model_width,
            scale_y=region_h / model_height,
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

    # ---------------- wait_for：只读条件轮询（锁外执行） ----------------

    def _wait_for_interruptible(self, hwnd: int, action: ComputerAction) -> None:
        """按 interval_ms 分片轮询只读条件，直到命中或超时（锁外执行）。

        设计红线（2026-11-09 设计 5.1 / 计划 Task 2 硬约束）：轮询**绝不**调用
        ``element_locator.collect``——collect 会整体替换 ref 全集
        （windows_elements_uia.py 的 collect 末尾 ``self._elements = elements``），
        模型手里的 el-N 会在等待期间全部失效。这里只走 ``locator.probe`` 的只读
        探测，全程不重建映射、不重新编号。

        分片语义：每片不超过 interval_ms，且不超过剩余时间；每片都用
        ``_stop_event.wait`` 等待，停止/关闭立即中断（computer_cancelled），
        绝不空转到超时。超时抛 computer_wait_timeout，message 提示改用 observe。
        """
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
            if self._wait_for_match(int(hwnd), condition, matcher, window_name):
                return
            remaining = deadline - time.monotonic()
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
        self, hwnd: int, condition: str, matcher: dict, window_name: str
    ) -> dict | None:
        """单轮条件检查：window_active 走顶层窗口标题，其余走只读 probe。"""
        if condition == "window_active":
            if not window_name:
                return None
            for title in self._adapter.list_top_window_titles() or ():
                folded = str(title).casefold()
                if window_name in folded:
                    return {"condition": condition, "title": str(title)}
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
        return locator.probe(int(hwnd), matcher)

    def _surface_signature_current_locked(
        self, origin_x: int, origin_y: int, width: int, height: int
    ) -> tuple:
        """当前桌面 surface 签名：前台窗口本地身份 + 虚拟桌面几何。"""
        return (
            int(self._adapter.foreground_window_id()),
            origin_x,
            origin_y,
            width,
            height,
        )

    def _require_current_surface_locked(self) -> None:
        """输入前复核：前台窗口身份/桌面几何必须与最近一次观察一致（R5）。

        用户切到其他软件、窗口切换或 DPI/分辨率变化都会使旧坐标失效：
        零输入并显式报错，由 controller 要求重新 observe。
        """
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
        # R10 区域基线失效复核：候选窗口/控件移动或消失使旧区域坐标作废。
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
        """键盘动作发送前复核：前台窗口必须是最近一次 pointer 动作的目标窗口。

        键盘没有目标点，焦点被抢走后按键会静默打进错误窗口。R5 只比对「最近
        一次观察」基线：用户从刚点击的窗口切回观察过的窗口时基线仍吻合，缺口
        正在这里——键盘输入会送到不该收键的窗口。参照优先级：

        1. 最近一次 pointer 动作（click/double_click/drag）的目标窗口；
        2. 没有 pointer 参照时回落到最近一次观察记录的前台窗口。

        两者都没有（尚未观察、也没点过）时放行，不阻塞首次键盘输入。不一致即
        零输入 + 显式报错，由模型 click 或 activate_surface 重建焦点。
        """
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

    def release_inputs(self) -> None:
        """尽力释放所有按下的键和鼠标键；失败只留痕不抛出。"""
        try:
            self._adapter.release_all()
        except Exception:
            _LOGGER.exception("释放桌面输入状态失败")

    def is_reusable(self) -> bool:
        """retained 领取裁决（R4）：桌面 backend 同步执行，无 pending 命令。"""
        with self._lock:
            return self._started and not self._closed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            screenshot_dir = self._screenshot_dir
            owns = self._owns_screenshot_dir
            self._screenshot_dir = None
            self._last_observation = None
        # 先中断进行中的显式 wait，再做其余清理（R8）。
        self._stop_event.set()
        self.release_inputs()
        # R10：释放语义定位器（COM 线程/UIA 元素）。
        locator = self._surface_locator
        if locator is not None:
            try:
                locator.close()
            except Exception:
                _LOGGER.exception("关闭桌面语义定位器失败")
        # Task A：释放 UIA 元素定位器（惰性 COM 线程）；失败只留痕不阻断收尾。
        element_locator = self._element_locator
        if element_locator is not None:
            try:
                element_locator.close()
            except Exception:
                _LOGGER.exception("关闭桌面元素定位器失败")
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
            if kind != "move":
                # Task C（P3）指针前置激活：目标点所属窗口非前台时先激活再输入。
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
                # 中文等非 ASCII 文本经 KEYEVENTF_UNICODE 注入会被 IME/目标
                # 应用输入处理扰动（会话 147 实测乱码），改走剪贴板 + Ctrl+V。
                self._adapter.set_clipboard_text(action.text)
                time.sleep(_CLIPBOARD_PASTE_DELAY_S)
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
