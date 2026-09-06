"""Windows UIA 元素定位器（2026-09-06 阶段二，a11y-first 仿做）。

只读无障碍树 + 语义 Invoke/Value，绝不注入内存或 hook。所有真实 UIA 调用
经 ``windows_elements_uia.RealUIAHost`` 在专用 COM 线程执行（复用
windows_surfaces._ComThread 模式）；测试注入 collect_fn/click_fn/set_value_fn
seam 绕过 COM。ref 生命周期与 surface_candidates 一致：仅在最近一次
collect 后有效，过期抛 computer_element_stale（错误码稳定英文，设计 21 节）。
"""

from __future__ import annotations

import threading

from core.computer.models import ComputerControlError

MAX_ELEMENTS = 200
MAX_TREE_DEPTH = 14
_MAX_NAME_CHARS = 80


class WindowsElementLocator:
    """前台窗口元素收集 + 元素引用操作；线程安全，关闭幂等。"""

    def __init__(
        self,
        *,
        com_host=None,
        collect_fn=None,
        click_fn=None,
        set_value_fn=None,
    ) -> None:
        self._lock = threading.Lock()
        self._elements: dict[str, object] = {}
        self._closed = False
        if collect_fn is not None:
            # 测试注入路径：com_host 仅为兼容签名占位。
            self._collect_fn = collect_fn
            self._click_fn = click_fn
            self._set_value_fn = set_value_fn
            self._real = None
        else:
            from core.computer.platforms.windows_elements_uia import RealUIAHost

            self._real = RealUIAHost()
            self._collect_fn = self._real.collect
            self._click_fn = self._real.click
            self._set_value_fn = self._real.set_value

    def collect(self, hwnd: int) -> list[dict]:
        """收集前台窗口可交互控件列表，并替换既有 ref 全集（旧 ref 即刻过期）。"""
        with self._lock:
            self._require_open()
            listing, elements = self._collect_fn(int(hwnd))
            self._elements = elements
            return listing

    def click(self, ref: str) -> None:
        with self._lock:
            self._require_open()
            self._click_fn(ref)

    def set_value(self, ref: str, text: str) -> None:
        with self._lock:
            self._require_open()
            self._set_value_fn(ref, text)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
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
