from __future__ import annotations

import logging
import threading
import time

from core.computer.models import ComputerControlError
from core.computer.platforms.windows_elements import (
    MAX_ELEMENTS,
    MAX_TREE_DEPTH,
    MAX_VISITED_NODES,
)

_LOGGER = logging.getLogger(__name__)

_UIA_CONTROL_TYPE_PROPERTY_ID = 30003
_UIA_INVOKE_PATTERN_ID = 10000
_UIA_VALUE_PATTERN_ID = 10002
_CUIAUTOMATION_CLSID = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"

_MAX_NAME_CHARS = 80
_PROBE_SUMMARY_CHARS = 120

_INTERACTIVE_CONTROL_TYPE_IDS = frozenset(
    {
        50000,
        50002,
        50003,
        50004,
        50005,
        50007,
        50008,
        50011,
        50013,
        50015,
        50019,
        50024,
        50029,
        50030,
        50031,
    }
)


class RealUIAHost:
    """真实 UIA 主机：全部 UIA 访问在专属 COM 线程执行。"""

    def __init__(self) -> None:
        from core.computer.platforms.windows_surfaces import _ComThread

        self._com = _ComThread()
        # ref → IUIAutomationElement 与元信息映射：只在 COM 线程的 job 内读写
        # （UIA 对象绝不离开该线程；COM 线程单消费者，无并发访问）。
        self._elements: dict[str, object] = {}
        self._element_meta: dict[str, dict] = {}
        self._com.start()

    @staticmethod
    def _safe_bounds(node) -> list[int]:
        try:
            rect = node.CurrentBoundingRectangle
            return [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
        except Exception:
            return [0, 0, 0, 0]

    @staticmethod
    def _safe_is_offscreen(node) -> bool:
        try:
            return bool(node.CurrentIsOffscreen)
        except Exception:
            return False

    @staticmethod
    def _safe_runtime_id(node) -> tuple[int, ...]:
        try:
            rtid = node.GetRuntimeId()
            return tuple(int(v) for v in rtid) if rtid is not None else ()
        except Exception:
            return ()

    def collect(
        self,
        hwnd: int,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ):
        """ElementFromHandle → ControlView BFS 收集。

        返回 (listing, ref→None 字典)；真实 ref→IUIAutomationElement 映射只
        留在本对象内存（COM 线程内读写，见 _require），供后续 click/set_value
        经 marshal 使用。返回字典仅承载 ref 全集供 locator 做成员门禁——
        COM 对象绝不随返回值离开线程（跨 apartment 传出/释放是未定义行为）。
        """

        def job(uia):
            if uia is None:
                raise ComputerControlError(
                    "computer_backend_error", "UI Automation 未初始化"
                )
            try:
                root = uia.ElementFromHandle(int(hwnd))
            except Exception as error:
                raise ComputerControlError(
                    "computer_backend_error", f"读取前台窗口控件树失败：{error}"
                ) from error
            walker = uia.ControlViewWalker
            listing: list[dict] = []
            elements: dict[str, object] = {}
            element_meta: dict[str, dict] = {}
            counter = 0
            visited = 0
            queue = [(root, 0)]
            while queue and counter < MAX_ELEMENTS and visited < MAX_VISITED_NODES:
                if cancel_event is not None and cancel_event.is_set():
                    raise ComputerControlError("computer_cancelled", "UIA 采集已取消")
                if deadline is not None and time.monotonic() >= deadline:
                    raise ComputerControlError("computer_wait_timeout", "UIA 采集超时")
                node, depth = queue.pop(0)
                visited += 1
                try:
                    child = walker.GetFirstChildElement(node)
                except Exception:
                    child = None
                while child is not None and visited < MAX_VISITED_NODES:
                    if cancel_event is not None and cancel_event.is_set():
                        raise ComputerControlError("computer_cancelled", "UIA 采集已取消")
                    if deadline is not None and time.monotonic() >= deadline:
                        raise ComputerControlError("computer_wait_timeout", "UIA 采集超时")
                    visited += 1
                    if depth + 1 <= MAX_TREE_DEPTH:
                        queue.append((child, depth + 1))
                    if counter < MAX_ELEMENTS:
                        try:
                            type_id = int(child.CurrentControlType)
                        except Exception:
                            type_id = 0
                        if type_id in _INTERACTIVE_CONTROL_TYPE_IDS:
                            bounds = self._safe_bounds(child)
                            if (
                                not self._safe_is_offscreen(child)
                                and bounds[2] > bounds[0]
                                and bounds[3] > bounds[1]
                            ):
                                counter += 1
                                ref = f"el-{counter}"
                                try:
                                    name = (child.CurrentName or "")[:_MAX_NAME_CHARS]
                                except Exception:
                                    name = ""
                                try:
                                    enabled = bool(child.CurrentIsEnabled)
                                except Exception:
                                    enabled = True
                                listing.append({
                                    "ref": ref,
                                    "kind": _control_type_name(uia, type_id),
                                    "name": name,
                                    "bounds": bounds,
                                    "enabled": enabled,
                                })
                                elements[ref] = child
                                element_meta[ref] = {
                                    "hwnd": int(hwnd),
                                    "control_type": type_id,
                                    "bounds": tuple(bounds),
                                    "runtime_id": self._safe_runtime_id(child),
                                    "name": name,
                                }
                    try:
                        child = walker.GetNextSiblingElement(child)
                    except Exception:
                        child = None
            self._elements = elements
            self._element_meta = element_meta
            return listing, {ref: None for ref in elements}

        timeout_code = (
            "computer_wait_timeout"
            if deadline is not None
            else "computer_surfaces_unavailable"
        )
        return self._com.call(
            job,
            deadline=deadline,
            cancel_event=cancel_event,
            timeout_code=timeout_code,
        )

    def clear(self) -> None:
        """在 COM 线程内清空 ref→元素映射（画面代数变化时释放引用）。"""
        def job(_uia):
            self._elements = {}
            self._element_meta = {}

        self._com.call(job)

    def probe(
        self,
        hwnd: int,
        matcher: dict,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict | None:
        payload = dict(matcher or {})
        timeout_code = (
            "computer_wait_timeout"
            if deadline is not None
            else "computer_surfaces_unavailable"
        )
        return self._com.call(
            lambda uia: self._probe_job(
                uia,
                int(hwnd),
                payload,
                deadline=deadline,
                cancel_event=cancel_event,
            ),
            deadline=deadline,
            cancel_event=cancel_event,
            timeout_code=timeout_code,
        )

    def _verify_element_before_action(
        self, ref: str, *, expected_hwnd: int | None = None
    ):
        element = self._require(ref)
        meta = getattr(self, "_element_meta", {}).get(str(ref), {})
        if (
            expected_hwnd is not None
            and meta.get("hwnd")
            and int(meta["hwnd"]) != int(expected_hwnd)
        ):
            raise ComputerControlError(
                "computer_element_stale", f"元素 {ref} 所属窗口已切换，请重新 list_elements"
            )
        try:
            live_type = int(element.CurrentControlType)
            rect = element.CurrentBoundingRectangle
            live_bounds = (
                int(rect.left),
                int(rect.top),
                int(rect.right),
                int(rect.bottom),
            )
            live_enabled = bool(element.CurrentIsEnabled)
        except Exception as error:
            raise ComputerControlError(
                "computer_element_stale", f"元素 {ref} 已失效：{type(error).__name__}"
            ) from error
        if meta.get("control_type") is not None and live_type != meta["control_type"]:
            raise ComputerControlError(
                "computer_element_stale", f"元素 {ref} 类型已变化，请重新 list_elements"
            )
        recorded_rtid = meta.get("runtime_id") or ()
        live_rtid = self._safe_runtime_id(element)
        if recorded_rtid and live_rtid and live_rtid != recorded_rtid:
            raise ComputerControlError(
                "computer_element_stale", f"元素 {ref} 已被替换，请重新 list_elements"
            )
        if self._safe_is_offscreen(element):
            raise ComputerControlError(
                "computer_element_stale", f"元素 {ref} 已离开可见区域，请重新 list_elements"
            )
        if live_bounds[2] <= live_bounds[0] or live_bounds[3] <= live_bounds[1]:
            raise ComputerControlError(
                "computer_element_stale", f"元素 {ref} 边界已失效，请重新 list_elements"
            )
        if meta.get("bounds") and live_bounds != tuple(meta["bounds"]):
            raise ComputerControlError(
                "computer_element_stale", f"元素 {ref} 位置已迁移，请重新 list_elements"
            )
        if not live_enabled:
            raise ComputerControlError(
                "computer_element_unsupported", f"元素 {ref} 当前已禁用，不可操作"
            )
        return element

    def click(self, ref: str, *, expected_hwnd: int | None = None) -> None:
        def job(uia):
            element = self._verify_element_before_action(
                ref, expected_hwnd=expected_hwnd
            )
            try:
                import comtypes.client

                pattern = element.GetCurrentPattern(_UIA_INVOKE_PATTERN_ID)
                if pattern is None:
                    raise ComputerControlError(
                        "computer_element_unsupported",
                        f"元素 {ref} 不支持 Invoke（请改用坐标点击）",
                    )
                invoke = pattern.QueryInterface(
                    comtypes.gen.UIAutomationClient.IUIAutomationInvokePattern
                )
                invoke.Invoke()
            except ComputerControlError:
                raise
            except Exception as error:
                raise ComputerControlError(
                    "computer_backend_error", f"元素 {ref} 点击失败：{error}"
                ) from error

        self._com.call(job)

    def set_value(
        self, ref: str, text: str, *, expected_hwnd: int | None = None
    ) -> None:
        def job(uia):
            element = self._verify_element_before_action(
                ref, expected_hwnd=expected_hwnd
            )
            try:
                import comtypes.client

                pattern = element.GetCurrentPattern(_UIA_VALUE_PATTERN_ID)
                if pattern is None:
                    raise ComputerControlError(
                        "computer_element_unsupported",
                        f"元素 {ref} 不支持 Value 写入（请改用剪贴板粘贴路径）",
                    )
                value = pattern.QueryInterface(
                    comtypes.gen.UIAutomationClient.IUIAutomationValuePattern
                )
                value.SetValue(str(text))
            except ComputerControlError:
                raise
            except Exception as error:
                raise ComputerControlError(
                    "computer_backend_error", f"元素 {ref} 写值失败：{error}"
                ) from error

        self._com.call(job)

    def close(self) -> None:
        try:
            self._com.close()
        except Exception:
            _LOGGER.exception("关闭 UIA COM 线程失败")

    def _probe_job(
        self,
        uia,
        hwnd: int,
        matcher: dict,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict | None:
        """COM 线程内的 probe 实现（只读；不触碰 self._elements 的赋值）。"""
        condition = str(matcher.get("condition") or "")
        if condition in ("element_exists", "element_enabled"):
            return self._probe_element(condition, matcher.get("element_ref"))
        if uia is None:
            raise ComputerControlError(
                "computer_backend_error", "UI Automation 未初始化"
            )
        return self._probe_text(
            uia,
            hwnd,
            str(matcher.get("text") or ""),
            deadline=deadline,
            cancel_event=cancel_event,
        )

    def _probe_element(self, condition: str, element_ref: object) -> dict | None:
        """element_* 条件：从既有映射按 ref 只读取值，不重建映射、不改编号。"""
        ref = str(element_ref or "")
        element = self._elements.get(ref)
        if element is None:
            raise ComputerControlError(
                "computer_element_stale", "元素引用已失效，请重新 list_elements"
            )
        try:
            name = str(element.CurrentName or "")[:_MAX_NAME_CHARS]
            if condition == "element_enabled" and not bool(element.CurrentIsEnabled):
                return None
        except Exception:
            # 元素随窗口消失或单节点 COM 异常：本次不命中，继续轮询到超时。
            return None
        return {"condition": condition, "ref": ref, "name": name}

    def _probe_text(
        self,
        uia,
        hwnd: int,
        needle: str,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict | None:
        if not needle:
            return None
        folded = needle.casefold()
        try:
            root = uia.ElementFromHandle(int(hwnd))
        except Exception:
            # 句柄失效/窗口已关闭：本次不命中，交由上层继续轮询。
            return None
        walker = uia.ControlViewWalker
        queue: list[tuple[object, int]] = [(root, 0)]
        visited = 0
        while queue and visited < MAX_VISITED_NODES and visited < MAX_ELEMENTS:
            if cancel_event is not None and cancel_event.is_set():
                return None
            if deadline is not None and time.monotonic() >= deadline:
                return None
            node, depth = queue.pop(0)
            visited += 1
            if depth > 0:
                name, value = _probe_node_texts(node)
                if folded in name.casefold() or folded in value.casefold():
                    return {
                        "condition": "text_exists",
                        "kind": _probe_node_kind(uia, node),
                        "name": name[:_PROBE_SUMMARY_CHARS],
                        "value": value[:_PROBE_SUMMARY_CHARS],
                    }
            if depth >= MAX_TREE_DEPTH:
                continue
            try:
                child = walker.GetFirstChildElement(node)
            except Exception:
                child = None
            while child is not None:
                if visited + len(queue) < MAX_ELEMENTS:
                    queue.append((child, depth + 1))
                try:
                    child = walker.GetNextSiblingElement(child)
                except Exception:
                    child = None
        return None

    def _require(self, ref: str):
        element = self._elements.get(str(ref))
        if element is None:
            raise ComputerControlError(
                "computer_element_stale", "元素引用已失效，请重新 list_elements"
            )
        return element


def _probe_node_texts(node) -> tuple[str, str]:
    """单节点的 (CurrentName, ValuePattern 当前值)；任一读取失败按空串处理。

    单节点 COM 异常绝不上抛：个别控件在树变化瞬间读属性会抛，跳过即可，
    不能因为一个坏节点让整次等待失败。
    """
    name = ""
    try:
        name = str(node.CurrentName or "")
    except Exception:
        name = ""
    value = ""
    try:
        import comtypes.client

        pattern = node.GetCurrentPattern(_UIA_VALUE_PATTERN_ID)
        if pattern is not None:
            interface = comtypes.gen.UIAutomationClient.IUIAutomationValuePattern
            value = str(pattern.QueryInterface(interface).CurrentValue or "")
    except Exception:
        value = ""
    return name, value


def _probe_node_kind(uia, node) -> str:
    try:
        return _control_type_name(uia, node.CurrentControlType)
    except Exception:
        return ""


def _control_type_name(uia, type_id: int) -> str:
    try:
        programmatic = uia.GetProgrammaticName(type_id) or ""
    except Exception:
        return ""
    if programmatic.startswith("ControlType."):
        return programmatic[len("ControlType."):] + "Control"
    return programmatic
