"""Win32 SendInput 输入适配器（设计 13.2-13.4、21）。

ctypes 封装 Win32 SendInput。所有底层调用点（SendInput、DPI awareness、
input desktop、foreground window、GetSystemMetrics、热键探测）都可通过构造
参数注入，pytest 绝不真实移动鼠标或发送键盘；默认实现只在生产/人工 smoke
路径惰性触碰 ctypes.windll。

preflight() 顺序固定（fail-closed）：
平台 win32 → 设置/确认 Per-Monitor V2 DPI awareness → input desktop 名称为
Default → 存在 foreground window → 紧急停止热键可注册。任何失败映射稳定
错误码，绝不请求管理员提权、绝不重启进程。

SendInput 返回数量少于请求数量即 computer_input_rejected，并按已成功前缀
尽力释放本批次已按下的键/鼠标键；组合键与 drag 在 finally 中释放已按下
状态。鼠标绝对坐标使用 MOUSEEVENTF_VIRTUALDESK|MOUSEEVENTF_ABSOLUTE，
Unicode 文本使用 KEYEVENTF_UNICODE。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from core.computer.models import ComputerControlError

_LOGGER = logging.getLogger(__name__)

# ---------------- Win32 常量 ----------------

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

# 统一动作键名 → 虚拟键码（登记制；与 core/computer/actions.py 的键名白名单对齐）。
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


# ---------------- 事件描述符（send_input seam 的输入形状） ----------------


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


# ---------------- ctypes INPUT 结构（仅默认 send_input 使用） ----------------

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


# ---------------- 默认 seam 实现（只在生产路径惰性触碰 windll） ----------------


def _ctypes_send_input(events: list) -> int:
    """默认 SendInput：把事件描述符转换为 INPUT 数组，返回成功注入的数量。"""
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
    """确认进程为 Per-Monitor V2；已是则只验证，否则显式设置后再验证。

    设置失败时降级评估 System DPI aware（2026-09-05 GUI 开关死锁修复）：
    pywebview WinForms 后端启动即 SetProcessDPIAware()，把 GUI 进程锁死在
    System aware——进程 DPI 感知只能设置一次，此处再切 PMv2 必然失败。
    System aware 下桌面操控坐标链路依然自洽：GetSystemMetrics/截图 DC/
    SendInput 0-65535 归一化全部工作在物理像素桌面坐标系（DPI 虚拟化只
    影响 UNAWARE 进程与窗口呈现，不影响这些 API），因此放行。UNAWARE
    （系统缩放≠100% 时 metrics 被虚拟化）与 PMv1 仍拒绝（fail-closed）。
    """
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
    # PMv2 设置失败（GUI 进程已被 pywebview 锁死）：System aware 坐标自洽，放行
    system = ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_SYSTEM_AWARE)
    return bool(
        user32.AreDpiAwarenessContextsEqual(
            user32.GetThreadDpiAwarenessContext(), system
        )
    )


def _ctypes_input_desktop_name() -> str | None:
    """当前 input desktop 名称；打不开（非交互式会话）返回 None。"""
    user32 = ctypes.windll.user32
    # 64 位句柄防御（Task 16）：ctypes 默认 restype 是 c_int，会把 64 位
    # HDESK 截断成 32 位——CloseDesktop 随即拿到无效句柄（句柄泄漏）。
    # 显式声明 c_void_p，CloseDesktop 的参数同样按指针宽度传递。
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
    # 64 位窗口句柄防御（Task 16）：默认 c_int restype 会截断 HWND。
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return int(user32.GetForegroundWindow() or 0)


def _ctypes_window_title(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return str(buffer.value)


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


# ---------------- 适配器 ----------------


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
        self._pressed_keys: list[str] = []
        self._pressed_buttons: set[str] = set()

    # ---------------- preflight 与运行期桌面检查 ----------------

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

    # ---------------- 底层发送与部分失败恢复 ----------------

    def _deliver(self, events: list) -> None:
        """发送一批事件；SendInput 部分成功时按已生效前缀尽力释放后报错。"""
        sent = self._send_input(list(events))
        if sent == len(events):
            return
        self._release_batch_prefix(events[: max(0, int(sent))])
        raise ComputerControlError(
            "computer_input_rejected",
            f"SendInput 只接受了 {sent}/{len(events)} 个输入事件（可能被 UIPI 拦截）",
        )

    def _release_batch_prefix(self, events: list) -> None:
        """部分 SendInput 成功后，为前缀中未配对的 down 事件补发 up（尽力而为）。"""
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

    def _key_up_quietly(self, name: str, vk: int) -> None:
        try:
            self._key_up(name, vk)
        except Exception:
            # 释放失败保留按下标记，供 release_all 重试（与 browser backend 一致）。
            _LOGGER.exception("释放按键失败: %s", name)

    @staticmethod
    def _map_key(name: object) -> tuple[str, int]:
        if not isinstance(name, str) or name not in KEY_NAME_TO_VK:
            raise ComputerControlError(
                "computer_invalid_action",
                f"不支持的键名: {name!r}",
            )
        return name, KEY_NAME_TO_VK[name]

    # ---------------- 公开输入 API ----------------

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
                self._key_up_quietly(name, vk)

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
            self._deliver([MouseEventDescriptor(0, 0, MOUSEEVENTF_WHEEL, delta * WHEEL_DELTA)])
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
            # try/finally 保证释放；up 失败时保留按下标记供 release_all 重试。
            try:
                self._deliver([MouseEventDescriptor(0, 0, up_flag)])
            except Exception:
                _LOGGER.exception("拖拽收尾释放鼠标键失败: %s", button)
                raise
            else:
                self._pressed_buttons.discard(button)

    def _absolute_move(self, x: int, y: int) -> MouseEventDescriptor:
        """物理像素 → 0-65535 归一化虚拟桌面坐标；发送前再次检查半开区间边界。"""
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
        """尽力释放全部已按下的键与鼠标键；释放失败只留痕不抛出。"""
        events: list = []
        for name in reversed(self._pressed_keys):
            events.append(
                KeyEventDescriptor(vk=KEY_NAME_TO_VK[name], scan=0, flags=KEYEVENTF_KEYUP)
            )
        for button in sorted(self._pressed_buttons):
            events.append(MouseEventDescriptor(0, 0, _MOUSE_BUTTON_FLAGS[button][1]))
        self._pressed_keys.clear()
        self._pressed_buttons.clear()
        if not events:
            return
        try:
            self._send_input(events)
        except Exception:
            _LOGGER.exception("释放全部输入状态失败")
