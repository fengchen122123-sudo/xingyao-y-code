"""Windows GUI window/tray lifecycle with deterministic application shutdown."""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class ApplicationLifecycle:
    def __init__(
        self,
        window,
        shutdown: Callable[[], None],
        *,
        icon_path: str = "",
        tray_factory: Callable | None = None,
    ) -> None:
        self._window = window
        self._shutdown = shutdown
        self._icon_path = icon_path
        self._tray_factory = tray_factory or self._create_native_tray
        self._tray = None
        self._tray_ready = False
        self._exiting = False
        self._finalized = False
        self._lock = threading.RLock()

    def _create_native_tray(self, show_window, exit_application):
        if sys.platform != "win32":
            raise RuntimeError("system tray is currently supported on Windows only")
        import pystray
        from PIL import Image

        if not self._icon_path or not os.path.isfile(self._icon_path):
            raise FileNotFoundError(self._icon_path or "tray icon")
        image = Image.open(self._icon_path)
        return pystray.Icon(
            "ycode",
            image,
            "Y-CODE · 星瑶",
            menu=pystray.Menu(
                pystray.MenuItem("显示主窗口", show_window, default=True),
                pystray.MenuItem("退出", exit_application),
            ),
        )

    def start_tray(self) -> bool:
        ready = threading.Event()

        def mark_ready(icon):
            icon.visible = True
            ready.set()

        try:
            tray = self._tray_factory(self.show_window, self.exit_application)
            try:
                tray.run_detached(setup=mark_ready)
            except TypeError:
                # Small test doubles may not expose pystray's setup keyword.
                tray.run_detached()
                ready.set()
            if not ready.wait(timeout=5.0):
                tray.stop()
                raise RuntimeError("system tray did not become ready")
        except Exception:
            logger.exception("系统托盘初始化失败；关闭按钮将按正常退出处理")
            return False
        with self._lock:
            self._tray = tray
            self._tray_ready = True
        return True

    def on_closing(self):
        """pywebview closing callback: False cancels native close."""
        with self._lock:
            hide = self._tray_ready and not self._exiting
        if hide:
            self._window.hide()
            return False
        return True

    def show_window(self, *_args) -> None:
        with self._lock:
            if self._exiting:
                return
        self._window.show()
        try:
            self._window.restore()
        except Exception:
            pass

    def _finalize_once(self) -> None:
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
            tray = self._tray
        try:
            self._shutdown()
        finally:
            if tray is not None:
                try:
                    tray.stop()
                except Exception:
                    logger.exception("停止系统托盘失败")

    def exit_application(self, *_args) -> None:
        with self._lock:
            if self._exiting:
                return
            self._exiting = True
        self._finalize_once()
        self._window.destroy()

    def finalize(self) -> None:
        """Called after the native GUI loop exits by any non-tray path."""
        with self._lock:
            self._exiting = True
        self._finalize_once()
