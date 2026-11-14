"""Windows 接入层；执行依赖仅在相应方法调用时导入。"""

import os
import shutil

from core.computer.models import ComputerControlError


_BROWSER_INSTALLS = {
    "edge": ("msedge.exe", os.path.join("Microsoft", "Edge", "Application", "msedge.exe")),
    "chrome": ("chrome.exe", os.path.join("Google", "Chrome", "Application", "chrome.exe")),
}


class WindowsPlatformAdapter:
    name = "windows"
    supported = True
    unavailable_reason = ""

    def find_browser_executable(self, browser: str) -> str:
        # chromium 只给 Linux serve 用。Windows 列出它为不可用，不探测、不抛错。
        if browser == "chromium":
            return ""
        if browser not in _BROWSER_INSTALLS:
            raise ComputerControlError("computer_invalid_config", f"不支持的浏览器：{browser!r}")
        executable_name, relative_path = _BROWSER_INSTALLS[browser]
        executable = shutil.which(executable_name)
        if executable:
            return executable
        # 同一浏览器依次检查原有安装位置，不跨浏览器或跨平台替换。
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(env_name)
            if root:
                candidate = os.path.join(root, relative_path)
                if os.path.isfile(candidate):
                    return candidate
        return ""

    def create_desktop_backend(self):
        from core.computer.windows_backend import WindowsComputerBackend

        return WindowsComputerBackend.create_default()

    def create_hotkey_api(self):
        from core.computer.hotkey import WindowsHotkeyApi

        return WindowsHotkeyApi()

    def preflight_desktop(self) -> None:
        from core.computer.win32_adapter import Win32InputAdapter

        Win32InputAdapter().preflight()

    def probe_hotkey(self) -> bool:
        from core.computer.win32_adapter import _default_hotkey_probe

        return _default_hotkey_probe()

    def create_surface_locator(self):
        """语义定位器（R10）；构造不枚举窗口、不初始化 COM。"""
        from core.computer.platforms.windows_surfaces import WindowsSurfaceLocator

        return WindowsSurfaceLocator()

    def browser_window_hints(self, pid: int) -> tuple:
        """受控浏览器进程可见窗口标题提示（只读标题，不读窗口内容）。"""
        from core.computer.platforms.windows_surfaces import browser_window_hints

        return browser_window_hints(pid)
