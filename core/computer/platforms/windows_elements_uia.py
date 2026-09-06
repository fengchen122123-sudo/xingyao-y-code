"""RealUIAHost：真实 COM 线程 UIA 元素收集与语义操作（2026-09-06 阶段二）。

复用 windows_surfaces._ComThread（专用 COM 线程 + 有界命令队列）与
UIAutomationCore CLSID；UIA 元素对象绝不离开 COM 线程，跨线程只传
脱敏 dict（ref/kind/name/bounds/enabled）。所有失败归一为稳定错误码：
- collect 失败 → computer_backend_error
- ref 不存在/已过期 → computer_element_stale
- 元素不支持 Invoke/Value pattern → computer_element_unsupported
"""

from __future__ import annotations

import logging

from core.computer.models import ComputerControlError
from core.computer.platforms.windows_elements import MAX_ELEMENTS, MAX_TREE_DEPTH

_LOGGER = logging.getLogger(__name__)

# UIA 稳定常量（UIAutomationCore 规范固定）。
_UIA_CONTROL_TYPE_PROPERTY_ID = 30003
_UIA_INVOKE_PATTERN_ID = 10000
_UIA_VALUE_PATTERN_ID = 10002
_CUIAUTOMATION_CLSID = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"

# 只收集的可交互控件类型 ID（UIA ControlType IDs，规范固定）。
_INTERACTIVE_CONTROL_TYPE_IDS = frozenset(
    {
        50000,  # Button
        50002,  # CheckBox
        50003,  # ComboBox
        50004,  # Edit
        50005,  # Hyperlink
        50007,  # ListItem
        50008,  # List（微信聊天消息列表等容器，读屏需要）
        50011,  # MenuItem
        50013,  # RadioButton
        50015,  # Slider
        50019,  # TabItem
        50024,  # TreeItem
        50029,  # DataItem
        50030,  # Document
        50031,  # SplitButton
    }
)


class RealUIAHost:
    """真实 UIA 主机：全部 UIA 访问在专属 COM 线程执行。"""

    def __init__(self) -> None:
        from core.computer.platforms.windows_surfaces import _ComThread

        self._com = _ComThread()
        # ref → IUIAutomationElement 映射：只在 COM 线程的 job 内读写
        # （UIA 对象绝不离开该线程；COM 线程单消费者，无并发访问）。
        self._elements: dict[str, object] = {}
        self._com.start()

    # ---------------- 对外三个入口（线程安全经 _ComThread marshal） ----------------

    def collect(self, hwnd: int):
        """ElementFromHandle → ControlView BFS 收集。

        返回 (listing, ref→IUIAutomationElement 映射)；映射只留在本对象内存，
        供同线程后续 click/set_value 经 marshal 使用。
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
            counter = 0
            queue = [(root, 0)]
            while queue and counter < MAX_ELEMENTS:
                node, depth = queue.pop(0)
                try:
                    child = walker.GetFirstChildElement(node)
                except Exception:
                    child = None
                while child is not None:
                    if depth + 1 <= MAX_TREE_DEPTH:
                        queue.append((child, depth + 1))
                    if counter < MAX_ELEMENTS:
                        try:
                            type_id = child.CurrentControlType
                        except Exception:
                            type_id = 0
                        if type_id in _INTERACTIVE_CONTROL_TYPE_IDS:
                            counter += 1
                            ref = f"el-{counter}"
                            try:
                                name = (child.CurrentName or "")[:MAX_NAME_CHARS]
                            except Exception:
                                name = ""
                            try:
                                rect = child.CurrentBoundingRectangle
                                bounds = [
                                    int(rect.left), int(rect.top),
                                    int(rect.right), int(rect.bottom),
                                ]
                            except Exception:
                                bounds = [0, 0, 0, 0]
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
                    try:
                        child = walker.GetNextSiblingElement(child)
                    except Exception:
                        child = None
            # 映射替换在 COM 线程内完成：旧 ref 即刻过期。
            self._elements = elements
            return listing, elements

        return self._com.call(job)

    def click(self, ref: str) -> None:
        def job(uia):
            element = self._require(ref)
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

    def set_value(self, ref: str, text: str) -> None:
        def job(uia):
            element = self._require(ref)
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

    # ---------------- 内部 ----------------

    def _require(self, ref: str):
        """COM 线程内校验 ref 并取回元素；不存在即过期。"""
        element = self._elements.get(str(ref))
        if element is None:
            raise ComputerControlError(
                "computer_element_stale", "元素引用已失效，请重新 list_elements"
            )
        return element


def _control_type_name(uia, type_id: int) -> str:
    """控件类型 ID → 稳定英文名（ButtonControl 等）；未知回空串。"""
    try:
        programmatic = uia.GetProgrammaticName(type_id) or ""
    except Exception:
        return ""
    if programmatic.startswith("ControlType."):
        return programmatic[len("ControlType."):] + "Control"
    return programmatic
