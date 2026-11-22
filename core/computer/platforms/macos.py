from core.computer.platforms.base import UnavailablePlatformAdapter


class MacOSPlatformAdapter(UnavailablePlatformAdapter):
    def __init__(self):
        super().__init__("macos", "macOS 电脑操控接口已预留，系统授权、桌面输入与紧急停止尚未适配")
