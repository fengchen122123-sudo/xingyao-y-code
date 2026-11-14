"""Linux 只提供无头浏览器。桌面输入和紧急停止仍然明确拒绝。"""

from __future__ import annotations

import shutil

from core.computer.platforms.base import UnavailablePlatformAdapter


class LinuxPlatformAdapter(UnavailablePlatformAdapter):
    def __init__(self):
        super().__init__(
            "linux",
            "Linux 不提供桌面输入与紧急停止，浏览器使用无头 Chromium",
        )

    @property
    def browser_supported(self) -> bool:
        return True

    @property
    def desktop_supported(self) -> bool:
        return False

    def find_browser_executable(self, browser: str) -> str:
        if browser != "chromium":
            return ""
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
            found = shutil.which(name)
            if found:
                return found
        return ""

    def browser_window_hints(self, pid: int) -> tuple:
        return ()
