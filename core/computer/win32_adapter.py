from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from core.computer.models import ComputerControlError

_LOGGER = logging.getLogger(__name__)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

_UOI_NAME = 2
_GENERIC_READ = 0x80000000
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
_ABSOLUTE_RANGE = 65535
_GA_ROOT = 2

KEY_NAME_TO_VK = {
    "CTRL": 0x11,
    "ALT": 0x12,
    "SHIFT": 0x10,
    "META": 0x5B,
    "ENTER": 0x0D,
    "TAB": 0x09,
    "ESC": 0x1B,
    "SPACE": 0x20,
    "BACKSPACE": 0x08,
    "DELETE": 0x2E,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,
}
KEY_NAME_TO_VK.update({chr(code): code for code in range(ord("A"), ord("Z") + 1)})
KEY_NAME_TO_VK.update({str(digit): ord(str(digit)) for digit in range(10)})
KEY_NAME_TO_VK.update({f"F{number}": 0x70 + number - 1 for number in range(1, 13)})

_MOUSE_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

_MOUSE_ACTIONS = frozenset(
    {"move", "click", "double_click", "down", "up", "wheel", "hwheel", "drag"}
)


@dataclass(frozen=True)
class MouseEventDescriptor:
    """一个鼠标输入事件；x/y 为 0-65535 归一化虚拟桌面坐标（仅 MOVE 使用）。"""

    x: int
    y: int
    flags: int
    data: int = 0


@dataclass(frozen=True)
class KeyEventDescriptor:
    """一个键盘输入事件；vk=0 + KEYEVENTF_UNICODE 时 scan 为 UTF-16 代码单元。"""

    vk: int
    scan: int
    flags: int


_ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUT_UNION)]


_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _ctypes_set_clipboard_text(text: str) -> None:
    import time as _time

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    # 64 位下 windll 默认 restype 是 32 位 int，会把 GlobalAlloc/SetClipboardData
    # 返回的指针宽度句柄截断（会话 156 实测 GlobalLock 失败），必须显式签名。
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = (wintypes.UINT, ctypes.c_void_p)
    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    data = text.encode("utf-16-le") + b"\x00\x00"
    for _attempt in range(10):
        if user32.OpenClipboard(0):
            handle = None
            try:
                user32.EmptyClipboard()
                handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
                if not handle:
                    raise OSError("GlobalAlloc 失败")
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    raise OSError("GlobalLock 失败")
                try:
                    ctypes.memmove(pointer, data, len(data))
                finally:
                    kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
                    raise OSError("SetClipboardData 失败")
                return  # 成功后句柄归系统所有，不得 GlobalFree
            except Exception as error:
                if handle:
                    try:
                        kernel32.GlobalFree(handle)
                    except Exception:
                        pass
                raise ComputerControlError(
                    "computer_backend_error", f"写入剪贴板失败：{error}"
                )
            finally:
                user32.CloseClipboard()
        _time.sleep(0.03)
    raise ComputerControlError(
        "computer_backend_error", "打开剪贴板失败（被其他进程长期占用）"
    )


def _ctypes_send_input(events: list) -> int:
    user32 = ctypes.windll.user32
    count = len(events)
    array = (_INPUT * count)()
    for index, event in enumerate(events):
        if isinstance(event, MouseEventDescriptor):
            array[index].type = INPUT_MOUSE
            array[index].union.mi = _MOUSEINPUT(event.x, event.y, event.data, event.flags, 0, 0)
        elif isinstance(event, KeyEventDescriptor):
            array[index].type = INPUT_KEYBOARD
            array[index].union.ki = _KEYBDINPUT(event.vk, event.scan, event.flags, 0, 0)
        else:
            raise ComputerControlError(
                "computer_invalid_action",
                f"未知输入事件类型: {type(event).__name__}",
            )
    return int(user32.SendInput(count, array, ctypes.sizeof(_INPUT)))


def _ctypes_ensure_dpi_awareness() -> bool:
    user32 = ctypes.windll.user32
    user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    pmv2 = ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    if user32.AreDpiAwarenessContextsEqual(user32.GetThreadDpiAwarenessContext(), pmv2):
        return True
    if user32.SetProcessDpiAwarenessContext(pmv2):
        if user32.AreDpiAwarenessContextsEqual(
            user32.GetThreadDpiAwarenessContext(), pmv2
        ):
            return True
    system = ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_SYSTEM_AWARE)
    return bool(
        user32.AreDpiAwarenessContextsEqual(
            user32.GetThreadDpiAwarenessContext(), system
        )
    )


def _ctypes_input_desktop_name() -> str | None:
    user32 = ctypes.windll.user32
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    handle = user32.OpenInputDesktop(0, False, _GENERIC_READ)
    if not handle:
        return None
    try:
        needed = wintypes.DWORD(0)
        user32.GetUserObjectInformationW(handle, _UOI_NAME, None, 0, ctypes.byref(needed))
        if not needed.value:
            return None
        buffer = ctypes.create_unicode_buffer(needed.value // 2 + 1)
        if not user32.GetUserObjectInformationW(
            handle, _UOI_NAME, buffer, needed.value, ctypes.byref(needed)
        ):
            return None
        return str(buffer.value)
    finally:
        user32.CloseDesktop(handle)


def _ctypes_foreground_window() -> int:
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return int(user32.GetForegroundWindow() or 0)


def _ctypes_window_from_point(x: int, y: int) -> int:
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.restype = ctypes.c_void_p
    user32.WindowFromPoint.argtypes = (wintypes.POINT,)
    user32.GetAncestor.restype = ctypes.c_void_p
    user32.GetAncestor.argtypes = (ctypes.c_void_p, wintypes.UINT)
    point = wintypes.POINT(int(x), int(y))
    hwnd = int(user32.WindowFromPoint(point) or 0)
    if not hwnd:
        return 0
    root = int(user32.GetAncestor(hwnd, _GA_ROOT) or 0)
    return root or hwnd


def _ctypes_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    user32 = ctypes.windll.user32
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    rect = wintypes.RECT()
    if not user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
        return None
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _prototype(fn, argtypes, restype) -> None:
    """ctypes 函数需要原型；测试桩是普通方法，设不上就跳过。"""
    if argtypes is not None:
        try:
            fn.argtypes = argtypes
        except (AttributeError, TypeError):
            pass
    try:
        fn.restype = restype
    except (AttributeError, TypeError):
        pass


def _ctypes_set_foreground_window(hwnd: int) -> bool:
    """把目标窗口置前，并以 GetForegroundWindow 作为唯一成功标准。

    第一次 SetForegroundWindow 被系统拒绝时，短暂把当前线程挂到前台线程上，
    BringWindowToTop 后再置前，并在 finally 里解开。不发送 Alt 键。
    核对失败返回 False，绝不把 API 的返回值当成已经置前。
    """
    user32 = ctypes.windll.user32
    target = int(hwnd)
    _prototype(user32.SetForegroundWindow, (wintypes.HWND,), wintypes.BOOL)
    user32.SetForegroundWindow(target)
    if _ctypes_foreground_window() == target:
        return True
    kernel32 = ctypes.windll.kernel32
    _prototype(
        user32.GetWindowThreadProcessId,
        (wintypes.HWND, ctypes.c_void_p),
        wintypes.DWORD,
    )
    _prototype(
        user32.AttachThreadInput,
        (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL),
        wintypes.BOOL,
    )
    _prototype(user32.BringWindowToTop, (wintypes.HWND,), wintypes.BOOL)
    _prototype(kernel32.GetCurrentThreadId, None, wintypes.DWORD)
    foreground = _ctypes_foreground_window()
    our_thread = int(kernel32.GetCurrentThreadId() or 0)
    foreground_thread = (
        int(user32.GetWindowThreadProcessId(int(foreground), None) or 0)
        if foreground
        else 0
    )
    attached = False
    try:
        if foreground_thread and our_thread and foreground_thread != our_thread:
            attached = bool(
                user32.AttachThreadInput(our_thread, foreground_thread, True)
            )
        user32.BringWindowToTop(target)
        user32.SetForegroundWindow(target)
    except Exception:
        _LOGGER.exception("前台二次获取失败")
    finally:
        if attached:
            try:
                user32.AttachThreadInput(our_thread, foreground_thread, False)
            except Exception:
                _LOGGER.exception("解除前台线程附着失败")
    return _ctypes_foreground_window() == target


def _ctypes_window_title(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return str(buffer.value)


def _ctypes_top_window_titles() -> tuple[str, ...]:
    """枚举可见且有标题的顶层窗口标题（wait_for window_active 的只读探测）。

    ctypes EnumWindows + IsWindowVisible + GetWindowTextW；隐藏窗口、无标题窗口
    （大量壳窗口/工具窗口）一律跳过。HWND 只在本函数内使用，绝不外传。
    """
    user32 = ctypes.windll.user32
    titles: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _on_window(hwnd, _lparam):  # noqa: ANN001 - ctypes 回调签名固定
        if not user32.IsWindowVisible(hwnd):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = str(buffer.value or "").strip()
        if title:
            titles.append(title)
        return True

    user32.EnumWindows(_on_window, 0)
    return tuple(titles)


def _ctypes_system_metrics(index: int) -> int:
    return int(ctypes.windll.user32.GetSystemMetrics(index))


def _default_hotkey_probe() -> bool:
    """热键探测：已被进程持有则直接可用，否则注册成功后立即注销。"""
    from core.computer.hotkey import (
        EmergencyStopHotkey,
        WindowsHotkeyApi,
        get_emergency_hotkey_snapshot,
    )

    if get_emergency_hotkey_snapshot().available:
        return True
    try:
        probe = EmergencyStopHotkey(WindowsHotkeyApi(), on_stop=lambda: None)
    except ComputerControlError:
        return False
    try:
        probe.start()
    except ComputerControlError:
        return False
    finally:
        probe.close()
    return True


class Win32InputAdapter:
    """SendInput 输入适配器；构造参数注入全部底层 seam，默认实现走 ctypes。"""

    def __init__(
        self,
        *,
        send_input: Callable[[list], int] | None = None,
        platform: str | Callable[[], str] | None = None,
        ensure_dpi_awareness: Callable[[], bool] | None = None,
        get_input_desktop_name: Callable[[], str | None] | None = None,
        get_foreground_window: Callable[[], int] | None = None,
        get_window_title: Callable[[int], str] | None = None,
        get_system_metrics: Callable[[int], int] | None = None,
        hotkey_check: Callable[[], bool] | None = None,
        set_clipboard_text: Callable[[str], None] | None = None,
        window_from_point: Callable[[int, int], int] | None = None,
        set_foreground_window: Callable[[int], bool] | None = None,
        list_top_window_titles: Callable[[], tuple[str, ...]] | None = None,
        window_rect: Callable[[int], tuple[int, int, int, int] | None] | None = None,
    ) -> None:
        self._send_input = send_input if send_input is not None else _ctypes_send_input
        if platform is None:
            self._get_platform: Callable[[], str] = lambda: sys.platform
        elif callable(platform):
            self._get_platform = platform
        else:
            self._get_platform = lambda: platform
        self._ensure_dpi_awareness = (
            ensure_dpi_awareness if ensure_dpi_awareness is not None else _ctypes_ensure_dpi_awareness
        )
        self._get_input_desktop_name = (
            get_input_desktop_name
            if get_input_desktop_name is not None
            else _ctypes_input_desktop_name
        )
        self._get_foreground_window = (
            get_foreground_window
            if get_foreground_window is not None
            else _ctypes_foreground_window
        )
        self._get_window_title = (
            get_window_title if get_window_title is not None else _ctypes_window_title
        )
        self._get_system_metrics = (
            get_system_metrics if get_system_metrics is not None else _ctypes_system_metrics
        )
        self._hotkey_check = (
            hotkey_check if hotkey_check is not None else _default_hotkey_probe
        )
        self._set_clipboard = (
            set_clipboard_text if set_clipboard_text is not None else _ctypes_set_clipboard_text
        )
        self._window_from_point = (
            window_from_point if window_from_point is not None else _ctypes_window_from_point
        )
        self._set_foreground = (
            set_foreground_window if set_foreground_window is not None else _ctypes_set_foreground_window
        )
        self._list_top_window_titles = (
            list_top_window_titles
            if list_top_window_titles is not None
            else _ctypes_top_window_titles
        )
        self._window_rect = window_rect if window_rect is not None else _ctypes_window_rect
        self._pressed_keys: list[str] = []
        self._pressed_buttons: set[str] = set()
        self._input_gate = threading.Lock()
        self._input_stop = threading.Event()

    def request_stop(self) -> None:
        """Reject new input batches after the currently submitted batch completes."""
        self._input_stop.set()
        with self._input_gate:
            pass

    def preflight(self) -> None:
        """固定顺序 fail-closed 检查；任何失败映射稳定错误码，不提权不重启。"""
        if self._get_platform() != "win32":
            raise ComputerControlError(
                "computer_unsupported_os",
                "完整电脑操控目前仅支持 Windows",
            )
        if not self._ensure_dpi_awareness():
            raise ComputerControlError(
                "computer_backend_start_failed",
                "无法设置或确认 Per-Monitor V2 DPI awareness，不会在错误坐标系中继续",
            )
        self._check_interactive_desktop()
        if not self._hotkey_check():
            raise ComputerControlError(
                "computer_hotkey_unavailable",
                "无法注册全局紧急停止热键 Ctrl+Alt+Esc",
            )

    def ensure_interactive(self) -> None:
        """每次桌面动作前重新检查 input desktop 与前台窗口（设计 13.4）。"""
        self._check_interactive_desktop()

    def _check_interactive_desktop(self) -> None:
        name = self._get_input_desktop_name()
        if name is None:
            raise ComputerControlError(
                "computer_noninteractive_session",
                "无法打开当前 input desktop（非交互式会话），电脑操控不可用",
            )
        if name != "Default":
            raise ComputerControlError(
                "computer_secure_desktop",
                f"当前 input desktop 为 {name!r}（锁屏或安全桌面），停止操控",
            )
        if not self._get_foreground_window():
            raise ComputerControlError(
                "computer_secure_desktop",
                "当前没有前台窗口（可能在锁屏或安全桌面），停止操控",
            )

    def virtual_screen_rect(self) -> tuple[int, int, int, int]:
        """(origin_x, origin_y, width, height)；原点允许多显示器负坐标。"""
        metrics = self._get_system_metrics
        origin_x = int(metrics(SM_XVIRTUALSCREEN))
        origin_y = int(metrics(SM_YVIRTUALSCREEN))
        width = metrics(SM_CXVIRTUALSCREEN)
        height = metrics(SM_CYVIRTUALSCREEN)
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            raise ComputerControlError(
                "computer_backend_start_failed",
                "无法读取有效的虚拟桌面尺寸",
            )
        return origin_x, origin_y, width, height

    def foreground_window_title(self) -> str:
        hwnd = self._get_foreground_window()
        if not hwnd:
            return ""
        title = self._get_window_title(hwnd)
        return title if isinstance(title, str) else ""

    def foreground_window_id(self) -> int:
        hwnd = self._get_foreground_window()
        return int(hwnd) if hwnd else 0

    def foreground_window_rect(self) -> tuple[int, int, int, int] | None:
        """前台窗口屏幕矩形 (left, top, right, bottom)；读不到时返回 None。"""
        hwnd = self.foreground_window_id()
        if not hwnd:
            return None
        try:
            rect = self._window_rect(int(hwnd))
        except Exception:
            _LOGGER.exception("读取前台窗口矩形失败")
            return None
        if not rect or len(tuple(rect)) != 4:
            return None
        left, top, right, bottom = (int(value) for value in rect)
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom

    def list_top_window_titles(self) -> tuple[str, ...]:
        """枚举可见且有标题的顶层窗口标题（wait_for window_active 只读探测）。

        best-effort：枚举失败返回空元组并留痕——条件等待会继续轮询到超时，
        绝不因为一次枚举异常直接判失败。只返回非空字符串标题。
        """
        try:
            titles = self._list_top_window_titles()
        except Exception:
            _LOGGER.exception("枚举顶层窗口标题失败")
            return ()
        return tuple(
            str(title) for title in (titles or ()) if isinstance(title, str) and title
        )

    def window_from_point(self, x: int, y: int) -> int:
        """物理坐标点所属顶层窗口 HWND（通用化重构 Task C 输入前置激活）。

        best-effort：查询失败返回 0 并留痕，绝不抛错阻断输入。
        """
        try:
            return int(self._window_from_point(int(x), int(y)) or 0)
        except Exception:
            _LOGGER.exception("WindowFromPoint 查询失败")
            return 0

    def set_foreground_window(self, hwnd: int) -> bool:
        """把目标窗口提到前台（通用化重构 Task C 输入前置激活）。

        best-effort：SetForegroundWindow 被拒绝时会短暂附着前台线程再试一次。
        仍对不上实际前台句柄则返回 False 并留痕，绝不抛错阻断调用方。
        """
        try:
            return bool(self._set_foreground(int(hwnd)))
        except Exception:
            _LOGGER.exception("SetForegroundWindow 激活失败")
            return False

    def _deliver(self, events: list, *, release_only: bool = False) -> None:
        if release_only and any(
            isinstance(event, KeyEventDescriptor)
            and not event.flags & KEYEVENTF_KEYUP
            or isinstance(event, MouseEventDescriptor)
            and any(event.flags & down for down, _up in _MOUSE_BUTTON_FLAGS.values())
            for event in events
        ):
            raise ComputerControlError(
                "computer_invalid_action", "停止后只允许释放已按下的键鼠输入"
            )
        with self._input_gate:
            if self._input_stop.is_set() and not release_only:
                raise ComputerControlError(
                    "computer_cancelled", "桌面输入已停止，拒绝提交新输入批次"
                )
            sent = self._send_input(list(events))
        if sent == len(events):
            return
        self._release_batch_prefix(events[: max(0, int(sent))])
        raise ComputerControlError(
            "computer_input_rejected",
            f"SendInput 只接受了 {sent}/{len(events)} 个输入事件（可能被 UIPI 拦截）",
        )

    def _release_batch_prefix(self, events: list) -> None:
        pending: list = []
        for event in events:
            if isinstance(event, KeyEventDescriptor):
                if event.flags & KEYEVENTF_KEYUP:
                    pending = [
                        item
                        for item in pending
                        if not (
                            isinstance(item, KeyEventDescriptor)
                            and item.vk == event.vk
                            and item.scan == event.scan
                        )
                    ]
                else:
                    pending.append(event)
            elif isinstance(event, MouseEventDescriptor):
                for button, (down_flag, up_flag) in _MOUSE_BUTTON_FLAGS.items():
                    if event.flags & down_flag:
                        pending.append(MouseEventDescriptor(0, 0, up_flag))
                    elif event.flags & up_flag:
                        pending = [
                            item
                            for item in pending
                            if not (
                                isinstance(item, MouseEventDescriptor)
                                and item.flags == up_flag
                            )
                        ]
        for event in reversed(pending):
            if isinstance(event, KeyEventDescriptor):
                release = KeyEventDescriptor(event.vk, event.scan, event.flags | KEYEVENTF_KEYUP)
            else:
                release = event
            try:
                self._send_input([release])
            except Exception:
                _LOGGER.exception("SendInput 部分失败后的释放补偿失败")

    def _key_down(self, name: str, vk: int) -> None:
        self._deliver([KeyEventDescriptor(vk=vk, scan=0, flags=0)])
        self._pressed_keys.append(name)

    def _key_up(self, name: str, vk: int) -> None:
        self._deliver([KeyEventDescriptor(vk=vk, scan=0, flags=KEYEVENTF_KEYUP)])
        try:
            self._pressed_keys.remove(name)
        except ValueError:
            pass

    def _key_up_quietly(
        self, name: str, vk: int, *, release_only: bool = True
    ) -> None:
        try:
            if release_only and self._input_stop.is_set():
                self._deliver(
                    [KeyEventDescriptor(vk=vk, scan=0, flags=KEYEVENTF_KEYUP)],
                    release_only=True,
                )
                try:
                    self._pressed_keys.remove(name)
                except ValueError:
                    pass
            else:
                self._key_up(name, vk)
        except Exception:
            _LOGGER.exception("释放按键失败: %s", name)

    @staticmethod
    def _map_key(name: object) -> tuple[str, int]:
        if not isinstance(name, str) or name not in KEY_NAME_TO_VK:
            raise ComputerControlError(
                "computer_invalid_action",
                f"不支持的键名: {name!r}",
            )
        return name, KEY_NAME_TO_VK[name]

    def send_keys(self, keys) -> None:
        """组合键：按顺序按下、逆序释放；任何失败都在 finally 中释放已按下键。"""
        if not isinstance(keys, (list, tuple)) or not keys:
            raise ComputerControlError("computer_invalid_action", "keypress 需要至少一个键")
        mapped = [self._map_key(name) for name in keys]
        names = {name for name, _vk in mapped}
        try:
            for name, vk in mapped:
                self._key_down(name, vk)
            for name, vk in reversed(mapped):
                self._key_up(name, vk)
        finally:
            for name in [item for item in list(self._pressed_keys) if item in names]:
                self._key_up_quietly(name, KEY_NAME_TO_VK[name])

    def send_text(self, text: str) -> None:
        """Unicode 文本：每个 UTF-16 代码单元一对 KEYEVENTF_UNICODE down/up。"""
        if not isinstance(text, str) or not text:
            raise ComputerControlError("computer_invalid_action", "type_text 需要非空文本")
        for char in text:
            units = char.encode("utf-16-le")
            for offset in range(0, len(units), 2):
                code_unit = int.from_bytes(units[offset : offset + 2], "little")
                self._deliver(
                    [
                        KeyEventDescriptor(vk=0, scan=code_unit, flags=KEYEVENTF_UNICODE),
                        KeyEventDescriptor(
                            vk=0,
                            scan=code_unit,
                            flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        ),
                    ]
                )

    def set_clipboard_text(self, text: str) -> None:
        """把 Unicode 文本写入系统剪贴板，供 type_text 中文剪贴板路径使用。"""
        if not isinstance(text, str) or not text:
            raise ComputerControlError("computer_invalid_action", "剪贴板文本必须非空")
        try:
            self._set_clipboard(text)
        except ComputerControlError:
            raise
        except OSError as error:
            raise ComputerControlError(
                "computer_backend_error", f"写入剪贴板失败：{error}"
            ) from error

    def send_mouse(
        self,
        *,
        action: str,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        delta: int = 0,
        path=(),
        modifiers=(),
    ) -> None:
        """鼠标动作统一入口；x/y 与 path 均为虚拟桌面物理像素坐标。"""
        if action not in _MOUSE_ACTIONS:
            raise ComputerControlError(
                "computer_invalid_action",
                f"未知鼠标动作: {action!r}",
            )
        if button not in _MOUSE_BUTTON_FLAGS:
            raise ComputerControlError(
                "computer_invalid_action",
                f"button 只支持 left/right/middle，收到 {button!r}",
            )
        mapped_modifiers = [self._map_key(name) for name in modifiers]
        pressed: list[tuple[str, int]] = []
        try:
            for name, vk in mapped_modifiers:
                self._key_down(name, vk)
                pressed.append((name, vk))
            self._dispatch_mouse(action, x=x, y=y, button=button, delta=delta, path=path)
        finally:
            for name, vk in reversed(pressed):
                self._key_up_quietly(name, vk, release_only=True)

    def _dispatch_mouse(self, action: str, *, x, y, button: str, delta: int, path) -> None:
        down_flag, up_flag = _MOUSE_BUTTON_FLAGS[button]
        if action == "move":
            self._deliver([self._absolute_move(x, y)])
        elif action == "click":
            self._deliver(
                [
                    self._absolute_move(x, y),
                    MouseEventDescriptor(0, 0, down_flag),
                    MouseEventDescriptor(0, 0, up_flag),
                ]
            )
        elif action == "double_click":
            self._deliver(
                [
                    self._absolute_move(x, y),
                    MouseEventDescriptor(0, 0, down_flag),
                    MouseEventDescriptor(0, 0, up_flag),
                    MouseEventDescriptor(0, 0, down_flag),
                    MouseEventDescriptor(0, 0, up_flag),
                ]
            )
        elif action == "down":
            self._deliver([MouseEventDescriptor(0, 0, down_flag)])
            self._pressed_buttons.add(button)
        elif action == "up":
            self._deliver([MouseEventDescriptor(0, 0, up_flag)])
            self._pressed_buttons.discard(button)
        elif action == "wheel":
            if type(delta) is not int:
                raise ComputerControlError("computer_invalid_action", "wheel delta 必须是整数")
            self._deliver([MouseEventDescriptor(0, 0, MOUSEEVENTF_WHEEL, -delta * WHEEL_DELTA)])
        elif action == "hwheel":
            if type(delta) is not int:
                raise ComputerControlError("computer_invalid_action", "hwheel delta 必须是整数")
            self._deliver([MouseEventDescriptor(0, 0, MOUSEEVENTF_HWHEEL, delta * WHEEL_DELTA)])
        elif action == "drag":
            self._drag(path, button, down_flag, up_flag)

    def _drag(self, path, button: str, down_flag: int, up_flag: int) -> None:
        points = list(path)
        if len(points) < 2:
            raise ComputerControlError("computer_invalid_action", "drag 需要至少 2 个 path 点")
        for point in points:
            if (
                not isinstance(point, (list, tuple))
                or len(point) != 2
                or type(point[0]) is not int
                or type(point[1]) is not int
            ):
                raise ComputerControlError(
                    "computer_invalid_action",
                    "drag path 的每个点必须是两个整数坐标",
                )
        self._deliver([self._absolute_move(points[0][0], points[0][1])])
        self._deliver([MouseEventDescriptor(0, 0, down_flag)])
        self._pressed_buttons.add(button)
        try:
            for point_x, point_y in points[1:]:
                self._deliver([self._absolute_move(point_x, point_y)])
        finally:
            try:
                self._deliver(
                    [MouseEventDescriptor(0, 0, up_flag)], release_only=True
                )
            except Exception:
                _LOGGER.exception("拖拽收尾释放鼠标键失败: %s", button)
                raise
            else:
                self._pressed_buttons.discard(button)

    def _absolute_move(self, x: int, y: int) -> MouseEventDescriptor:
        if type(x) is not int or type(y) is not int:
            raise ComputerControlError("computer_invalid_action", "坐标 x/y 必须是整数")
        origin_x, origin_y, width, height = self.virtual_screen_rect()
        if not (origin_x <= x < origin_x + width and origin_y <= y < origin_y + height):
            raise ComputerControlError(
                "computer_coordinate_out_of_bounds",
                f"物理坐标 ({x}, {y}) 超出虚拟桌面半开区间 "
                f"[{origin_x}, {origin_x + width}) x [{origin_y}, {origin_y + height})",
            )
        norm_x = (
            round((x - origin_x) * _ABSOLUTE_RANGE / (width - 1)) if width > 1 else 0
        )
        norm_y = (
            round((y - origin_y) * _ABSOLUTE_RANGE / (height - 1)) if height > 1 else 0
        )
        return MouseEventDescriptor(
            x=norm_x,
            y=norm_y,
            flags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        )

    def release_all(self) -> None:
        with self._input_gate:
            pressed_keys = list(self._pressed_keys)
            pressed_buttons = sorted(self._pressed_buttons)
            events: list = []
            for name in reversed(pressed_keys):
                events.append(
                    KeyEventDescriptor(
                        vk=KEY_NAME_TO_VK[name], scan=0, flags=KEYEVENTF_KEYUP
                    )
                )
            for button in pressed_buttons:
                events.append(MouseEventDescriptor(0, 0, _MOUSE_BUTTON_FLAGS[button][1]))
            if not events:
                return
            try:
                sent = self._send_input(events)
            except Exception as error:
                raise ComputerControlError(
                    "computer_cleanup_failed",
                    f"释放已按下的输入失败：{type(error).__name__}",
                ) from error
            if sent != len(events):
                raise ComputerControlError(
                    "computer_input_rejected",
                    f"释放输入只接受了 {sent}/{len(events)} 个事件，按键状态仍待确认",
                )
            self._pressed_keys.clear()
            self._pressed_buttons.clear()
