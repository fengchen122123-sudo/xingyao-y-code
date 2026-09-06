"""平台接入契约；不加载任何本机输入或图形依赖。"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.computer.models import ComputerBackend, ComputerControlError

if TYPE_CHECKING:
    from core.computer.hotkey import HotkeyApi
    from core.computer.surfaces import SurfaceLocator


@runtime_checkable
class ComputerPlatformAdapter(Protocol):
    name: str
    supported: bool
    unavailable_reason: str

    def find_browser_executable(self, browser: str) -> str: ...

    def create_desktop_backend(self) -> ComputerBackend: ...

    def create_hotkey_api(self) -> "HotkeyApi": ...

    def preflight_desktop(self) -> None: ...

    def probe_hotkey(self) -> bool: ...

    def create_surface_locator(self) -> "SurfaceLocator":
        """语义定位器（R10）；未实现的平台显式抛 computer_unsupported_os。"""
        ...

    def browser_window_hints(self, pid: int) -> tuple:
        """受控浏览器进程的可见窗口标题提示（R5 受控页裁决系统侧信号）。

        返回 (hwnd, title, is_foreground) 元组；未实现的平台显式抛
        computer_unsupported_os（backend 据此回落到 DOM 可见性裁决）。
        """
        ...


@dataclass(frozen=True)
class UnavailablePlatformAdapter:
    """未实现平台的显式边界：仅允许查询，不伪造可执行后端。"""

    name: str
    unavailable_reason: str

    @property
    def supported(self) -> bool:
        return False

    def find_browser_executable(self, browser: str) -> str:
        return ""

    def _unsupported(self) -> ComputerControlError:
        return ComputerControlError("computer_unsupported_os", self.unavailable_reason)

    def create_desktop_backend(self) -> ComputerBackend:
        raise self._unsupported()

    def create_hotkey_api(self) -> "HotkeyApi":
        raise self._unsupported()

    def preflight_desktop(self) -> None:
        raise self._unsupported()

    def probe_hotkey(self) -> bool:
        raise self._unsupported()

    def create_surface_locator(self) -> "SurfaceLocator":
        raise self._unsupported()

    def browser_window_hints(self, pid: int) -> tuple:
        raise self._unsupported()
