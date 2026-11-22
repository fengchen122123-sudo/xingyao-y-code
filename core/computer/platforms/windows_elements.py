from __future__ import annotations

import inspect
import threading

from core.computer.models import ComputerControlError

MAX_ELEMENTS = 200
MAX_TREE_DEPTH = 14
MAX_VISITED_NODES = 2000
_MAX_NAME_CHARS = 80


def _call_with_supported_kwargs(fn, *args, **kwargs):
    if not kwargs:
        return fn(*args)
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in params}
    return fn(*args, **filtered)


class WindowsElementLocator:
    """前台窗口元素收集 + 元素引用操作；线程安全，关闭幂等。"""

    def __init__(
        self,
        *,
        com_host=None,
        collect_fn=None,
        click_fn=None,
        set_value_fn=None,
        probe_fn=None,
    ) -> None:
        self._lock = threading.Lock()
        self._elements: dict[str, object] = {}
        self._hwnd: int = 0
        self._closed = False
        self._real = None
        if collect_fn is not None:
            # 测试注入路径：com_host 仅为兼容签名占位。
            self._collect_fn = collect_fn
            self._click_fn = click_fn
            self._set_value_fn = set_value_fn
            self._probe_fn = probe_fn
            self._needs_real = False
        else:
            # 生产路径惰性启动（通用化重构 Task A）：构造绝不触碰 COM——
            # 首次 collect/click/set_value/probe 才创建 RealUIAHost（专用 COM 线程）。
            self._collect_fn = None
            self._click_fn = None
            self._set_value_fn = None
            self._probe_fn = None
            self._needs_real = True

    def _ensure_real_locked(self) -> None:
        """首次真实调用时惰性构造 RealUIAHost（调用方必须持有锁）。"""
        if not self._needs_real or self._real is not None:
            return
        from core.computer.platforms.windows_elements_uia import RealUIAHost

        self._real = RealUIAHost()
        self._collect_fn = self._real.collect
        self._click_fn = self._real.click
        self._set_value_fn = self._real.set_value
        # 只读探测入口（wait_for 用）；宿主无 probe 时保持 None，probe 显式报错。
        self._probe_fn = getattr(self._real, "probe", None)

    def probe(
        self,
        hwnd: int,
        matcher: dict,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict | None:
        """只读条件探测（wait_for 轮询）：绝不调用 collect、绝不替换 ref 全集。

        collect 会整体替换 ref 映射（collect 的返回值内含新 ref 全集），等待期间
        一旦调用，模型手里的 el-N 立刻失效；因此 probe 只转发到宿主的只读探测，
        并对 element_* 条件先用本层 ref 全集做成员门禁（invalidate 后立即 stale）。
        """
        payload = dict(matcher or {})
        with self._lock:
            self._require_open()
            if str(payload.get("condition") or "") in (
                "element_exists",
                "element_enabled",
            ):
                self._require_ref_locked(str(payload.get("element_ref") or ""))
            self._ensure_real_locked()
            probe_fn = self._probe_fn
            if probe_fn is None:
                raise ComputerControlError(
                    "computer_elements_unavailable",
                    "当前 UIA 元素定位器不支持只读条件探测",
                )
            return _call_with_supported_kwargs(
                probe_fn,
                int(hwnd),
                payload,
                deadline=deadline,
                cancel_event=cancel_event,
            )

    def collect(
        self,
        hwnd: int,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[dict]:
        """收集前台窗口可交互控件列表，并替换既有 ref 全集（旧 ref 即刻过期）。"""
        with self._lock:
            self._require_open()
            self._ensure_real_locked()
            target_hwnd = int(hwnd)
            listing, elements = _call_with_supported_kwargs(
                self._collect_fn,
                target_hwnd,
                deadline=deadline,
                cancel_event=cancel_event,
            )
            self._hwnd = target_hwnd
            self._elements = elements
            return listing

    def invalidate(self) -> None:
        """清空元素 ref 全集（通用化重构 Task B）：画面代数变化时由 backend 调用，
        全部旧 ref 立即过期，后续 click/set_value 抛 computer_element_stale。"""
        with self._lock:
            self._hwnd = 0
            self._elements = {}
            real = self._real
        if real is not None:
            try:
                real.clear()
            except Exception:
                pass

    def click(self, ref: str, *, expected_hwnd: int | None = None) -> None:
        with self._lock:
            self._require_open()
            self._require_hwnd_locked(expected_hwnd)
            self._require_ref_locked(ref)
            self._ensure_real_locked()
            _call_with_supported_kwargs(
                self._click_fn, ref, expected_hwnd=expected_hwnd
            )

    def set_value(
        self, ref: str, text: str, *, expected_hwnd: int | None = None
    ) -> None:
        with self._lock:
            self._require_open()
            self._require_hwnd_locked(expected_hwnd)
            self._require_ref_locked(ref)
            self._ensure_real_locked()
            _call_with_supported_kwargs(
                self._set_value_fn, ref, text, expected_hwnd=expected_hwnd
            )

    def _require_hwnd_locked(self, expected_hwnd: int | None) -> None:
        if expected_hwnd is not None and self._hwnd and int(expected_hwnd) != int(self._hwnd):
            raise ComputerControlError(
                "computer_element_stale", "前台窗口已变化，元素引用已失效，请重新 list_elements"
            )

    def _require_ref_locked(self, ref: str) -> None:
        if ref not in self._elements:
            raise ComputerControlError(
                "computer_element_stale", "元素引用已失效，请重新 list_elements"
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._hwnd = 0
            self._elements = {}
            real = self._real
        if real is not None:
            try:
                real.close()
            except Exception:
                import logging

                logging.getLogger(__name__).exception("关闭 UIA 元素定位器失败")

    def _require_open(self) -> None:
        if self._closed:
            raise ComputerControlError(
                "computer_elements_unavailable", "UIA 元素定位器已关闭"
            )
