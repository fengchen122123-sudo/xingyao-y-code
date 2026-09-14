"""Windows GUI window/tray lifecycle with deterministic application shutdown."""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Callable

logger = logging.getLogger(__name__)


def _app_title() -> str:
    """窗口/托盘标题：含设置页自定义的智能体显示名。"""
    try:
        from core.persona_settings import get_agent_display_name

        return f"星瑶Code · {get_agent_display_name()}"
    except Exception:
        return "星瑶Code · 星瑶"


class ApplicationLifecycle:
    def __init__(
        self,
        window,
        shutdown: Callable[[], None],
        *,
        icon_path: str = "",
        tray_factory: Callable | None = None,
        has_running_work: Callable[[], bool] | None = None,
        confirm_close_running: Callable[[bool], str] | None = None,
    ) -> None:
        self._window = window
        self._shutdown = shutdown
        self._icon_path = icon_path
        self._tray_factory = tray_factory or self._create_native_tray
        self._has_running_work = has_running_work
        self._confirm_close_running = (
            confirm_close_running or self._native_confirm_close_running
        )
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
            _app_title(),
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

    def is_tray_ready(self) -> bool:
        """托盘是否就绪（frameless 前端关闭确认框据决定是否提供后台驻留选项）。"""
        with self._lock:
            return self._tray_ready

    def on_closing(self):
        """pywebview closing callback: False cancels native close.

        关闭策略（避免"以为退出了、进程驻留后台改文件"的认知偏差）：
        - 已在退出序列中 / 没有回合在跑：放行原生关闭（真退出）。
        - 有回合在跑：弹原生确认框。
          后台继续（仅托盘可用时提供）→ 隐藏窗口驻留托盘；
          停止并退出 → 走 exit_application 完整关停序列（取消回合 → drain）；
          取消 → 返回窗口。
        - 未注入 has_running_work（旧调用方/测试替身）：退回旧行为，
          托盘就绪即隐藏驻留。
        """
        with self._lock:
            exiting = self._exiting
            tray_ready = self._tray_ready
        if exiting:
            return True
        if self._has_running_work is None:
            # 旧行为兼容：托盘就绪 → 隐藏驻留
            if tray_ready:
                self._window.hide()
                return False
            return True
        try:
            running = bool(self._has_running_work())
        except Exception:
            logger.exception("检查运行中回合失败，按无回合处理")
            running = False
        if not running:
            return True
        choice = self._confirm_close_running(allow_background=tray_ready)
        if choice == "cancel":
            return False
        if choice == "background":
            self._window.hide()
            return False
        # 'exit'：停止回合并退出（关停序列含回合取消与 drain）
        self.exit_application()
        return False

    def _native_confirm_close_running(self, *, allow_background: bool) -> str:
        """win32 原生三选/二选框；非 win32 或弹框失败时保守按退出处理。

        返回 'background' / 'exit' / 'cancel'。
        """
        if sys.platform != "win32":
            return "exit"
        try:
            import ctypes

            MB_YESNOCANCEL = 0x3
            MB_YESNO = 0x4
            MB_ICONQUESTION = 0x20
            MB_TOPMOST = 0x40000
            IDYES, IDNO = 6, 7
            if allow_background:
                text = (
                    "有对话回合仍在执行。\n\n"
                    "【是】最小化到托盘继续运行\n"
                    "【否】停止回合并退出 星瑶Code\n"
                    "【取消】返回窗口"
                )
                ret = ctypes.windll.user32.MessageBoxW(
                    None, text, _app_title(),
                    MB_YESNOCANCEL | MB_ICONQUESTION | MB_TOPMOST,
                )
                if ret == IDYES:
                    return "background"
                if ret == IDNO:
                    return "exit"
                return "cancel"
            text = (
                "有对话回合仍在执行（系统托盘不可用，无法后台驻留）。\n\n"
                "【是】停止回合并退出 星瑶Code\n"
                "【否】返回窗口"
            )
            ret = ctypes.windll.user32.MessageBoxW(
                None, text, _app_title(),
                MB_YESNO | MB_ICONQUESTION | MB_TOPMOST,
            )
            return "exit" if ret == IDYES else "cancel"
        except Exception:
            logger.exception("关闭确认框弹出失败，按停止并退出处理")
            return "exit"

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
        # 菜单回调运行在 pystray 消息循环线程上（win32 后端在 TrackPopupMenuEx
        # 返回后同步调用）。shutdown 内含多段 join（回合线程 drain、子代理
        # shutdown 各最长 5s），若在该线程同步执行会长时间卡死托盘消息循环，
        # tray.stop() 投递的 WM_STOP 也得不到处理；window.destroy() 从托盘线程
        # 直接调用也未被 pywebview 承诺线程安全。整个 stop + destroy 序列移到
        # 独立退出线程执行，菜单回调立即返回。
        # （已核实 pystray>=0.19.5：stop() 只 PostMessage 异步停消息循环，
        # join 的是短命的 setup 线程且有 5s 超时，不存在自 join 死锁。）
        threading.Thread(
            target=self._exit_sequence,
            daemon=True,
            name="gui-exit-sequence",
        ).start()

    def _exit_sequence(self) -> None:
        self._finalize_once()
        try:
            self._window.destroy()
        except Exception:
            logger.exception("销毁主窗口失败")

    def finalize(self) -> None:
        """Called after the native GUI loop exits by any non-tray path."""
        with self._lock:
            self._exiting = True
        self._finalize_once()
