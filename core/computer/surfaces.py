"""平台无关的软件/控件语义定位契约（R10）。

本模块只定义值对象与协议，不导入任何本机图形/COM 依赖。Windows 实现在
``core/computer/platforms/windows_surfaces.py``，由 WindowsPlatformAdapter 提供；
macOS/Linux 在各自平台模块实现前一律显式拒绝（computer_unsupported_os）。

ref 只引用当前会话已观察到的对象：每个候选绑定 locator 会话与观察代数
（surface_generation），操作时必须回传同一代数；模型不得伪造 HWND、进程 ID
或任意脚本字符串。应用名称有多个候选时返回候选列表，绝不自动点第一个。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# 候选 kind 取值：顶层窗口 / 托盘图标 / 窗口内控件。
SURFACE_KINDS = ("window", "tray_icon", "control")

# 候选支持的操作：activate（置前台窗口）/ invoke（语义调用，如 UIA Invoke）/
# click_point（边界中点的显式坐标点击）。
SURFACE_OPERATIONS = ("activate", "invoke", "click_point")

MAX_SURFACE_LABEL_CHARS = 80


@dataclass(frozen=True)
class SurfaceCandidate:
    """一次观察中发现的候选对象；bounds 为虚拟桌面物理坐标 (x, y, w, h)。"""

    ref: str
    kind: str  # SURFACE_KINDS 之一
    label: str  # 受长度限制的名称摘要
    surface_generation: int
    supported_operations: tuple[str, ...]
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)


@dataclass(frozen=True)
class ObservationRegion:
    """局部观察区域（R10）：原点为虚拟桌面物理坐标，含模型图缩放尺寸。

    裁剪图坐标反算与 screenshot_to_physical 同构，但 original_width/height
    是区域尺寸，不是整个虚拟桌面尺寸：

    physical_x = origin_x + round(x * original_width / model_width)
    physical_y = origin_y + round(y * original_height / model_height)
    """

    origin_x: int
    origin_y: int
    original_width: int
    original_height: int
    model_width: int
    model_height: int


def region_to_physical(x: int, y: int, region: ObservationRegion) -> tuple[int, int]:
    """把区域模型图坐标反算为虚拟桌面物理坐标（半开区间，越界拒绝，不 clamp）。"""
    from core.computer.models import ComputerControlError

    if type(x) is not int or type(y) is not int:
        raise ComputerControlError(
            "computer_invalid_action", "坐标 x/y 必须是整数（bool 不透传）"
        )
    if region.model_width <= 0 or region.model_height <= 0:
        raise ComputerControlError(
            "computer_invalid_action", "区域观察缺少有效的模型宽高，无法换算坐标"
        )
    if not (0 <= x < region.model_width and 0 <= y < region.model_height):
        raise ComputerControlError(
            "computer_coordinate_out_of_bounds",
            f"截图坐标 ({x}, {y}) 超出区域观察画面 "
            f"{region.model_width}x{region.model_height} 半开区间",
        )
    physical_x = region.origin_x + round(x * region.original_width / region.model_width)
    physical_y = region.origin_y + round(y * region.original_height / region.model_height)
    if not (
        region.origin_x <= physical_x < region.origin_x + region.original_width
        and region.origin_y <= physical_y < region.origin_y + region.original_height
    ):
        raise ComputerControlError(
            "computer_coordinate_out_of_bounds",
            f"物理坐标 ({physical_x}, {physical_y}) 超出区域半开区间",
        )
    return physical_x, physical_y


@runtime_checkable
class SurfaceLocator(Protocol):
    """平台语义定位器；所有调用在本平台实现指定的专属线程上执行。"""

    def list_windows(self) -> tuple[SurfaceCandidate, ...]: ...

    def list_tray_icons(self) -> tuple[SurfaceCandidate, ...]: ...

    def activate_window(self, ref: str, generation: int) -> None:
        """激活候选窗口并验证实际前台身份；失败显式抛领域错误。"""
        ...

    def invoke_element(self, ref: str, generation: int) -> None:
        """按候选实际支持的操作调用（Invoke/边界中点显式点击）；失败显式抛出。"""
        ...

    def close(self) -> None: ...
