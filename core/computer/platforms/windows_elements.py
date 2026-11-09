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
        probe_fn=None,
    ) -> None:
        self._lock = threading.Lock()
        self._elements: dict[str, object] = {}
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

    def probe(self, hwnd: int, matcher: dict) -> dict | None:
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
            return probe_fn(int(hwnd), payload)

    def collect(self, hwnd: int) -> list[dict]:
        """收集前台窗口可交互控件列表，并替换既有 ref 全集（旧 ref 即刻过期）。"""
        with self._lock:
            self._require_open()
            self._ensure_real_locked()
            listing, elements = self._collect_fn(int(hwnd))
            self._elements = elements
            return listing

    def invalidate(self) -> None:
        """清空元素 ref 全集（通用化重构 Task B）：画面代数变化时由 backend 调用，
        全部旧 ref 立即过期，后续 click/set_value 抛 computer_element_stale。"""
        with self._lock:
            self._elements = {}
            real = self._real
        if real is not None:
            # 真实映射在 COM 线程内释放（本层字典只承载 ref 名，无 COM 对象）
            try:
                real.clear()
            except Exception:
                pass

    def click(self, ref: str) -> None:
        with self._lock:
            self._require_open()
            self._require_ref_locked(ref)
            self._ensure_real_locked()
            self._click_fn(ref)

    def set_value(self, ref: str, text: str) -> None:
        with self._lock:
            self._require_open()
            self._require_ref_locked(ref)
            self._ensure_real_locked()
            self._set_value_fn(ref, text)

    def _require_ref_locked(self, ref: str) -> None:
        """ref 有效性门禁：不在最近一次 collect 全集内即过期（宿主映射无法
        跨线程直清，locator 层先拦一道保证 invalidate 语义真实生效）。"""
        if ref not in self._elements:
            raise ComputerControlError(
                "computer_element_stale", "元素引用已失效，请重新 list_elements"
            )

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
