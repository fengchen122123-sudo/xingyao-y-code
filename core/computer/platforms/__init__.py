"""固定内核的平台选择入口；不提供插件注册或跨平台降级。"""

import sys

from core.computer.platforms.base import ComputerPlatformAdapter, UnavailablePlatformAdapter


def get_platform_adapter(platform: str | None = None) -> ComputerPlatformAdapter:
    selected = sys.platform if platform is None else platform
    if selected == "win32":
        from core.computer.platforms.windows import WindowsPlatformAdapter

        return WindowsPlatformAdapter()
    if selected == "darwin":
        from core.computer.platforms.macos import MacOSPlatformAdapter

        return MacOSPlatformAdapter()
    if selected == "linux":
        from core.computer.platforms.linux import LinuxPlatformAdapter

        return LinuxPlatformAdapter()
    return UnavailablePlatformAdapter("unsupported", f"电脑操控尚未支持当前平台：{selected!r}")
