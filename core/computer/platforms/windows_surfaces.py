"""Windows 语义定位实现（R10）：窗口枚举/激活 + UIA 托盘图标候选。

线程模型与依赖边界：

- 所有 UI Automation/COM 对象在专属 COM 线程初始化、使用和释放，控件对象
  绝不跨线程传出（host 只返回脱敏的序号/名称/边界元组）。
- Win32 ctypes 的 HWND/HDC 句柄参数与返回值全部按指针宽度声明
  （wintypes.HWND 等），不存在只声明 GetForegroundWindow 而漏掉后续
  接收句柄 API 的问题。
- pytest 通过注入 fake host 验证，绝不触碰真实窗口、UIA 或鼠标键盘。
- UIA 初始化失败（系统不提供所需元素）时托盘路径显式报
  computer_surfaces_unavailable，绝不宣称 UIA 一定能解决。
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
from ctypes import wintypes

from core.computer.models import ComputerControlError
from core.computer.surfaces import (
    MAX_SURFACE_LABEL_CHARS,
    SurfaceCandidate,
)

_LOGGER = logging.getLogger(__name__)

_SW_RESTORE = 9

# UIA 稳定常量（属性/模式/控件类型 ID 由 UIAutomationCore 规范固定）。
_UIA_CONTROL_TYPE_PROPERTY_ID = 30003
_UIA_BUTTON_CONTROL_TYPE_ID = 50000
_UIA_LIST_ITEM_CONTROL_TYPE_ID = 50007
_UIA_INVOKE_PATTERN_ID = 10000
_UIA_TREE_SCOPE_DESCENDANTS = 0x4
_CUIAUTOMATION_CLSID = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"

# 托盘宿主窗口类名：主任务栏 / 副任务栏 / 隐藏图标展开区。
_TRAY_HOST_CLASSES = (
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow",
    # Windows 11 XAML 托盘飞出面板宿主（展开"显示隐藏的图标"后出现）。
    "TopLevelWindowForOverflowXamlIsland",
)

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None

_EnumWindowsProc = (
    ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    if _user32 is not None
    else None
)

if _user32 is not None:
    # 句柄型参数/返回值统一按指针宽度声明；EnumWindows 首参数为回调类型。
    _user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowRect.argtypes = [wintypes.HWND, wintypes.LPRECT]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.PostMessageW.restype = wintypes.BOOL


def browser_window_hints(pid: int) -> tuple[tuple[int, str, bool], ...]:
    """指定进程可见顶层窗口的标题提示（R5 受控页裁决的系统侧信号）。

    返回 (hwnd, title, is_foreground)；只读窗口标题，不读取任何窗口内容。
    """
    if _user32 is None:
        raise ComputerControlError(
            "computer_unsupported_os", "窗口标题提示目前仅支持 Windows"
        )
    foreground = int(_user32.GetForegroundWindow() or 0)
    results: list[tuple[int, str, bool]] = []

    def callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        owner = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if int(owner.value) != int(pid):
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if title.strip():
            results.append((int(hwnd), title, int(hwnd) == foreground))
        return True

    _user32.EnumWindows(_EnumWindowsProc(callback), 0)
    return tuple(results)


class _ComThread:
    """专属 COM 线程：所有 UIA 调用经有界命令队列 marshal 到该线程执行。"""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._init_error: BaseException | None = None
        self._uia = None
        self._thread = threading.Thread(
            target=self._main, name="computer-surface-uia", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=10)
        self._raise_if_unavailable()

    def _raise_if_unavailable(self) -> None:
        """线程死亡或初始化失败时立即抛出明确错误，绝不伪装成「调用超时」。"""
        if self._init_error is not None:
            raise ComputerControlError(
                "computer_surfaces_unavailable",
                f"UI Automation 不可用：{type(self._init_error).__name__}",
            )
        if not self._thread.is_alive():
            # 线程未启动或已退出：队列不会有人消费，任何投递都必然空等。
            raise ComputerControlError(
                "computer_surfaces_unavailable",
                "UI Automation 不可用：COM 线程已退出",
            )

    def call(self, fn):
        """在 COM 线程执行 fn(uia) 并返回结果；UIA 对象绝不离开该线程。"""
        # 投递前先确认线程可消费任务，避免把初始化失败/线程死亡伪装成超时。
        self._raise_if_unavailable()
        done = threading.Event()
        holder: dict = {}

        def job():
            try:
                holder["result"] = fn(self._uia)
            except BaseException as error:
                holder["error"] = error
            finally:
                done.set()

        self._queue.put(job)
        if not done.wait(timeout=10):
            # 线程可能在等待窗口内死亡：优先报告真实原因。
            self._raise_if_unavailable()
            raise ComputerControlError(
                "computer_surfaces_unavailable", "UI Automation 调用超时"
            )
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=5)

    def _main(self) -> None:
        try:
            try:
                import comtypes
                import comtypes.client

                comtypes.CoInitialize()
                comtypes.client.GetModule("UIAutomationCore.dll")
                from comtypes.gen.UIAutomationClient import IUIAutomation

                self._uia = comtypes.client.CreateObject(
                    _CUIAUTOMATION_CLSID, interface=IUIAutomation
                )
            except BaseException as error:
                # 初始化失败（comtypes 缺失、CoInitialize 失败等）必须落盘错误并
                # 唤醒等待方；绝不让异常逸出线程、留下 ready 永不置位的假象。
                self._init_error = error
                return
            finally:
                self._ready.set()
            while True:
                job = self._queue.get()
                if job is None:
                    return
                job()
        finally:
            try:
                import comtypes

                comtypes.CoUninitialize()
            except Exception:
                pass


class _DefaultSurfaceHost:
    """真实 Win32/UIA 访问；所有方法只返回脱敏元组，不暴露句柄/控件对象。"""

    def __init__(self) -> None:
        self._com: _ComThread | None = None
        self._com_lock = threading.Lock()

    # ---------------- 窗口（纯 Win32，可在调用线程执行） ----------------

    def enum_windows(self) -> list[tuple[int, str, int, tuple[int, int, int, int], bool]]:
        """可见且有标题的顶层窗口：(hwnd, title, pid, bounds, iconic)。"""
        if _user32 is None:
            raise ComputerControlError(
                "computer_unsupported_os", "语义定位目前仅支持 Windows"
            )
        results: list[tuple[int, str, int, tuple[int, int, int, int], bool]] = []

        def callback(hwnd, _lparam):
            if not _user32.IsWindowVisible(hwnd):
                return True
            length = _user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value[:MAX_SURFACE_LABEL_CHARS]
            if not title.strip():
                return True
            rect = wintypes.RECT()
            if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            pid = wintypes.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            results.append(
                (
                    int(hwnd),
                    title,
                    int(pid.value),
                    (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
                    bool(_user32.IsIconic(hwnd)),
                )
            )
            return True

        _user32.EnumWindows(_EnumWindowsProc(callback), 0)
        return results

    def window_bounds(self, hwnd: int) -> tuple[int, int, int, int] | None:
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
            return None
        return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

    def activate_window(self, hwnd: int) -> None:
        """ShowWindow 恢复 + SetForegroundWindow，随后验证实际前台身份。

        SetForegroundWindow 受系统限制；返回/验证失败显式报错，绝不通过
        后台盲输假装已激活。
        """
        handle = int(hwnd)
        if _user32.IsIconic(handle):
            _user32.ShowWindow(handle, _SW_RESTORE)
        _user32.SetForegroundWindow(handle)
        # GetForegroundWindow 可能返回 NULL（restype=HWND → None）。
        if int(_user32.GetForegroundWindow() or 0) != handle:
            raise ComputerControlError(
                "computer_action_failed",
                "激活窗口失败：系统拒绝将目标窗口置前台（SetForegroundWindow 受限）",
            )

    def current_foreground(self) -> int:
        return int(_user32.GetForegroundWindow() or 0)

    def close_window(self, hwnd: int) -> None:
        """礼貌关闭窗口（WM_CLOSE）：应用自行决定收起到托盘或退出。

        仅用于验收探针（窗口隐藏→托盘恢复矩阵）；不终止进程。
        """
        _WM_CLOSE = 0x0010
        _user32.PostMessageW(int(hwnd), _WM_CLOSE, 0, 0)

    # ---------------- 托盘图标（UIA，专属 COM 线程） ----------------

    def _com_thread(self) -> _ComThread:
        with self._com_lock:
            if self._com is None:
                self._com = _ComThread()
                self._com.start()
            return self._com

    def _find_tray_hosts(self) -> list[int]:
        hosts = []
        for class_name in _TRAY_HOST_CLASSES:
            hwnd = _user32.FindWindowW(class_name, None)
            if hwnd:
                hosts.append(int(hwnd))
        return hosts

    def list_tray_buttons(self) -> list[tuple[int, str, tuple[int, int, int, int], bool]]:
        """返回 (element_serial, name, bounds, can_invoke)；元素本体留在 COM 线程。

        每个元素的来源窗口类名记录在 ``_element_source``（诊断/验收用）。
        """
        hosts = self._find_tray_hosts()
        if not hosts:
            return []
        com = self._com_thread()
        class_names = {}
        for hwnd in hosts:
            buf = ctypes.create_unicode_buffer(64)
            _user32.GetClassNameW(hwnd, buf, 64)
            class_names[int(hwnd)] = buf.value

        def collect(uia):
            buttons = []
            registry: dict[int, object] = {}
            for hwnd in hosts:
                try:
                    element = uia.ElementFromHandle(hwnd)
                except Exception:
                    continue
                for control_type in (
                    _UIA_BUTTON_CONTROL_TYPE_ID,
                    _UIA_LIST_ITEM_CONTROL_TYPE_ID,
                ):
                    try:
                        condition = uia.CreatePropertyCondition(
                            _UIA_CONTROL_TYPE_PROPERTY_ID, control_type
                        )
                        found = element.FindAll(_UIA_TREE_SCOPE_DESCENDANTS, condition)
                    except Exception:
                        continue
                    for index in range(found.Length):
                        item = found.GetElement(index)
                        try:
                            name = str(item.CurrentName or "")[:MAX_SURFACE_LABEL_CHARS]
                            if not name.strip():
                                continue
                            rect = item.CurrentBoundingRectangle
                            bounds = (
                                int(rect.left),
                                int(rect.top),
                                int(rect.right - rect.left),
                                int(rect.bottom - rect.top),
                            )
                            if bounds[2] <= 0 or bounds[3] <= 0:
                                continue
                            can_invoke = False
                            try:
                                # 不支持的模式返回 NULL COM 指针而不抛错——
                                # 必须显式判空（comtypes NULL 指针为 falsy）。
                                can_invoke = bool(
                                    item.GetCurrentPattern(_UIA_INVOKE_PATTERN_ID)
                                )
                            except Exception:
                                can_invoke = False
                            # 元素引用只登记进本线程内的注册表并整体替换（旧
                            # 引用随之在本线程释放）；返回值只带序号与纯数据——
                            # COM 接口指针跨 apartment 传出/释放是未定义行为，
                            # 此前把 item 直接带出线程违反了本文件头部的不变量。
                            serial = id(item)
                            registry[serial] = item
                            buttons.append((hwnd, serial, name, bounds, can_invoke))
                        except Exception:
                            continue
            self._elements = registry
            return buttons

        raw = com.call(collect)
        # 序号→来源窗口类名（纯字符串，调用线程持有安全）
        buttons: list[tuple[int, str, tuple[int, int, int, int], bool]] = []
        self._element_source = {}
        for hwnd, serial, name, bounds, can_invoke in raw:
            self._element_source[serial] = class_names.get(hwnd, "")
            buttons.append((serial, name, bounds, can_invoke))
        return buttons

    def element_source(self, serial: int) -> str:
        """托盘候选的来源宿主窗口类名（如 NotifyIconOverflowWindow=隐藏区）。"""
        return getattr(self, "_element_source", {}).get(int(serial), "")

    def invoke_button(self, serial: int) -> None:
        com = self._com_thread()

        def invoke(_uia):
            # 注册表只在 COM 线程内读写（见 list_tray_buttons）
            item = getattr(self, "_elements", {}).get(int(serial))
            if item is None:
                raise ComputerControlError(
                    "computer_surface_unknown", "托盘候选已失效，请重新观察"
                )
            pattern = item.GetCurrentPattern(_UIA_INVOKE_PATTERN_ID)
            if not pattern:
                raise ComputerControlError(
                    "computer_invalid_action",
                    "该候选当前不提供 Invoke 模式，请改用 observe_region + 显式点击",
                )
            from comtypes.gen.UIAutomationClient import IUIAutomationInvokePattern

            invoke_pattern = pattern.QueryInterface(IUIAutomationInvokePattern)
            invoke_pattern.Invoke()

        try:
            com.call(invoke)
        except ComputerControlError:
            raise
        except Exception as error:
            raise ComputerControlError(
                "computer_action_failed",
                f"托盘候选语义调用失败：{type(error).__name__}",
            ) from error

    def element_bounds(self, serial: int) -> tuple[int, int, int, int] | None:
        com = self._com_thread()

        def bounds(_uia):
            # 注册表只在 COM 线程内读写（见 list_tray_buttons）
            item = getattr(self, "_elements", {}).get(int(serial))
            if item is None:
                return None
            rect = item.CurrentBoundingRectangle
            return (
                int(rect.left),
                int(rect.top),
                int(rect.right - rect.left),
                int(rect.bottom - rect.top),
            )

        try:
            return com.call(bounds)
        except Exception:
            return None

    def process_name(self, pid: int) -> str:
        try:
            import psutil

            return str(psutil.Process(int(pid)).name() or "")
        except Exception:
            return ""

    def close(self) -> None:
        with self._com_lock:
            com = self._com
            self._com = None
        if com is not None:
            # 元素注册表在 COM 线程内清空（引用随之在该线程释放），绝不从
            # 调用线程丢弃 COM 引用
            try:
                com.call(lambda _uia: setattr(self, "_elements", {}))
            except Exception:
                pass
            com.close()


class WindowsSurfaceLocator:
    """SurfaceLocator 的 Windows 实现；ref 绑定会话/代数，绝不接受伪造句柄。"""

    def __init__(self, host=None) -> None:
        self._host = host or _DefaultSurfaceHost()
        self._lock = threading.Lock()
        self._generation = 0
        self._candidates: dict[str, SurfaceCandidate] = {}
        # ref → host 侧原生键（hwnd 或 UIA 元素序号），绝不回传给模型。
        self._native_keys: dict[str, int] = {}
        self._closed = False

    # ---------------- 候选登记 ----------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise ComputerControlError(
                "computer_surfaces_unavailable", "语义定位器已关闭"
            )

    def _register(self, kind: str, label: str, bounds, operations, native_key: int) -> SurfaceCandidate:
        serial = len(self._candidates) + 1
        ref = f"surface-{self._generation}-{kind}-{serial}"
        candidate = SurfaceCandidate(
            ref=ref,
            kind=kind,
            label=label[:MAX_SURFACE_LABEL_CHARS],
            surface_generation=self._generation,
            supported_operations=tuple(operations),
            bounds=tuple(int(v) for v in bounds),
        )
        self._candidates[ref] = candidate
        self._native_keys[ref] = int(native_key)
        return candidate

    def list_windows(self) -> tuple[SurfaceCandidate, ...]:
        with self._lock:
            self._ensure_open()
            self._generation += 1
            self._candidates = {
                ref: c for ref, c in self._candidates.items() if c.kind != "window"
            }
            self._native_keys = {
                ref: key for ref, key in self._native_keys.items() if ref in self._candidates
            }
            rows = self._host.enum_windows()
            for hwnd, title, pid, bounds, iconic in rows:
                process = self._host.process_name(pid)
                label = f"{title}（{process}）" if process else title
                self._register("window", label, bounds, ("activate", "click_point"), hwnd)
            return tuple(c for c in self._candidates.values() if c.kind == "window")

    def list_tray_icons(self) -> tuple[SurfaceCandidate, ...]:
        with self._lock:
            self._ensure_open()
            self._generation += 1
            self._candidates = {
                ref: c for ref, c in self._candidates.items() if c.kind != "tray_icon"
            }
            self._native_keys = {
                ref: key for ref, key in self._native_keys.items() if ref in self._candidates
            }
            rows = self._host.list_tray_buttons()
            for serial, name, bounds, can_invoke in rows:
                operations = ("invoke", "click_point") if can_invoke else ("click_point",)
                self._register("tray_icon", name, bounds, operations, serial)
            return tuple(c for c in self._candidates.values() if c.kind == "tray_icon")

    # ---------------- 候选解析与操作 ----------------

    def _resolve(self, ref: str, generation: int) -> tuple[SurfaceCandidate, int]:
        with self._lock:
            self._ensure_open()
            candidate = self._candidates.get(ref)
            if candidate is None or candidate.surface_generation != generation:
                raise ComputerControlError(
                    "computer_surface_unknown",
                    "语义定位候选不存在或观察代数已过期，请重新 list_surfaces 观察",
                )
            return candidate, self._native_keys[ref]

    def resolve_candidate(self, ref: str, generation: int) -> SurfaceCandidate | None:
        """只读解析（区域失效复核用）；未命中返回 None 而不是抛错。"""
        with self._lock:
            candidate = self._candidates.get(ref)
            if candidate is None or candidate.surface_generation != generation:
                return None
            return candidate

    def current_candidate_bounds(self, ref: str, generation: int) -> tuple | None:
        """候选的当前边界（窗口移动/消失检测）；失效返回 None。"""
        candidate = self.resolve_candidate(ref, generation)
        if candidate is None:
            return None
        native_key = self._native_keys.get(ref)
        if native_key is None:
            return None
        if candidate.kind == "window":
            return self._host.window_bounds(native_key)
        return self._host.element_bounds(native_key)

    def activate_window(self, ref: str, generation: int) -> None:
        candidate, native_key = self._resolve(ref, generation)
        if candidate.kind != "window" or "activate" not in candidate.supported_operations:
            raise ComputerControlError(
                "computer_invalid_action",
                f"候选 {candidate.kind} 不支持 activate 操作",
            )
        self._host.activate_window(native_key)

    def invoke_element(self, ref: str, generation: int) -> None:
        candidate, native_key = self._resolve(ref, generation)
        if "invoke" not in candidate.supported_operations:
            raise ComputerControlError(
                "computer_invalid_action",
                "该候选不支持语义调用（invoke），可依据 bounds 显式点击其中心点",
            )
        self._host.invoke_button(native_key)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._candidates = {}
            self._native_keys = {}
        close = getattr(self._host, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                _LOGGER.exception("关闭 Windows 语义定位 host 失败")
