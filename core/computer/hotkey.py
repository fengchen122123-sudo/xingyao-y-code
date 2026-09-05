"""全局紧急停止热键 Ctrl+Alt+Esc（设计 3.3、15.1、17.3）。

EmergencyStopHotkey 依赖注入 HotkeyApi：回调只调用 on_stop，绝不直接触碰
GUI、prompt_toolkit 或 Provider 流。默认 Windows 实现（WindowsHotkeyApi）用
独立消息线程注册 MOD_CONTROL|MOD_ALT + VK_ESCAPE，只在收到对应 WM_HOTKEY
时回调；其他平台构造即抛 computer_unsupported_os。注册失败抛
computer_hotkey_unavailable 且 available=False。

get_emergency_hotkey_snapshot() 提供含 available 字段的保守快照，供
core.command_shell.get_system_settings() 惰性导入聚合；无活动实例时
返回 available=False。
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from core.computer.models import ComputerControlError

_LOGGER = logging.getLogger(__name__)

EMERGENCY_HOTKEY_LABEL = "Ctrl+Alt+Esc"

# Win32 常量（仅默认 WindowsHotkeyApi 使用）
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_NOREPEAT = 0x4000
_VK_ESCAPE = 0x1B
_WM_HOTKEY = 0x0312
_WM_APP_QUIT = 0x8001  # WM_APP + 1，close() 发给消息线程的退出消息
_HOTKEY_ID = 1


@runtime_checkable
class HotkeyApi(Protocol):
    """平台热键原语：register 同步返回注册是否成功，unregister 必须幂等。"""

    def register(self, on_hotkey: Callable[[], None]) -> bool: ...

    def unregister(self) -> None: ...


@dataclass(frozen=True)
class EmergencyHotkeySnapshot:
    """系统设置聚合用快照；无活动实例时保守返回 available=False。"""

    available: bool = False


_registry_lock = threading.Lock()
_active_hotkey: "EmergencyStopHotkey | None" = None


def _set_active_hotkey(instance: "EmergencyStopHotkey | None") -> None:
    global _active_hotkey
    with _registry_lock:
        _active_hotkey = instance


def get_emergency_hotkey_snapshot() -> EmergencyHotkeySnapshot:
    with _registry_lock:
        active = _active_hotkey
    if active is None:
        return EmergencyHotkeySnapshot(available=False)
    return EmergencyHotkeySnapshot(available=active.available)


class EmergencyStopHotkey:
    """Ctrl+Alt+Esc 生命周期持有者；start/close 幂等，回调只发 stop 请求。"""

    def __init__(self, api: HotkeyApi, on_stop: Callable[[], None]) -> None:
        self._api = api
        self._on_stop = on_stop
        self._lock = threading.Lock()
        self._started = False
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            registered = self._api.register(lambda: self._dispatch_for_test(EMERGENCY_HOTKEY_LABEL))
            if not registered:
                self._available = False
                raise ComputerControlError(
                    "computer_hotkey_unavailable",
                    "无法注册全局紧急停止热键 Ctrl+Alt+Esc",
                )
            self._started = True
            self._available = True
        _set_active_hotkey(self)

    def close(self) -> None:
        global _active_hotkey
        with self._lock:
            if not self._started:
                return
            self._started = False
        try:
            self._api.unregister()
        finally:
            self._available = False
            with _registry_lock:
                if _active_hotkey is self:
                    _active_hotkey = None

    def _dispatch_for_test(self, hotkey_label: str) -> None:
        """测试与消息线程共用的分发入口：仅匹配固定热键时调用 on_stop。"""
        if hotkey_label != EMERGENCY_HOTKEY_LABEL:
            return
        self._on_stop()


class WindowsHotkeyApi:
    """默认 Win32 实现：独立消息线程注册 Ctrl+Alt+Esc 并泵 WM_HOTKEY。"""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ComputerControlError(
                "computer_unsupported_os",
                "紧急停止热键目前仅支持 Windows",
            )
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._registered = False
        self._ready = threading.Event()
        self._on_hotkey: Callable[[], None] | None = None

    def register(self, on_hotkey: Callable[[], None]) -> bool:
        with self._state_lock:
            if self._thread is not None:
                return self._registered
            self._on_hotkey = on_hotkey
            self._registered = False
            self._thread_id = 0
            self._ready.clear()
            thread = threading.Thread(
                target=self._message_loop,
                name="computer-emergency-hotkey",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        if not self._ready.wait(timeout=10):
            self.unregister()
            return False
        if not self._registered:
            self.unregister()
            return False
        return True

    def unregister(self) -> None:
        with self._state_lock:
            thread = self._thread
            thread_id = self._thread_id
            self._thread = None
            self._thread_id = 0
            self._registered = False
        if thread is None:
            return
        if thread_id and thread.is_alive():
            self._post_quit_message(thread_id)
        thread.join(timeout=10)

    def _post_quit_message(self, thread_id: int) -> None:
        """发退出消息让消息线程自己 UnregisterHotKey 后退出；失败必须留痕。"""
        try:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(thread_id, _WM_APP_QUIT, 0, 0)
        except Exception:
            _LOGGER.exception("紧急停止热键退出消息发送失败")

    def _message_loop(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        thread_id = kernel32.GetCurrentThreadId()
        ok = user32.RegisterHotKey(
            None, _HOTKEY_ID, _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT, _VK_ESCAPE
        )
        with self._state_lock:
            self._thread_id = thread_id
            self._registered = bool(ok)
        self._ready.set()
        if not ok:
            return
        message = wintypes.MSG()
        try:
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result in (0, -1):
                    break
                if message.message == _WM_HOTKEY and message.wParam == _HOTKEY_ID:
                    self._invoke_on_hotkey()
                elif message.message == _WM_APP_QUIT:
                    break
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)

    def _invoke_on_hotkey(self) -> None:
        """消息泵保护：回调异常不拖垮热键线程，但必须 logging 留痕。"""
        callback = self._on_hotkey
        if callback is None:
            return
        try:
            callback()
        except Exception:
            _LOGGER.exception("紧急停止热键回调执行失败")
