"""Linux 接入位置；X11/Wayland 必须分别实现与验收，不默认互换。"""

from core.computer.platforms.base import UnavailablePlatformAdapter


class LinuxPlatformAdapter(UnavailablePlatformAdapter):
    def __init__(self):
        super().__init__("linux", "Linux 电脑操控接口已预留，X11/Wayland 桌面输入与紧急停止尚未适配")
