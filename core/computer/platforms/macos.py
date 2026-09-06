"""macOS 接入位置；授权、坐标、输入与热键未实现前保持不可用。"""

from core.computer.platforms.base import UnavailablePlatformAdapter


class MacOSPlatformAdapter(UnavailablePlatformAdapter):
    def __init__(self):
        super().__init__("macos", "macOS 电脑操控接口已预留，系统授权、桌面输入与紧急停止尚未适配")
